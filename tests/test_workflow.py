from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
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

    def deliver(self, task, thread="dev", evidence_status=None):
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], claim["run"]["run_type"], thread, claim["run"]["id"])
        path = self.project / "src" / "APage.tsx"
        path.write_text(path.read_text(encoding="utf-8") + "// delivery\n", encoding="utf-8")
        evidence = {"criterion": "功能可用", "evidence": "测试通过"}
        if evidence_status:
            evidence["status"] = evidence_status
        return self.service.submit_delivery(claim["run"]["id"], "完成", "测试通过", [{"file": "src/APage.tsx", "symbols": ["APage"]}], [evidence])

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
        self.deliver(first, "multi-first-thread", evidence_status="passed")
        self.assertIsNone(self.service.claim_next_task("worker"))
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET auto_dispatch=1 WHERE id=?", (second["id"],))
        self.deliver(second, "multi-second-thread", evidence_status="passed")
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

        delivered = self.deliver(task, evidence_status="passed")

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
            self.deliver(task)

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

    def test_combined_code_review_self_heals_environment_before_verdict(self):
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

        checks = self.service.run_acceptance_checks(
            task["id"], review["run"]["id"]
        )
        result = self.service.review_code(
            task["id"],
            review["run"]["id"],
            "pass",
            passed_items=["project-rules", "功能可用"],
        )

        self.assertEqual((1, 1), (
            checks["self_heal_attempts"], checks["self_healed"]
        ))
        self.assertEqual("healed", checks["checks"][0]["repair_status"])
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
            passed_items=["功能可用"],
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
                reasons=["缺少测试用例"], passed_items=["功能可用"],
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

    def test_combined_review_failure_reworks_without_creating_acceptance_bug(self):
        task = self.task("combined-review-fail")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        result = self.service.review_code(
            task["id"], review["run"]["id"], "fail", reasons=["按钮不可用"],
            passed_items=["project-rules"], failed_criteria=["功能可用"],
        )
        self.assertEqual("rework", result["status"])
        self.assertFalse(any(item["type"] == "bug" for item in self.service.list_tasks()))
        retry = self.service.claim_next_task("worker")
        self.assertEqual("original-dev", retry["resume_thread_id"])

    def test_result_partition_is_strict(self):
        task = self.task("strict")
        self.deliver(task)
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        with self.assertRaises(ValueError):
            self.service.review_code(
                task["id"], review["run"]["id"], "pass",
                passed_items=["project-rules", "功能可用"],
                failed_criteria=["功能可用"],
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
            passed_items=["project-rules", "功能可用"],
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
            passed_items=["project-rules", "功能可用"],
        )
        self.assertEqual("done", completed["status"])

    def test_execution_failure_requires_explicit_retry_and_resumes_thread(self):
        task = self.task("execution-failure")
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], "execution", "failed-dev-thread", claim["run"]["id"])
        failed = self.service.interrupt_unsubmitted_run(claim["run"]["id"], "开发进程异常退出")
        self.assertEqual("failed", failed["task"]["status"])
        self.assertEqual("interrupted", failed["run"]["status"])
        self.assertTrue(failed["task"]["retry_required"])
        self.assertIsNone(self.service.claim_next_task("other-worker"))
        self.service.transition_task(task["id"], "ready")
        retry = self.service.claim_next_task("worker")
        self.assertEqual("execution", retry["run"]["run_type"])
        self.assertEqual("failed-dev-thread", retry["resume_thread_id"])

    def test_expired_execution_is_failed_and_waits_for_explicit_retry(self):
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
        self.assertEqual("failed", expired["status"])
        self.assertTrue(expired["retry_required"])
        self.assertEqual("expired", self.service.get_run(claim["run"]["id"])["status"])
        self.assertIsNone(self.service.claim_next_task("other-worker"))

    def test_generic_transition_and_done_gate_are_blocked(self):
        task = self.task("gates")
        with self.assertRaises(ValueError):
            self.service.transition_task(task["id"], "done")
        with self.assertRaises(Exception):
            with self.service.db.transaction() as connection:
                connection.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))

    def test_bug_combined_review_failure_reworks_same_bug(self):
        bug = self.task("nested-bug", type="bug")
        self.deliver(bug, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(bug["id"], "code_review", "bug-review", review["run"]["id"])
        self.service.review_code(
            bug["id"], review["run"]["id"], "fail", reasons=["仍失败"],
            passed_items=["project-rules"], failed_criteria=["功能可用"],
        )
        self.assertEqual(1, sum(item["type"] == "bug" for item in self.service.list_tasks()))
        rework = self.service.claim_next_task("worker")
        self.assertEqual("rework", rework["run"]["run_type"])
        self.assertEqual("original-dev", rework["resume_thread_id"])
        self.assertIn("仍失败", rework["dispatch_prompt"])
        self.assertIn("功能可用", rework["dispatch_prompt"])

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
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", title="empty-checks", location_analysis_id=analysis["id"], review_contract={"checks": [], "quality_gates": self.review_contract(True)["quality_gates"]}))

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
            passed_items=["project-rules", "功能可用"],
        )
        self.assertEqual("done", completed["status"])

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
            passed_items=["project-rules", "功能可用"],
        )
        self.assertEqual("done", completed["status"])

        blocked_review = self.task("blocked-code-review")
        self.deliver(blocked_review, "blocked-dev")
        self.service.transition_task(blocked_review["id"], "blocked", "review blocked")
        self.assertEqual("code_review", self.service.transition_task(blocked_review["id"], "code_review")["status"])


if __name__ == "__main__":
    unittest.main()
