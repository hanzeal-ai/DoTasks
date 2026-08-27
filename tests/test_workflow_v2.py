from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from core.service import TaskboardService


class WorkflowV2Test(unittest.TestCase):
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
        self.old_vault = os.environ.get("CODEX_TASKBOARD_OBSIDIAN_VAULT")
        os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = str(Path(self.temp.name) / "vault")
        self.service = TaskboardService(self.temp.name)
        self.service.set_dispatcher_enabled(True)

    def tearDown(self):
        if self.old_vault is None:
            os.environ.pop("CODEX_TASKBOARD_OBSIDIAN_VAULT", None)
        else:
            os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = self.old_vault
        self.temp.cleanup()

    def analysis(self, title="v2"):
        project = str(self.project)
        self.service.report_location_status(project, True, "connected", "ok", {
            "tool": "codegraph_explore", "files": ["src/APage.tsx"], "symbols": ["APage"],
        })
        analysis = self.service.prepare_location_analysis({"title": title, "goal": "实现功能", "project": project, "modules": ["a-page"]})
        return self.service.complete_location_analysis(analysis["analysis_id"], {"tool": "codegraph_explore", "files": ["src/APage.tsx"]}, [{"file": "src/APage.tsx", "symbols": ["APage"]}], [{"criterion": "功能可用", "file": "src/APage.tsx", "symbol": "APage", "method": "测试", "expected": "通过"}])

    def task(self, title="v2", **overrides):
        project = str(self.project)
        analysis = self.analysis(title)
        payload = {
            "title": title, "project": project, "goal": "实现功能", "scope": ["组件"], "out_of_scope": [],
            "acceptance_criteria": ["功能可用"], "modules": ["a-page"], "location_analysis_id": analysis["id"],
            "dependency_analysis": {"decision": "independent"},
            "implementation_contract": {"targets": [{"file": "src/APage.tsx", "symbols": ["APage"]}], "ordered_steps": [{"action": "修改组件", "file": "src/APage.tsx", "symbol": "APage"}]},
            "review_contract": {"checks": ["遵守项目规范"]},
        }
        payload.update(overrides)
        return self.service.create_task({**payload, "status": "ready", "workflow_version": 2})

    def deliver(self, task, thread="dev"):
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(task["id"], claim["run"]["run_type"], thread, claim["run"]["id"])
        path = self.project / "src" / "APage.tsx"
        path.write_text(path.read_text(encoding="utf-8") + "// delivery\n", encoding="utf-8")
        return self.service.submit_delivery(claim["run"]["id"], "完成", "测试通过", [{"file": "src/APage.tsx", "symbols": ["APage"]}], [{"criterion": "功能可用", "evidence": "测试通过"}])

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
            "analysis_id": prepared["analysis_id"],
            "title": "单包创建", "project": str(self.project), "goal": "实现功能",
            "scope": ["组件"], "out_of_scope": [], "modules": ["a-page"],
            "location_evidence": {
                "tool": "codegraph_explore", "query": "APage",
                "files": ["src/APage.tsx"], "symbols": ["APage"],
            },
            "targets": [{"file": "src/APage.tsx", "symbols": ["APage"], "reason": "主组件"}],
            "ordered_steps": [{"file": "src/APage.tsx", "symbol": "APage", "action": "修改组件"}],
            "review_checks": ["遵守项目规范"],
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
            [{"file": "src/APage.tsx", "symbols": ["APage"], "reason": "主组件"}],
            task["implementation_contract"]["targets"],
        )
        self.assertEqual(["遵守项目规范"], task["review_contract"]["checks"])
        self.assertEqual("independent", task["dependency_analysis"]["decision"])
        self.assertNotIn("dependency_candidates", prepared["obsidian"])
        self.assertEqual(1, len(history_queries))

    def test_finalize_task_intake_pauses_for_strong_dependency_candidate(self):
        existing = self.task("重复模块任务")
        prepared = self.service.prepare_location_analysis({
            "title": "重复模块任务", "goal": "实现功能", "project": str(self.project),
            "modules": ["a-page"],
        })
        payload = {
            "analysis_id": prepared["analysis_id"],
            "title": "重复模块任务", "project": str(self.project), "goal": "实现功能",
            "scope": ["组件"], "out_of_scope": [], "modules": ["a-page"],
            "location_evidence": {
                "tool": "codegraph_explore", "query": "APage",
                "files": ["src/APage.tsx"], "symbols": ["APage"],
            },
            "targets": [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            "ordered_steps": [{"file": "src/APage.tsx", "symbol": "APage", "action": "修改组件"}],
            "review_checks": ["遵守项目规范"],
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

    def test_execution_review_acceptance_done(self):
        task = self.task()
        self.assertEqual(2, task["workflow_version"])
        self.deliver(task)
        review = self.service.claim_next_code_review_task("reviewer")
        self.assertIn('["遵守项目规范"]', review["dispatch_prompt"])
        self.assertIn("不要混入 acceptance_criteria", review["dispatch_prompt"])
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        self.service.review_code(task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        result = self.service.accept_task(task["id"], acceptance["run"]["id"], "pass", passed_criteria=["功能可用"], failed_criteria=[])
        self.assertEqual("done", result["status"])
        acceptance_conversation = next(
            item for item in self.service.list_conversations(task["id"])
            if item["role"] == "acceptance"
        )
        self.assertEqual("completed", acceptance_conversation["status"])
        self.assertTrue(any(
            item["event_type"] == "acceptance_completed"
            and item["payload"]["verdict"] == "pass"
            for item in self.service.list_events("task", task["id"])
        ))
        with self.service.db.connection() as connection:
            experience = connection.execute(
                "SELECT id FROM experiences WHERE source_tasks=?",
                ('["' + task["id"] + '"]',),
            ).fetchone()
            synced_entities = {
                (row["entity_type"], row["entity_id"], row["status"])
                for row in connection.execute(
                    "SELECT entity_type, entity_id, status FROM integration_outbox"
                ).fetchall()
            }
        self.assertIsNotNone(experience)
        self.assertIn(("task", task["id"], "completed"), synced_entities)
        self.assertIn(("experience", experience["id"], "completed"), synced_entities)

    def test_review_failure_reuses_development_thread(self):
        task = self.task("review-fail")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        self.service.review_code(task["id"], review["run"]["id"], "fail", reasons=["规范错误"], failed_criteria=["遵守项目规范"])
        retry = self.service.claim_next_task("worker")
        self.assertEqual("original-dev", retry["resume_thread_id"])
        self.assertEqual("rework", retry["run"]["run_type"])
        self.service.bind_conversation(task["id"], "rework", "original-dev", retry["run"]["id"])
        path = self.project / "src" / "APage.tsx"
        path.write_text(path.read_text(encoding="utf-8") + "// review fix\n", encoding="utf-8")
        delivery = self.service.submit_delivery(
            retry["run"]["id"], "修复 Code Review 问题", "测试通过",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": "功能可用", "evidence": "测试通过"}],
        )
        self.assertEqual("code_review", delivery["task"]["status"])
        second_review = self.service.claim_next_code_review_task("reviewer")
        self.assertEqual("review-thread", second_review["resume_thread_id"])
        self.assertNotEqual(review["run"]["id"], second_review["run"]["id"])
        self.service.bind_conversation(task["id"], "code_review", "review-thread", second_review["run"]["id"])
        with self.service.db.connection() as connection:
            review_runs = connection.execute(
                """SELECT runs.id, runs.delivery_run_id, runs.attempt, runs.status, mapping.thread_id
                   FROM task_runs runs
                   JOIN task_run_conversations mapping ON mapping.run_id=runs.id
                   WHERE runs.task_id=? AND runs.run_type='code_review'
                   ORDER BY runs.attempt""",
                (task["id"],),
            ).fetchall()
        self.assertEqual(2, len(review_runs))
        self.assertEqual(2, len({row["id"] for row in review_runs}))
        self.assertEqual(2, len({row["delivery_run_id"] for row in review_runs}))
        self.assertEqual(2, len({row["attempt"] for row in review_runs}))
        self.assertEqual({"review-thread"}, {row["thread_id"] for row in review_runs})
        self.assertEqual("completed", review_runs[0]["status"])
        self.assertEqual("running", review_runs[1]["status"])
        self.service.review_code(task["id"], second_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        reviews = self.service.list_reviews(task["id"])
        self.assertEqual([1, 2], [item["round"] for item in reviews])
        self.assertEqual(["fail", "pass"], [item["verdict"] for item in reviews])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        completed = self.service.accept_task(
            task["id"], acceptance["run"]["id"], "pass",
            passed_criteria=["功能可用"], failed_criteria=[],
        )
        self.assertEqual("done", completed["status"])

    def test_acceptance_failure_creates_bug_and_reuses_thread(self):
        task = self.task("accept-fail")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        self.service.review_code(task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        self.service.accept_task(task["id"], acceptance["run"]["id"], "fail", reasons=["按钮不可用"], passed_criteria=[], failed_criteria=["功能可用"])
        bug = next(item for item in self.service.list_tasks() if item["type"] == "bug")
        self.assertEqual("acceptance_blocked", self.service.get_task(task["id"])["status"])
        acceptance_conversation = next(
            item for item in self.service.list_conversations(task["id"])
            if item["role"] == "acceptance"
        )
        self.assertEqual("completed", acceptance_conversation["status"])
        self.assertTrue(any(
            item["event_type"] == "acceptance_completed"
            and item["payload"]["verdict"] == "fail"
            and item["payload"]["created_bug_task_id"] == bug["id"]
            for item in self.service.list_events("task", task["id"])
        ))
        self.assertTrue(any(
            item["event_type"] == "created_from_acceptance_failure"
            for item in self.service.list_events("task", bug["id"])
        ))
        with self.service.db.connection() as connection:
            synced_tasks = {
                row["entity_id"]
                for row in connection.execute(
                    "SELECT entity_id FROM integration_outbox WHERE entity_type='task' AND status='completed'"
                ).fetchall()
            }
        self.assertIn(task["id"], synced_tasks)
        self.assertIn(bug["id"], synced_tasks)
        self.assertEqual("original-dev", bug["codex_thread_id"])
        self.assertTrue(any(r["relation_type"] == "defect_of" for r in self.service.task_relations(bug["id"])))
        bug_delivery = self.deliver(bug, "original-dev")
        self.assertEqual("code_review", bug_delivery["task"]["status"])
        bug_review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(bug["id"], "code_review", "bug-review", bug_review["run"]["id"])
        self.service.review_code(bug["id"], bug_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        bug_acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(bug["id"], "acceptance", "bug-acceptance", bug_acceptance["run"]["id"])
        self.assertEqual("done", self.service.accept_task(bug["id"], bug_acceptance["run"]["id"], "pass", passed_criteria=["功能可用"], failed_criteria=[])["status"])
        original_acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "original-acceptance", original_acceptance["run"]["id"])
        self.assertEqual("done", self.service.accept_task(task["id"], original_acceptance["run"]["id"], "pass", passed_criteria=["功能可用"], failed_criteria=[])["status"])

    def test_result_partition_is_strict(self):
        task = self.task("strict")
        self.deliver(task)
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        self.service.review_code(task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        with self.assertRaises(ValueError):
            self.service.accept_task(task["id"], acceptance["run"]["id"], "pass", passed_criteria=["功能可用"], failed_criteria=["功能可用"])

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
        self.service.review_code(first["id"], first_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        first_acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(first["id"], "acceptance", "first-acceptance", first_acceptance["run"]["id"])
        self.service.accept_task(first["id"], first_acceptance["run"]["id"], "pass", passed_criteria=["功能可用"], failed_criteria=[])
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
        self.service.review_code(third["id"], third_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        third_acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(third["id"], "acceptance", "third-acceptance", third_acceptance["run"]["id"])
        completed = self.service.accept_task(
            third["id"], third_acceptance["run"]["id"], "pass",
            passed_criteria=["功能可用"], failed_criteria=[],
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

    def test_generic_transition_and_v2_done_gate_are_blocked(self):
        task = self.task("gates")
        with self.assertRaises(ValueError):
            self.service.transition_task(task["id"], "done")
        with self.assertRaises(Exception):
            with self.service.db.transaction() as connection:
                connection.execute("UPDATE tasks SET status='done' WHERE id=?", (task["id"],))

    def test_bug_acceptance_failure_does_not_create_nested_bug(self):
        task = self.task("nested-bug")
        self.deliver(task, "original-dev")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        self.service.review_code(task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        self.service.accept_task(task["id"], acceptance["run"]["id"], "fail", reasons=["bad"], passed_criteria=[], failed_criteria=["功能可用"])
        bug = next(item for item in self.service.list_tasks() if item["type"] == "bug")
        self.deliver(bug, "original-dev")
        bug_review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(bug["id"], "code_review", "bug-review", bug_review["run"]["id"])
        self.service.review_code(bug["id"], bug_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        bug_acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(bug["id"], "acceptance", "bug-acceptance", bug_acceptance["run"]["id"])
        self.service.accept_task(bug["id"], bug_acceptance["run"]["id"], "fail", reasons=["仍失败"], passed_criteria=[], failed_criteria=["功能可用"])
        self.assertEqual(1, sum(item["type"] == "bug" for item in self.service.list_tasks()))
        rework = self.service.claim_next_task("worker")
        self.assertEqual("rework", rework["run"]["run_type"])
        self.assertEqual("original-dev", rework["resume_thread_id"])
        self.assertIn("仍失败", rework["dispatch_prompt"])
        self.assertIn("功能可用", rework["dispatch_prompt"])

    def test_v2_contracts_and_dependency_relations_are_complete_and_consistent(self):
        base = {
            "title": "contract-check", "project": str(self.project), "goal": "实现功能", "scope": ["组件"],
            "acceptance_criteria": ["功能可用"], "modules": ["a-page"],
            "dependency_analysis": {"decision": "independent"},
            "implementation_contract": {"targets": [{"file": "src/APage.tsx", "symbols": ["APage"]}], "ordered_steps": []},
            "review_contract": {"checks": ["遵守项目规范"]},
        }
        analysis = self.analysis("empty-steps")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", workflow_version=2, location_analysis_id=analysis["id"]))

        analysis = self.analysis("mismatched-target")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", workflow_version=2, title="mismatched-target", location_analysis_id=analysis["id"], implementation_contract={
                "targets": [{"file": "src/Other.tsx", "symbols": ["Other"]}],
                "ordered_steps": [{"file": "src/Other.tsx", "symbol": "Other", "action": "修改"}],
            }))

        analysis = self.analysis("empty-checks")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", workflow_version=2, title="empty-checks", location_analysis_id=analysis["id"], implementation_contract={
                "targets": [{"file": "src/APage.tsx", "symbols": ["APage"]}],
                "ordered_steps": [{"file": "src/APage.tsx", "symbol": "APage", "action": "修改"}],
            }, review_contract={"checks": []}))

        prerequisite = self.task("prerequisite")
        analysis = self.analysis("dependency-mismatch")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", workflow_version=2, title="dependency-mismatch", location_analysis_id=analysis["id"],
                dependency_analysis={"decision": "depends_on", "target_task_id": "TASK-9999"},
                relations=[{"relation_type": "depends_on", "target_task_id": prerequisite["id"]}],
                implementation_contract={"targets": [{"file": "src/APage.tsx", "symbols": ["APage"]}],
                                         "ordered_steps": [{"file": "src/APage.tsx", "symbol": "APage", "action": "修改"}]}))

        analysis = self.analysis("independent-relation")
        with self.assertRaises(ValueError):
            self.service.create_task(dict(base, status="ready", workflow_version=2, title="independent-relation", location_analysis_id=analysis["id"],
                relations=[{"relation_type": "depends_on", "target_task_id": prerequisite["id"]}],
                implementation_contract={"targets": [{"file": "src/APage.tsx", "symbols": ["APage"]}],
                                         "ordered_steps": [{"file": "src/APage.tsx", "symbol": "APage", "action": "修改"}]}))

    def test_code_review_and_acceptance_interruptions_resume_independent_threads(self):
        task = self.task("interrupt-review")
        self.deliver(task, "dev-thread")
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "code-review-thread", review["run"]["id"])
        interrupted = self.service.interrupt_unsubmitted_run(review["run"]["id"], "reviewer stopped")
        self.assertEqual("interrupted", interrupted["run"]["status"])
        self.assertEqual("code_review", interrupted["task"]["status"])
        self.assertIsNone(interrupted["task"]["active_run_id"])
        self.assertEqual(0, interrupted["task"]["retry_required"])
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET review_retry_after=NULL WHERE id=?", (task["id"],))
        resumed_review = self.service.claim_next_code_review_task("reviewer")
        self.assertEqual("code-review-thread", resumed_review["resume_thread_id"])
        self.service.bind_conversation(task["id"], "code_review", "code-review-thread", resumed_review["run"]["id"])
        self.service.review_code(task["id"], resumed_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        interrupted = self.service.interrupt_unsubmitted_run(acceptance["run"]["id"], "acceptance stopped")
        self.assertEqual("interrupted", interrupted["run"]["status"])
        self.assertEqual("acceptance", interrupted["task"]["status"])
        self.assertIsNone(interrupted["task"]["active_run_id"])
        self.assertEqual(0, interrupted["task"]["retry_required"])
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET review_retry_after=NULL WHERE id=?", (task["id"],))
        resumed_acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.assertEqual("acceptance-thread", resumed_acceptance["resume_thread_id"])

    def test_expired_review_stages_keep_status_and_recover(self):
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
        self.service.review_code(task["id"], resumed_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "expired-acceptance", acceptance["run"]["id"])
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE task_runs SET lease_expires_at=datetime('now','-1 second') WHERE id=?", (acceptance["run"]["id"],))
        self.assertEqual(1, self.service.recover_expired_runs())
        expired_acceptance = self.service.get_task(task["id"])
        self.assertEqual("acceptance", expired_acceptance["status"])
        self.assertIsNone(expired_acceptance["active_run_id"])
        self.assertEqual(0, expired_acceptance["retry_required"])
        self.assertEqual("expired", self.service.get_run(acceptance["run"]["id"])["status"])

    def test_three_review_interruptions_disable_dispatch_without_failure(self):
        task = self.task("three-review-interruptions")
        self.deliver(task, "dev-thread")
        for index in range(3):
            claim = self.service.claim_next_code_review_task("reviewer")
            self.service.bind_conversation(task["id"], "code_review", f"review-thread-{index}", claim["run"]["id"])
            result = self.service.interrupt_unsubmitted_run(claim["run"]["id"], f"stop {index}")
            if index < 2:
                with self.service.db.transaction() as connection:
                    connection.execute("UPDATE tasks SET review_retry_after=NULL WHERE id=?", (task["id"],))
            else:
                self.assertEqual(0, result["task"]["auto_dispatch"])
        final = self.service.get_task(task["id"])
        self.assertEqual("code_review", final["status"])
        self.assertEqual(0, final["retry_required"])
        with self.assertRaises(ValueError):
            self.service.transition_task(task["id"], "code_review", token_used=1)
        restored = self.service.transition_task(task["id"], "code_review", auto_dispatch=True)
        self.assertEqual(1, restored["auto_dispatch"])
        self.assertEqual(0, restored["review_interrupt_count"])
        self.assertIsNone(restored["review_retry_after"])
        resumed_review = self.service.claim_next_code_review_task("reviewer")
        self.assertIsNotNone(resumed_review)
        self.service.bind_conversation(task["id"], "code_review", "review-thread-restored", resumed_review["run"]["id"])
        self.service.review_code(task["id"], resumed_review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        for index in range(3):
            result = self.service.interrupt_unsubmitted_run(acceptance["run"]["id"], f"acceptance stop {index}")
            if index < 2:
                with self.service.db.transaction() as connection:
                    connection.execute("UPDATE tasks SET review_retry_after=NULL WHERE id=?", (task["id"],))
                acceptance = self.service.claim_next_acceptance_task("acceptance")
                self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
            else:
                self.assertEqual(0, result["task"]["auto_dispatch"])
        restored = self.service.transition_task(task["id"], "acceptance", auto_dispatch=True)
        self.assertEqual(1, restored["auto_dispatch"])
        self.assertEqual(0, restored["review_interrupt_count"])
        self.assertIsNone(restored["review_retry_after"])
        self.assertIsNotNone(self.service.claim_next_acceptance_task("acceptance"))
        ready_task = self.task("direct-stage-gate")
        with self.assertRaises(ValueError):
            self.service.transition_task(ready_task["id"], "code_review")

    def test_pause_resume_preserves_v2_review_and_acceptance_stages(self):
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
        self.service.review_code(task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"])
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.service.bind_conversation(task["id"], "acceptance", "acceptance-thread", acceptance["run"]["id"])
        self.service.transition_task(task["id"], "blocked", "pause acceptance")
        blocked = self.service.get_task(task["id"])
        self.assertEqual("acceptance", blocked["blocked_from_status"])
        restored = self.service.transition_task(task["id"], "acceptance")
        self.assertEqual("acceptance", restored["status"])
        self.service.interrupt_unsubmitted_run(acceptance["run"]["id"], "cleanup")
        self.service.pause_all_tasks("pause acceptance")
        resumed_acceptance = self.service.resume_task(task["id"])
        self.assertEqual("acceptance", resumed_acceptance["status"])
        self.assertEqual(0, resumed_acceptance["retry_required"])
        self.service.set_dispatcher_enabled(True)

        blocked_review = self.task("blocked-code-review")
        self.deliver(blocked_review, "blocked-dev")
        self.service.transition_task(blocked_review["id"], "blocked", "review blocked")
        self.assertEqual("code_review", self.service.transition_task(blocked_review["id"], "code_review")["status"])


if __name__ == "__main__":
    unittest.main()
