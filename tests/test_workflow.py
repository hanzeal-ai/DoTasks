from __future__ import annotations

import os
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from core.run_context import model_run_context
from core.service import TaskboardService


class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "APage.tsx").write_text("export function APage() { return null }\n", encoding="utf-8")
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.email", "test@example.com"], check=True)
        subprocess.run(["git", "-C", str(self.project), "config", "user.name", "Test"], check=True)
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "initial"], check=True)
        self.old_vault = os.environ.get("DOTASKS_OBSIDIAN_VAULT")
        os.environ["DOTASKS_OBSIDIAN_VAULT"] = str(Path(self.temp.name) / "vault")
        self.service = TaskboardService(self.temp.name)
        self.service.set_dispatcher_enabled(True)

    def tearDown(self):
        if self.old_vault is None:
            os.environ.pop("DOTASKS_OBSIDIAN_VAULT", None)
        else:
            os.environ["DOTASKS_OBSIDIAN_VAULT"] = self.old_vault
        self.temp.cleanup()

    def analysis(self, title="workflow", acceptance_plan=None):
        project = str(self.project)
        self.service.report_location_status(project, True, "connected", "ok", {
            "tool": "codegraph_explore", "files": ["src/APage.tsx"], "symbols": ["APage"],
        })
        analysis = self.service.prepare_location_analysis({"title": title, "goal": "实现功能", "project": project, "modules": ["a-page"]})
        target = {
            "file": "src/APage.tsx", "mode": "modify", "symbols": ["APage"],
            "tasks": [{"symbol": "APage", "action": "修改组件"}],
        }
        return self.service.complete_location_analysis(
            analysis["analysis_id"],
            {"tool": "codegraph_explore", "files": ["src/APage.tsx"]},
            [target],
            acceptance_plan or [{
                "criterion": "功能可用", "file": "src/APage.tsx",
                "symbol": "APage", "method": "测试", "expected": "通过",
            }],
            {"decision": "independent"},
            {"targets": [target]},
            {
                "checks": [{"id": "project-rules", "description": "遵守项目规范", "kind": "code"}],
                "quality_gates": {"code_review": {"required": True, "reason": "code change"}},
            },
        )

    def task(self, title="workflow", **overrides):
        project = str(self.project)
        located_acceptance_plan = overrides.pop("located_acceptance_plan", None)
        analysis = self.analysis(title, located_acceptance_plan)
        payload = {
            "title": title, "project": project, "goal": "实现功能", "scope": ["组件"], "out_of_scope": [],
            "acceptance_criteria": ["功能可用"], "modules": ["a-page"], "location_analysis_id": analysis["id"],
            "dependency_analysis": {"decision": "independent"},
            "implementation_contract": {"targets": [{"file": "src/APage.tsx", "mode": "modify", "symbols": ["APage"], "tasks": [{"action": "修改组件", "symbol": "APage"}]}]},
            "review_contract": {
                "checks": [{"id": "project-rules", "description": "遵守项目规范", "kind": "code"}],
                "quality_gates": {"code_review": {"required": True, "reason": "code change"}},
            },
        }
        payload.update(overrides)
        return self.service.create_task({**payload, "status": "ready"})

    def deliver(self, task, thread="dev", evidence_status=None, *, changed=True):
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], claim["run"]["run_type"], thread, claim["run"]["id"])
        changed_locations = []
        if changed:
            path = self.project / "src" / "APage.tsx"
            path.write_text(path.read_text(encoding="utf-8") + "// delivery\n", encoding="utf-8")
            changed_locations = [{"file": "src/APage.tsx", "symbols": ["APage"]}]
        evidence = {"criterion": "功能可用", "evidence": "测试通过"}
        if evidence_status:
            evidence["status"] = evidence_status
        return self.service.submit_delivery(
            claim["run"]["id"], "完成", "测试通过", changed_locations, [evidence]
        )

    @staticmethod
    def review_contract(code_review: bool):
        return {
            "checks": [{"id": "project-rules", "description": "遵守项目规范", "kind": "code"}],
            "quality_gates": {
                "code_review": {
                    "required": code_review,
                    "reason": "是否属于代码任务",
                },
            },
        }

    def test_finalize_task_intake_derives_duplicate_contract_fields(self):
        history_queries = []
        original_search = self.service.obsidian.search_task_dependencies

        def tracked_search(*args, **kwargs):
            history_queries.append((args, kwargs))
            return original_search(*args, **kwargs)

        self.service.obsidian.search_task_dependencies = tracked_search
        prepared = self.service.prepare_location_analysis({
            "title": "单包创建", "goal": "实现功能", "project": str(self.project),
            "modules": ["a-page"],
        })

        result = self.service.finalize_task_intake({
            "intake_kind": "task",
            "analysis_id": prepared["analysis_id"],
            "title": "单包创建", "project": str(self.project), "goal": "实现功能",
            "scope": ["组件"], "out_of_scope": [], "modules": ["a-page"],
            "location_evidence": {
                "tool": "codegraph_explore", "query": "APage",
                "files": ["src/APage.tsx"], "symbols": ["APage"],
            },
            "targets": [{"file": "src/APage.tsx", "mode": "modify", "symbols": ["APage"], "reason": "主组件", "tasks": [{"symbol": "APage", "action": "修改组件"}]}],
            "review_checks": [{
                "id": "遵守项目规范",
                "description": "遵守项目规范",
                "kind": "static",
            }],
            "quality_gates": {
                "code_review": {"required": True, "reason": "生产代码变更"},
            },
            "acceptance_plan": [{
                "criterion": "功能可用", "file": "src/APage.tsx", "symbol": "APage",
                "method": "运行聚焦测试", "command": "test -f src/APage.tsx",
                "expected": "检查通过", "check_type": "automated",
            }],
        })

        self.assertEqual("created", result["status"])
        task = self.service.get_task(result["task_id"])
        self.assertEqual("ready", task["status"])
        self.assertEqual(["功能可用"], task["acceptance_criteria"])
        self.assertEqual(
            [{
                "file": "src/APage.tsx", "symbols": ["APage"], "reason": "主组件",
                "mode": "modify",
                "tasks": [{"symbol": "APage", "action": "修改组件"}],
            }],
            task["implementation_contract"]["targets"],
        )
        self.assertEqual(
            [{"id": "遵守项目规范", "description": "遵守项目规范", "kind": "static"}],
            task["review_contract"]["checks"],
        )
        self.assertEqual(
            {
                "code_review": {"required": True, "reason": "生产代码变更"},
            },
            task["review_contract"]["quality_gates"],
        )
        self.assertEqual("independent", task["dependency_analysis"]["decision"])
        self.assertNotIn("dependency_candidates", prepared["obsidian"])
        self.assertEqual(1, len(history_queries))

    def test_create_target_needs_no_existing_symbol_or_separate_steps(self):
        prepared = self.service.prepare_location_analysis({
            "title": "补充测试", "goal": "增加页面测试", "project": str(self.project),
            "modules": ["a-page"],
        })
        result = self.service.finalize_task_intake({
            "intake_kind": "task",
            "analysis_id": prepared["analysis_id"],
            "title": "补充测试", "project": str(self.project), "goal": "增加页面测试",
            "scope": ["测试"], "out_of_scope": [], "modules": ["a-page"],
            "location_evidence": {
                "tool": "codegraph_explore", "query": "APage tests",
                "files": ["src/APage.test.tsx"],
            },
            "targets": [{
                "file": "src/APage.test.tsx", "mode": "create", "symbols": [],
                "tasks": [{"action": "覆盖页面身份切换行为"}],
            }],
            "review_checks": [{
                "id": "测试有效", "description": "测试覆盖目标行为", "kind": "static",
            }],
            "quality_gates": {
                "code_review": {"required": True, "reason": "新增代码测试"},
            },
            "acceptance_plan": [{
                "criterion": "测试可执行", "file": "src/APage.test.tsx", "symbol": "",
                "method": "运行测试", "command": "test -f src/APage.test.tsx",
                "expected": "测试文件存在", "check_type": "automated",
            }],
            "dependency_analysis": {"decision": "independent"},
        })
        task = self.service.get_task(result["task_id"])
        target = task["implementation_contract"]["targets"][0]
        self.assertEqual("create", target["mode"])
        self.assertEqual([], target["symbols"])
        self.assertEqual("覆盖页面身份切换行为", target["tasks"][0]["action"])

    def test_multiple_dependencies_are_scheduler_only_and_all_must_finish(self):
        first = self.task("multi-first", review_contract=self.review_contract(False))
        second = self.task("multi-second", review_contract=self.review_contract(False))
        dependent = self.task(
            "multi-dependent",
            dependency_analysis={
                "decision": "depends_on",
                "depends_tasks": [first["id"], second["id"]],
                "history_tasks": [first["id"]],
            },
        )
        stored = dependent["dependency_analysis"]
        self.assertEqual([first["id"], second["id"]], stored["depends_tasks"])
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET auto_dispatch=0 WHERE id IN (?, ?)",
                (first["id"], second["id"]),
            )
        self.assertIsNone(self.service.claim_next_task("worker"))
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET auto_dispatch=1 WHERE id=?", (first["id"],))
        self.deliver(
            first, "multi-first-thread", evidence_status="passed", changed=False
        )
        self.assertIsNone(self.service.claim_next_task("worker"))
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET auto_dispatch=1 WHERE id=?", (second["id"],))
        self.deliver(
            second, "multi-second-thread", evidence_status="passed", changed=False
        )
        claim = self.service.claim_next_task("worker")
        self.assertEqual(dependent["id"], claim["task"]["id"])
        model_context = model_run_context(claim["run"]["context_snapshot"])
        self.assertNotIn("depends_tasks", model_context)
        self.assertNotIn("history_tasks", model_context)

    def test_non_code_delivery_skips_code_review(self):
        task = self.task(
            "skip-both",
            review_contract=self.review_contract(False),
        )

        delivered = self.deliver(task, evidence_status="passed", changed=False)

        self.assertEqual("done", delivered["task"]["status"])
        self.assertEqual("done", delivered["next_stage"])
        self.assertEqual("completed", delivered["run"]["status"])
        self.assertFalse(delivered["review_dispatch_required"])
        self.assertEqual([], self.service.list_reviews(task["id"]))

    def test_skipped_code_review_still_requires_passed_development_evidence(self):
        task = self.task(
            "skip-acceptance-with-pending-evidence",
            review_contract=self.review_contract(False),
        )

        with self.assertRaisesRegex(
            ValueError, "must pass during development"
        ):
            self.deliver(task, changed=False)

    def test_finalize_task_intake_pauses_for_strong_dependency_candidate(self):
        existing = self.task("重复模块任务")
        prepared = self.service.prepare_location_analysis({
            "title": "重复模块任务", "goal": "实现功能", "project": str(self.project),
            "modules": ["a-page"],
        })
        payload = {
            "intake_kind": "task",
            "analysis_id": prepared["analysis_id"],
            "title": "重复模块任务", "project": str(self.project), "goal": "实现功能",
            "scope": ["组件"], "out_of_scope": [], "modules": ["a-page"],
            "location_evidence": {
                "tool": "codegraph_explore", "query": "APage",
                "files": ["src/APage.tsx"], "symbols": ["APage"],
            },
            "targets": [{"file": "src/APage.tsx", "mode": "modify", "symbols": ["APage"], "tasks": [{"symbol": "APage", "action": "修改组件"}]}],
            "review_checks": [{"id": "project-rules", "description": "遵守项目规范", "kind": "code"}],
            "quality_gates": {
                "code_review": {"required": True, "reason": "代码修改"},
            },
            "acceptance_plan": [{
                "criterion": "功能可用", "file": "src/APage.tsx", "symbol": "APage",
                "method": "运行聚焦测试", "command": "test -f src/APage.tsx",
                "expected": "检查通过", "check_type": "automated",
            }],
        }

        result = self.service.finalize_task_intake(payload)

        self.assertEqual("requires_confirmation", result["status"])
        self.assertEqual(existing["id"], result["dependency_analysis"]["candidates"][0]["task_id"])
        self.assertEqual("prepared", self.service.get_location_analysis(prepared["analysis_id"])["status"])

    def test_manual_acceptance_checks_do_not_define_code_review_verdict(self):
        task = self.task(
            "review-environment-self-heal",
            located_acceptance_plan=[{
                "criterion": "功能可用",
                "file": "src/APage.tsx",
                "symbol": "APage",
                "method": "运行聚焦检查",
                "command": (
                    "test -f .venv/review.ready || "
                    "(echo 'ModuleNotFoundError: No module named review_runtime' >&2; exit 1)"
                ),
                "expected": "项目运行环境可用",
                "check_type": "automated",
                "failure_category": "environment",
                "repair_command": "mkdir -p .venv && touch .venv/review.ready",
            }],
        )
        self.deliver(task)
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(
            task["id"], "code_review", "review-thread", review["run"]["id"]
        )

        review_context = model_run_context(review["run"]["context_snapshot"])
        result = self.service.review_code(
            task["id"],
            review["run"]["id"],
            "pass",
            passed_items=["project-rules"],
        )

        self.assertNotIn("acceptance_plan", review_context)
        self.assertNotIn("acceptance_criteria", review_context["task"])
        self.assertEqual(
            ["review_code"],
            review["run"]["context_snapshot"]["tool_contract"]["allowed_completion_tools"],
        )
        self.assertEqual("done", result["status"])

    def test_missing_tests_schedule_bounded_self_healing_rework(self):
        task = self.task("missing-tests")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(
            task["id"], "code_review", "review-thread", review["run"]["id"]
        )

        result = self.service.review_code(
            task["id"],
            review["run"]["id"],
            "fail",
            reasons=["缺少测试用例"],
            passed_items=[],
            failed_criteria=["project-rules"],
            failure_locations=[{"file": "tests/test_a_page.py", "symbols": []}],
        )

        self.assertEqual("rework", result["status"])
        self.assertEqual(
            {
                "category": "project",
                "attempt": 1,
                "attempt_limit": 2,
                "scheduled": True,
                "evidence": ["缺少测试用例"],
                "locations": [
                    {
                        "file": "tests/test_a_page.py", "symbols": [],
                        "mode": "create",
                        "tasks": [{"action": "修复project问题：缺少测试用例"}],
                    }
                ],
            },
            result["implementation_contract"]["self_heal"],
        )
        with self.service.db.connection() as connection:
            target = connection.execute(
                "SELECT symbol FROM task_targets WHERE task_id=? AND file=?",
                (task["id"], "tests/test_a_page.py"),
            ).fetchone()
        self.assertEqual("", target["symbol"])
        retry = self.service.claim_next_task("worker")
        self.assertEqual("rework", retry["run"]["run_type"])
        self.assertIn("第 1/2 次自动自愈", retry["dispatch_prompt"])
        retry_context = model_run_context(retry["run"]["context_snapshot"])
        healing_target = next(
            item for item in retry_context["targets"]
            if item["file"] == "tests/test_a_page.py"
        )
        self.assertEqual("create", healing_target["mode"])
        self.assertEqual("修复project问题：缺少测试用例", healing_target["tasks"][0]["action"])
        self.assertTrue(any(
            event["event_type"] == "self_heal_scheduled"
            and event["payload"]["failure_category"] == "project"
            for event in self.service.list_events("task", task["id"])
        ))

    def test_recoverable_review_failure_requires_an_exact_repair_location(self):
        task = self.task("missing-repair-location")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(
            task["id"], "code_review", "review-thread", review["run"]["id"]
        )

        with self.assertRaisesRegex(ValueError, "exact failure_location file"):
            self.service.review_code(
                task["id"], review["run"]["id"], "fail",
                reasons=["缺少测试用例"], passed_items=[],
                failed_criteria=["project-rules"], failure_category="project",
            )

        current = self.service.get_task(task["id"])
        self.assertEqual("code_review", current["status"])
        self.assertEqual(review["run"]["id"], current["active_run_id"])

    def test_create_task_persists_disabled_auto_dispatch(self):
        owner = self.task("active-batch-owner")
        owner_claim = self.service.claim_next_task("owner-worker")
        self.assertEqual(owner["id"], owner_claim["task"]["id"])
        task = self.task("manual-dispatch", auto_dispatch=False)

        self.assertFalse(task["auto_dispatch"])
        self.assertEqual("ready", task["status"])
        self.assertIsNone(task["execution_batch"])
        self.assertIsNone(self.service.claim_next_task("worker"))

    def test_code_quality_review_failure_reworks_without_creating_acceptance_bug(self):
        task = self.task("combined-review-fail")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        result = self.service.review_code(
            task["id"], review["run"]["id"], "fail", reasons=["代码职责混杂"],
            passed_items=[], failed_criteria=["project-rules"],
        )
        self.assertEqual("rework", result["status"])
        self.assertFalse(any(item["type"] == "bug" for item in self.service.list_tasks()))
        queued = self.task("ordinary-ready-after-rework", auto_dispatch=False)
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET auto_dispatch=1 WHERE id=?", (queued["id"],),
            )
        retry = self.service.claim_next_task("worker")
        self.assertEqual(task["id"], retry["task"]["id"])
        self.assertEqual("original-dev", retry["resume_thread_id"])
        self.assertEqual("ready", self.service.get_task(queued["id"])["status"])

    def test_code_quality_review_stops_after_three_rework_rounds(self):
        task = self.task("review-rework-limit")
        self.deliver(task, "original-dev")
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET review_rework_count=3 WHERE id=?", (task["id"],),
            )
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(
            task["id"], "code_review", "review-thread", review["run"]["id"],
        )

        result = self.service.review_code(
            task["id"], review["run"]["id"], "fail",
            reasons=["代码职责仍然混杂"], passed_items=[],
            failed_criteria=["project-rules"],
        )

        self.assertEqual("waiting_confirmation", result["status"])
        self.assertFalse(result["auto_dispatch"])
        self.assertEqual(4, result["review_rework_count"])
        event = next(
            item for item in reversed(self.service.list_events("task", task["id"]))
            if item["event_type"] == "code_reviewed"
        )
        self.assertTrue(event["payload"]["review_rework_exhausted"])
        self.assertEqual(3, event["payload"]["review_rework_limit"])

    def test_result_partition_is_strict(self):
        task = self.task("strict")
        self.deliver(task)
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        with self.assertRaises(ValueError):
            self.service.review_code(
                task["id"], review["run"]["id"], "pass",
                passed_items=["project-rules"],
                failed_criteria=["project-rules"],
            )

    def test_depends_on_waits_and_continues_from_resumes(self):
        first = self.task("first")
        second = self.task("second")
        self.service.add_relation(second["id"], first["id"], "depends_on")
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET auto_dispatch=0 WHERE id=?", (first["id"],))
        self.assertIsNone(self.service.claim_next_task("worker"))
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET auto_dispatch=1 WHERE id=?", (first["id"],))
        self.deliver(first, "first-thread")
        first_review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(first["id"], "code_review", "first-review", first_review["run"]["id"])
        self.service.review_code(
            first["id"], first_review["run"]["id"], "pass",
            passed_items=["project-rules"],
        )
        claim = self.service.claim_next_task("worker")
        self.assertEqual("", claim["resume_thread_id"])
        self.service.interrupt_unsubmitted_run(claim["run"]["id"], "test cleanup")
        self.service.transition_task(second["id"], "cancelled")

        third = self.task("third")
        self.service.add_relation(third["id"], first["id"], "continues_from")
        claim = self.service.claim_next_task("worker")
        self.assertEqual("first-thread", claim["resume_thread_id"])

        self.service.bind_conversation(third["id"], "execution", "first-thread", claim["run"]["id"])
        path = self.project / "src" / "APage.tsx"
        path.write_text(path.read_text(encoding="utf-8") + "// continued task\n", encoding="utf-8")
        self.service.submit_delivery(
            claim["run"]["id"], "在任务一基础上继续开发", "测试通过",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": "功能可用", "evidence": "测试通过"}],
        )
        third_review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(third["id"], "code_review", "third-review", third_review["run"]["id"])
        completed = self.service.review_code(
            third["id"], third_review["run"]["id"], "pass",
            passed_items=["project-rules"],
        )
        self.assertEqual("done", completed["status"])

    def test_high_confidence_relation_evidence_creates_dependency(self):
        first = self.task("relation-provider", auto_dispatch=False)
        second = self.task(
            "relation-consumer",
            dependency_analysis={
                "decision": "independent",
                "relation_evidence": [{
                    "task_id": first["id"],
                    "relation_type": "depends_on",
                    "confidence": "high",
                    "kind": "artifact_dependency",
                    "reason": "消费前置任务新增的 APage 接口",
                    "source": "codegraph_explore",
                    "files": ["src/APage.tsx"],
                    "symbols": ["APage"],
                }],
            },
        )
        analysis = second["dependency_analysis"]
        self.assertEqual("depends_on", analysis["decision"])
        self.assertEqual([first["id"]], analysis["depends_tasks"])
        relation = next(
            item for item in self.service.task_relations(second["id"])
            if item["relation_type"] == "depends_on"
        )
        self.assertEqual("消费前置任务新增的 APage 接口", relation["description"])

    def test_execution_failure_auto_requeues_and_resumes_thread(self):
        task = self.task("execution-failure")
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], "execution", "failed-dev-thread", claim["run"]["id"])
        failed = self.service.interrupt_unsubmitted_run(claim["run"]["id"], "开发进程异常退出")
        self.assertEqual("ready", failed["task"]["status"])
        self.assertEqual("interrupted", failed["run"]["status"])
        self.assertTrue(failed["task"]["retry_required"])
        self.assertEqual(1, failed["task"]["execution_recovery_count"])
        retry = self.service.claim_next_task("other-worker")
        self.assertEqual("execution", retry["run"]["run_type"])
        self.assertEqual("failed-dev-thread", retry["resume_thread_id"])

    def test_scheduler_cycle_respects_pause_and_acknowledges_generation(self):
        task = self.task("scheduler-cycle")
        self.service.set_dispatcher_enabled(False)
        paused = self.service.claim_schedule_cycle(
            "codex-native-controller", force=True
        )
        self.assertEqual("paused", paused["status"])

        self.service.set_dispatcher_enabled(True)
        cycle = self.service.claim_schedule_cycle(
            "codex-native-controller", force=False
        )
        self.assertEqual("claimed", cycle["status"])
        busy = self.service.claim_schedule_cycle(
            "codex-native-controller", force=True
        )
        self.assertEqual("busy", busy["status"])
        dispatches = cycle["development"]["dispatches"]
        self.assertEqual([task["id"]], [item["entity_id"] for item in dispatches])
        with self.assertRaisesRegex(ValueError, "generation is invalid"):
            self.service.complete_schedule_cycle(
                "codex-native-controller", cycle["cycle_generation"] + 100
            )
        completed = self.service.complete_schedule_cycle(
            "codex-native-controller", cycle["cycle_generation"]
        )
        self.assertFalse(completed["pending"])

    def test_attention_transition_creates_a_durable_scheduler_wakeup(self):
        task = self.task("attention-wakeup")
        cycle = self.service.claim_schedule_cycle(
            "codex-native-controller", force=True
        )
        dispatch = cycle["development"]["dispatches"][0]
        self.service.bind_native_dispatch(
            dispatch["run_id"], "attention-dev-thread",
            dispatch_attempt_id=dispatch["dispatch_attempt_id"],
        )
        self.service.complete_schedule_cycle(
            "codex-native-controller", cycle["cycle_generation"]
        )
        self.service.report_run_blocked(
            task["id"], dispatch["run_id"], "blocked", "等待外部权限"
        )
        snapshot = self.service.scheduler_snapshot()
        self.assertTrue(snapshot["pending"])
        self.assertIn(
            snapshot["last_trigger"], {"task_state_changed", "run_state_changed"},
        )

    def test_safe_execution_block_gets_one_bounded_repair(self):
        task = self.task("safe-block-repair")
        first = self.service.claim_next_task("worker")
        self.service.bind_conversation(
            task["id"], "execution", "development-thread", first["run"]["id"],
        )
        failure_location = {
            "file": "tests/test_a_page.py",
            "symbols": [],
        }

        repaired = self.service.report_run_blocked(
            task["id"], first["run"]["id"], "blocked", "缺少测试用例",
            "project", [failure_location],
        )

        self.assertEqual("rework", repaired["status"])
        self.assertTrue(repaired["auto_dispatch"])
        self.assertEqual(
            {"attempt": 1, "attempt_limit": 1, "scheduled": True},
            {
                key: repaired["implementation_contract"]["self_heal"][key]
                for key in ("attempt", "attempt_limit", "scheduled")
            },
        )
        retry = self.service.claim_next_task("worker")
        self.assertEqual("development-thread", retry["resume_thread_id"])
        self.service.bind_conversation(
            task["id"], "rework", "development-thread", retry["run"]["id"],
        )

        exhausted = self.service.report_run_blocked(
            task["id"], retry["run"]["id"], "blocked", "仍然缺少测试用例",
            "project", [failure_location],
        )

        self.assertEqual("blocked", exhausted["status"])

    def test_failure_advice_cannot_trigger_repair_or_expand_targets(self):
        task = self.task("advisory-block")
        first = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], "execution", "advice-thread", first["run"]["id"])
        advice = {"category": "environment", "advisory_only": True}
        def classify_after_transition(reason):
            self.assertEqual("blocked", self.service.get_task(task["id"])["status"])
            self.assertIsNone(self.service.get_task(task["id"])["active_run_id"])
            return advice
        with patch.object(self.service.decisions, "classify_failure", side_effect=classify_after_transition):
            result = self.service.report_run_blocked(
                task["id"], first["run"]["id"], "blocked", "未知运行错误",
                failure_locations=[{"file": "new.py", "symbols": []}],
            )
        self.assertEqual("blocked", result["status"])
        self.assertEqual(task["implementation_contract"], result["implementation_contract"])
        with self.service.db.connection() as connection:
            event = connection.execute("SELECT payload FROM events WHERE event_type='failure_decision_advisory'").fetchone()
        self.assertIsNotNone(event)

    def test_optional_decision_audit_error_does_not_fail_completed_transition(self):
        task = self.task("advice-audit-error")
        first = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], "execution", "audit-error-thread", first["run"]["id"])
        original = self.service._event
        def fail_only_audit(connection, entity_type, entity_id, event_type, payload):
            if event_type == "failure_decision_advisory":
                raise sqlite3.OperationalError("database is locked")
            return original(connection, entity_type, entity_id, event_type, payload)
        with patch.object(self.service.decisions, "classify_failure", return_value={"category": "environment"}), \
             patch.object(self.service, "_event", side_effect=fail_only_audit), \
             self.assertLogs("core.service.execution", level="WARNING"):
            result = self.service.report_run_blocked(task["id"], first["run"]["id"], "blocked", "未知错误")
        self.assertEqual("blocked", result["status"])
        self.assertEqual("blocked", self.service.get_task(task["id"])["status"])

    def test_pending_native_creation_keeps_scheduler_wakeup_unacknowledged(self):
        task = self.task("pending-native-creation")
        cycle = self.service.claim_schedule_cycle(
            "codex-native-controller", force=True,
        )
        dispatch = cycle["development"]["dispatches"][0]
        self.service.mark_native_dispatch_pending(
            dispatch["run_id"], "pending-client-thread",
            dispatch_attempt_id=dispatch["dispatch_attempt_id"],
        )

        completed = self.service.complete_schedule_cycle(
            "codex-native-controller", cycle["cycle_generation"],
        )
        self.assertTrue(completed["pending"])
        resumed = self.service.claim_schedule_cycle(
            "codex-native-controller", force=False,
        )
        pending = resumed["development"]["dispatches"]
        self.assertEqual([task["id"]], [item["entity_id"] for item in pending])
        self.assertEqual("pending_thread", pending[0]["status"])

    def test_false_high_confidence_relation_evidence_is_rejected(self):
        first = self.task("relation-evidence-provider", auto_dispatch=False)
        with self.assertRaisesRegex(
            ValueError, "does not match the connected location result",
        ):
            self.task(
                "relation-evidence-consumer",
                dependency_analysis={
                    "decision": "independent",
                    "relation_evidence": [{
                        "task_id": first["id"],
                        "relation_type": "depends_on",
                        "confidence": "high",
                        "kind": "artifact_dependency",
                        "reason": "伪造的不相干调用关系",
                        "source": "codegraph_explore",
                        "files": ["src/Unrelated.ts"],
                        "symbols": ["Unrelated"],
                    }],
                },
            )

    def test_expired_execution_auto_requeues_and_resumes_thread(self):
        task = self.task("execution-timeout")
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], "execution", "timeout-dev-thread", claim["run"]["id"])
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE task_runs SET lease_expires_at=datetime('now','-1 second') WHERE id=?",
                (claim["run"]["id"],),
            )
        self.assertEqual(1, self.service.recover_expired_runs())
        expired = self.service.get_task(task["id"])
        self.assertEqual("ready", expired["status"])
        self.assertTrue(expired["retry_required"])
        self.assertEqual("expired", self.service.get_run(claim["run"]["id"])["status"])
        retry = self.service.claim_next_task("other-worker")
        self.assertEqual("timeout-dev-thread", retry["resume_thread_id"])

    def test_execution_recovery_exhaustion_moves_to_attention(self):
        task = self.task("execution-recovery-limit")
        for attempt in range(1, 4):
            claim = self.service.claim_next_task(f"worker-{attempt}")
            self.service.bind_conversation(
                task["id"], "execution", "recovery-dev-thread", claim["run"]["id"]
            )
            result = self.service.interrupt_unsubmitted_run(
                claim["run"]["id"], f"开发进程第 {attempt} 次异常退出"
            )
            if attempt < 3:
                self.assertEqual("ready", result["task"]["status"])
            else:
                self.assertEqual("failed", result["task"]["status"])
                self.assertFalse(result["task"]["auto_dispatch"])
        self.assertIsNone(self.service.claim_next_task("worker-4"))

    def test_generic_transition_and_done_gate_are_blocked(self):
        task = self.task("gates")
        with self.assertRaises(ValueError):
            self.service.transition_task(task["id"], "done")
        with self.assertRaises(Exception):
            with self.service.db.transaction() as connection:
                connection.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))

    def test_bug_code_quality_review_failure_reworks_same_bug(self):
        bug = self.task("nested-bug", type="bug")
        self.deliver(bug, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(bug["id"], "code_review", "bug-review", review["run"]["id"])
        self.service.review_code(
            bug["id"], review["run"]["id"], "fail", reasons=["仍失败"],
            passed_items=[], failed_criteria=["project-rules"],
        )
        self.assertEqual(1, sum(item["type"] == "bug" for item in self.service.list_tasks()))
        rework = self.service.claim_next_task("worker")
        self.assertEqual("rework", rework["run"]["run_type"])
        self.assertEqual("original-dev", rework["resume_thread_id"])
        self.assertIn("仍失败", rework["dispatch_prompt"])
        self.assertIn("project-rules", rework["dispatch_prompt"])

    def test_contracts_and_dependency_relations_are_complete_and_consistent(self):
        base = {
            "title": "contract-check", "project": str(self.project), "goal": "实现功能", "scope": ["组件"],
            "acceptance_criteria": ["功能可用"], "modules": ["a-page"],
            "dependency_analysis": {"decision": "independent"},
            "implementation_contract": {"targets": [{"file": "src/APage.tsx", "mode": "modify", "symbols": ["APage"], "tasks": [{"symbol": "APage", "action": "修改"}]}]},
            "review_contract": self.review_contract(True),
        }
        analysis = self.analysis("empty-steps")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", location_analysis_id=analysis["id"], implementation_contract={"targets": [{"file": "src/APage.tsx", "mode": "modify", "symbols": ["APage"], "tasks": []}]}))

        analysis = self.analysis("mismatched-target")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", title="mismatched-target", location_analysis_id=analysis["id"], implementation_contract={
                "targets": [{"file": "src/Other.tsx", "mode": "modify", "symbols": ["Other"], "tasks": [{"symbol": "Other", "action": "修改"}]}],
            }))

        analysis = self.analysis("empty-checks")
        defaulted = self.service.create_task(dict(
            base,
            status="ready",
            title="empty-checks",
            location_analysis_id=analysis["id"],
            review_contract={
                "checks": [],
                "quality_gates": self.review_contract(True)["quality_gates"],
            },
        ))
        self.assertEqual(
            ["code-quality", "security-vulnerabilities", "cohesion-coupling"],
            [item["id"] for item in defaulted["review_contract"]["checks"]],
        )

        prerequisite = self.task("prerequisite")
        analysis = self.analysis("dependency-mismatch")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", title="dependency-mismatch", location_analysis_id=analysis["id"],
                dependency_analysis={"decision": "depends_on", "depends_tasks": ["TASK-9999"]},
                relations=[{"relation_type": "depends_on", "target_task_id": prerequisite["id"]}],
                ))

        analysis = self.analysis("independent-relation")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", title="independent-relation", location_analysis_id=analysis["id"],
                relations=[{"relation_type": "depends_on", "target_task_id": prerequisite["id"]}],
                ))

    def test_expired_code_review_keeps_status_and_recovers(self):
        task = self.task("expired-review")
        self.deliver(task, "dev-thread")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "expired-code-review", review["run"]["id"])
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE task_runs SET lease_expires_at=datetime('now','-1 second') WHERE id=?", (review["run"]["id"],))
        self.assertEqual(1, self.service.recover_expired_runs())
        expired = self.service.get_task(task["id"])
        self.assertEqual("code_review", expired["status"])
        self.assertIsNone(expired["active_run_id"])
        self.assertEqual(0, expired["retry_required"])
        self.assertEqual("expired", self.service.get_run(review["run"]["id"])["status"])
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET review_retry_after=NULL WHERE id=?", (task["id"],))
        resumed_review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "expired-code-review", resumed_review["run"]["id"])
        completed = self.service.review_code(
            task["id"], resumed_review["run"]["id"], "pass",
            passed_items=["project-rules"],
        )
        self.assertEqual("done", completed["status"])

    def test_code_review_prompt_excludes_task_goal_and_acceptance(self):
        task = self.task("natural-review-prompt")
        self.deliver(task, "dev-thread")

        review = self.service.claim_next_code_review_task("reviewer")

        prompt = review["dispatch_prompt"]
        self.assertTrue(prompt.startswith("请审查以下代码变更：\n\n"))
        self.assertIn(f"标题：{task['title']}", prompt)
        self.assertNotIn(f"目标：{task['goal']}", prompt)
        self.assertNotIn("验收标准：", prompt)
        self.assertNotIn("run_acceptance_checks", prompt)
        self.assertIn("代码质量、安全漏洞以及高内聚低耦合", prompt)
        self.assertIn("Diff 必须且只能通过现成 Git 命令获取", prompt)
        self.assertIn("git -C <workspace_path> diff --no-ext-diff", prompt)
        self.assertIn("git -C <workspace_path> diff --no-index", prompt)
        self.assertIn("不得读取交付补丁文件", prompt)
        self.assertIn("与审查项相关的所有现成工具", prompt)
        self.assertIn("不得安装新工具、编写临时脚本或自制扫描器", prompt)
        self.assertIn("变更符号的直接依赖、调用方", prompt)
        self.assertIn("不得仅因任务目标看起来未完成而判定失败", prompt)
        self.assertIn("纯格式、命名偏好或非阻断建议不得放入 failed_criteria", prompt)
        self.assertNotIn("只要本阶段代码质量检查全部通过，也必须给出 pass", prompt)
        review_context = model_run_context(review["run"]["context_snapshot"])
        self.assertEqual(
            {"base_revision", "changed_files", "workspace_path"},
            set(review_context["diff_scope"]),
        )
        self.assertNotIn("artifact_path", review_context["diff_scope"])
        self.assertNotIn("artifact_sha256", review_context["diff_scope"])
        self.assertNotIn("$dotasks-lifecycle", prompt)
        self.assertNotIn("dotasks:dotasks-lifecycle", prompt)
        self.assertLess(prompt.index("请审查以下代码变更："), prompt.index("RUN_CONTEXT_JSON="))

    def test_missing_review_callback_retries_immediately_and_uses_latest_thread(self):
        task = self.task("missing-review-callback")
        self.deliver(task, "dev-thread")

        first = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="code_review"
        )
        self.service.bind_native_dispatch(
            first["run_id"], "review-thread-1",
            dispatch_attempt_id=first["dispatch_attempt_id"],
        )
        self.service.fail_native_dispatch(
            first["run_id"], "原生 Codex 任务已结束但未提交生命周期回调"
        )

        interrupted = self.service.get_task(task["id"])
        self.assertIsNone(interrupted["review_retry_after"])
        second = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="code_review"
        )
        self.assertEqual("review-thread-1", second["resume_thread_id"])
        self.service.bind_native_dispatch(
            second["run_id"], "review-thread-2",
            resume_fallback_reason="review-thread-1 archived",
            dispatch_attempt_id=second["dispatch_attempt_id"],
        )
        self.service.fail_native_dispatch(
            second["run_id"], "原生 Codex 任务已结束但未提交生命周期回调"
        )

        third = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="code_review"
        )
        self.assertEqual("review-thread-2", third["resume_thread_id"])
        self.service.bind_native_dispatch(
            third["run_id"], "review-thread-3",
            dispatch_attempt_id=third["dispatch_attempt_id"],
        )
        self.service.fail_native_dispatch(
            third["run_id"], "原生 Codex 任务已结束但未提交生命周期回调"
        )
        exhausted = self.service.get_task(task["id"])
        self.assertEqual(3, exhausted["review_interrupt_count"])
        self.assertFalse(exhausted["auto_dispatch"])
        self.assertIsNone(self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="code_review"
        ))

    def test_pause_resume_preserves_code_review_stage(self):
        task = self.task("pause-code-review")
        self.deliver(task, "dev-thread")
        self.service.pause_all_tasks("pause review")
        paused = self.service.get_task(task["id"])
        self.assertEqual("paused", paused["status"])
        self.assertEqual("code_review", paused["paused_from_status"])
        resumed_result = self.service.resume_all_tasks()
        self.assertEqual(1, resumed_result["resumed_tasks"])
        resumed = self.service.get_task(task["id"])
        self.assertEqual("code_review", resumed["status"])
        self.assertEqual(0, resumed["retry_required"])
        self.service.set_dispatcher_enabled(True)
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        completed = self.service.review_code(
            task["id"], review["run"]["id"], "pass",
            passed_items=["project-rules"],
        )
        self.assertEqual("done", completed["status"])

        blocked_review = self.task("blocked-code-review")
        self.deliver(blocked_review, "blocked-dev")
        self.service.transition_task(blocked_review["id"], "blocked", "review blocked")
        self.assertEqual("code_review", self.service.transition_task(blocked_review["id"], "code_review")["status"])


if __name__ == "__main__":
    unittest.main()
