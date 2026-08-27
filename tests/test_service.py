from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from core.db import Database
from core.service import TaskboardService


class TaskboardServiceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.example_project = Path(self.temp.name) / "example"
        self.example_project.mkdir()
        self.unreported_project = Path(self.temp.name) / "unreported"
        self.unreported_project.mkdir()
        self.other_project = Path(self.temp.name) / "other-project"
        self.other_project.mkdir()
        self.second_project = Path(self.temp.name) / "second-project"
        self.second_project.mkdir()
        self.previous_vault = os.environ.get("CODEX_TASKBOARD_OBSIDIAN_VAULT")
        os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = str(Path(self.temp.name) / "vault")
        Path("/tmp/example").mkdir(parents=True, exist_ok=True)
        for path in ("/tmp/other-project", "/tmp/second-project", "/tmp/unreported"):
            Path(path).mkdir(parents=True, exist_ok=True)
        self.service = TaskboardService(self.temp.name)
        self.service.set_dispatcher_enabled(True)

    def tearDown(self) -> None:
        if self.previous_vault is None:
            os.environ.pop("CODEX_TASKBOARD_OBSIDIAN_VAULT", None)
        else:
            os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = self.previous_vault
        self.temp.cleanup()

    def test_dispatcher_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as home:
            service = TaskboardService(home)
            self.assertFalse(service.dispatcher_enabled())

    def test_task_token_budget_setting_controls_new_tasks(self):
        existing = self.create_ready_task()
        self.assertEqual(60000, existing["token_budget"])
        self.assertEqual({"task_token_budget": 60000}, self.service.task_settings())

        self.assertEqual(
            {"task_token_budget": 120000},
            self.service.update_task_settings({"task_token_budget": 120000}),
        )
        created = self.create_ready_task()
        self.assertEqual(120000, created["token_budget"])
        self.assertEqual(60000, self.service.get_task(existing["id"])["token_budget"])
        self.assertEqual(
            {"task_token_budget": 120000},
            TaskboardService(self.temp.name).task_settings(),
        )

    def test_task_token_budget_setting_rejects_invalid_values(self):
        for value in (0, -1, True, "120000"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                self.service.update_task_settings({"task_token_budget": value})

    def test_location_schema_uses_generic_names(self):
        with self.service.db.connection() as connection:
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            columns = {
                row["name"] for row in connection.execute(
                    "PRAGMA table_info(location_analyses)"
                )
            }
        self.assertIn("location_reports", tables)
        self.assertNotIn("codegraph_reports", tables)
        self.assertIn("location_plan", columns)
        self.assertIn("location_evidence", columns)
        self.assertNotIn("codegraph_plan", columns)
        self.assertNotIn("codegraph_evidence", columns)

    def create_ready_task(self):
        return self.create_located_task({
            "title": "实现A页面导入入口",
            "project": str(self.example_project),
            "modules": ["a-page"],
            "goal": "增加导入入口",
            "scope": ["导入按钮"],
            "out_of_scope": ["批量导入历史记录"],
            "acceptance_criteria": ["有权限用户可见", "无权限用户不可见"],
        })

    def create_located_task(self, payload, targets=None):
        # Existing service tests exercise the pre-v2 review API explicitly.
        payload = {**payload, "workflow_version": payload.get("workflow_version", 1)}
        project = payload["project"]
        located_targets = targets or [{"file": "src/APage.tsx", "symbols": ["APage"], "reason": "primary target"}]
        plan_target = located_targets[0]
        plan_symbol = (plan_target.get("symbols") or [""])[0]
        self.service.report_location_status(
            project, True, "connected", "Agent query succeeded",
            {"tool": "codegraph_explore", "query": "APage", "files": [plan_target["file"]], "symbols": [plan_symbol]}, "test-agent",
        )
        analysis = self.service.prepare_location_analysis(payload)
        dependency_analysis = {"decision": "independent"}
        implementation_contract = {
            "targets": located_targets,
            "ordered_steps": [
                {
                    "file": target["file"],
                    "symbol": symbol,
                    "action": "apply the focused test change",
                }
                for target in located_targets
                for symbol in target.get("symbols", [])[:1]
            ],
        }
        review_contract = {"checks": ["Verify the focused change and acceptance criteria"]}
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"],
            {"query": "context", "symbols": ["APage"]},
            located_targets,
            [{
                "criterion": criterion,
                "file": plan_target["file"],
                "symbol": plan_symbol,
                "method": "focused test",
                "expected": criterion,
            } for criterion in payload["acceptance_criteria"]],
            dependency_analysis,
            implementation_contract,
            review_contract,
        )
        return self.service.create_task({
            **payload,
            "status": "ready",
            "location_analysis_id": completed["id"],
            "dependency_analysis": dependency_analysis,
            "implementation_contract": implementation_contract,
            "review_contract": review_contract,
        })

    def prepare_change_request(self, task, *, request_text="把加法改成减法"):
        target = {"file": "src/APage.tsx", "symbols": ["APage"], "reason": "revised behavior"}
        criteria = ["输入两个数字后显示差值"]
        proposed = {
            "title": "A 页面减法计算",
            "project": task["project"],
            "modules": ["a-page"],
            "goal": "将计算方式从加法调整为减法",
            "scope": ["修改计算逻辑"],
            "out_of_scope": ["增加新的计算类型"],
            "acceptance_criteria": criteria,
            "priority": "P1",
            "source_thread_id": "native-source-thread",
            "workflow_version": 2,
        }
        self.service.report_location_status(
            task["project"], True, "connected", "Agent query succeeded",
            {"tool": "codegraph_explore", "query": "APage", "files": [target["file"]], "symbols": ["APage"]},
            "test-agent",
        )
        analysis = self.service.prepare_location_analysis(proposed, "change", task["id"])
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "APage", "symbols": ["APage"]}, [target],
            [{
                "criterion": criteria[0], "file": target["file"], "symbol": "APage",
                "method": "focused test", "expected": criteria[0],
            }],
            {"decision": "independent"},
            {"targets": [target], "ordered_steps": [{"file": target["file"], "symbol": "APage", "action": "replace addition with subtraction"}]},
            {"checks": ["Verify subtraction behavior and regression coverage"]},
        )
        proposed.update({
            "location_analysis_id": completed["id"],
            "dependency_analysis": {"decision": "independent"},
            "implementation_contract": completed["implementation_contract"],
            "review_contract": completed["review_contract"],
        })
        return self.service.prepare_task_change_confirmation({
            "candidate_task_id": task["id"], "source_thread_id": "native-source-thread",
            "request_text": request_text, "proposed_task": proposed,
            "evidence": {"reasons": ["来自同一 Codex 会话", "模块重合：a-page"]},
        })

    def test_ready_task_requires_goal_project_and_acceptance(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            self.service.create_task({
                "title": "信息不完整",
                "status": "ready",
                "workflow_version": 2,
            })

    def test_confirmed_task_enters_ready_queue_without_requirement(self):
        task = self.create_ready_task()
        self.assertEqual("ready", task["status"])
        self.assertIsNone(task["requirement_id"])
        board = self.service.board()
        self.assertNotIn("requirements", board)
        self.assertEqual([str(self.example_project.resolve())], board["projects"])
        self.assertEqual(0, board["counts"]["attention"])

        self.service.transition_task(task["id"], "ready", auto_dispatch=False)
        self.assertEqual(1, self.service.board()["counts"]["attention"])

    def test_task_change_detection_uses_current_thread_without_explicit_task_id(self):
        task = self.create_located_task({
            "title": "A 页面加法计算", "project": str(self.example_project),
            "modules": ["a-page"], "goal": "实现两个数字相加", "scope": ["加法"],
            "out_of_scope": ["其他运算"], "acceptance_criteria": ["显示相加结果"],
            "source_thread_id": "native-source-thread",
        })

        result = self.service.detect_task_change({
            "title": "修改另一个模块", "project": task["project"],
            "goal": "同时调整相关展示", "modules": ["other-module"],
            "source_thread_id": "native-source-thread",
        })

        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(task["id"], result["candidate"]["task_id"])
        self.assertIn("来自同一 Codex 会话", result["candidate"]["reasons"])

    def test_task_change_detection_ignores_generic_module_and_terminal_history(self):
        active = self.create_located_task({
            "title": "登录页品牌文案修改",
            "project": str(self.example_project),
            "modules": ["web", "登录认证"],
            "goal": "修改登录页主标题",
            "scope": ["登录页文案"],
            "out_of_scope": ["其他页面"],
            "acceptance_criteria": ["主标题更新"],
        })
        result = self.service.detect_task_change({
            "title": "场地预约增加仅本次",
            "project": active["project"],
            "goal": "修改重复预约的范围选项",
            "modules": ["web", "场景管理", "场地预约"],
        })
        self.assertEqual("new_task", result["decision"])
        self.assertFalse(result["requires_confirmation"])
        self.assertEqual([], result["candidates"])

        self.service.transition_task(active["id"], "cancelled")
        terminal_result = self.service.detect_task_change({
            "title": "登录页品牌文案修改",
            "project": active["project"],
            "goal": "修改登录页主标题",
            "modules": ["web", "登录认证"],
        })
        self.assertEqual("new_task", terminal_result["decision"])
        self.assertNotIn("relation", terminal_result)

    def test_dependency_analysis_ignores_terminal_and_weak_candidates(self):
        task = self.create_located_task({
            "title": "登录页品牌文案修改",
            "project": str(self.example_project),
            "modules": ["web", "登录认证"],
            "goal": "修改登录页主标题",
            "scope": ["登录页文案"],
            "out_of_scope": ["其他页面"],
            "acceptance_criteria": ["主标题更新"],
        })
        self.service.obsidian.search_task_dependencies = lambda *args, **kwargs: [{
            "task_id": task["id"], "status": "historical",
            "evidence_path": "task.md", "summary": "login task", "score": 999,
        }]

        weak = self.service.analyze_task_dependencies({
            "title": "场地预约增加仅本次",
            "goal": "修改重复预约的范围选项",
            "project": task["project"],
            "modules": ["web", "场景管理", "场地预约"],
            "located_symbols": ["VenueBooking"],
        })
        self.assertEqual("independent", weak["decision"])
        self.assertEqual("weak_match", weak["ignored_candidates"][0]["ignored_reason"])

        self.service.transition_task(task["id"], "cancelled")
        terminal = self.service.analyze_task_dependencies({
            "title": "登录页品牌文案修改",
            "goal": "修改登录页主标题",
            "project": task["project"],
            "modules": ["web", "登录认证"],
            "located_symbols": ["Login"],
        })
        self.assertEqual("independent", terminal["decision"])
        self.assertEqual("terminal_task", terminal["ignored_candidates"][0]["ignored_reason"])

    def test_interactive_change_revision_interrupts_run_and_preserves_task_thread(self):
        task = self.create_ready_task()
        claimed = self.service.claim_next_task("developer", task["project"])
        self.service.bind_conversation(task["id"], "execution", "development-thread", claimed["run"]["id"])
        change = self.prepare_change_request(self.service.get_task(task["id"]))

        self.assertEqual([change["id"]], [item["id"] for item in self.service.board()["pending_task_changes"]])
        resolved = self.service.resolve_task_change_confirmation(change["id"], "revise")

        revised = resolved["task"]
        self.assertEqual(task["id"], revised["id"])
        self.assertEqual("rework", revised["status"])
        self.assertEqual(2, revised["context_version"])
        self.assertEqual("development-thread", revised["codex_thread_id"])
        self.assertEqual("A 页面减法计算", revised["title"])
        self.assertEqual("interrupted", self.service.get_run(claimed["run"]["id"])["status"])
        self.assertEqual([], self.service.board()["pending_task_changes"])
        with self.service.db.connection() as connection:
            revision = connection.execute(
                "SELECT version, change_request_id FROM task_revisions WHERE task_id=?", (task["id"],),
            ).fetchone()
        self.assertEqual((2, change["id"]), tuple(revision))

    def test_interactive_change_can_create_separate_related_task(self):
        task = self.create_ready_task()
        change = self.prepare_change_request(task, request_text="另建一个减法任务")

        resolved = self.service.resolve_task_change_confirmation(change["id"], "create_new")

        self.assertNotEqual(task["id"], resolved["task"]["id"])
        self.assertEqual("ready", resolved["task"]["status"])
        relations = self.service.task_relations(resolved["task"]["id"])
        self.assertTrue(any(
            item["relation_type"] == "references" and item["target_task_id"] == task["id"]
            for item in relations
        ))

    def test_existing_pending_revision_rejects_string_contracts(self):
        task = self.create_ready_task()
        change = self.prepare_change_request(task)
        proposed = dict(change["proposed_task"])
        proposed["implementation_contract"] = {
            "targets": ["src/APage.tsx::APage"],
            "ordered_steps": ["src/APage.tsx::APage — replace addition with subtraction"],
        }
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE task_change_requests SET proposed_task=? WHERE id=?",
                (json.dumps(proposed, ensure_ascii=False), change["id"]),
            )

        with self.assertRaisesRegex(ValueError, "must be an object"):
            self.service.resolve_task_change_confirmation(change["id"], "revise")

    def test_location_completion_rejects_string_contracts(self):
        task = self.create_ready_task()
        target = {"file": "src/APage.tsx", "symbols": ["APage"]}
        proposed = {
            "title": "A 页面修订", "project": task["project"],
            "goal": "调整 A 页面", "modules": ["a-page"],
        }
        analysis = self.service.prepare_location_analysis(proposed, "change", task["id"])

        with self.assertRaisesRegex(ValueError, "must be an object"):
            self.service.complete_location_analysis(
                analysis["analysis_id"], {"query": "APage", "symbols": ["APage"]}, [target],
                [{
                    "criterion": "功能可用", "file": target["file"], "symbol": "APage",
                    "method": "focused test", "expected": "功能可用",
                }],
                {"decision": "independent"},
                {
                    "targets": ["src/APage.tsx::APage"],
                    "ordered_steps": ["src/APage.tsx::APage — 调整 A 页面"],
                },
                {"checks": ["检查 A 页面改动"]},
            )

    def test_stage_timing_and_token_usage_are_recorded_per_run(self):
        task = self.create_ready_task()
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status_started_at='2000-01-01 00:00:00' WHERE id=?",
                (task["id"],),
            )

        claimed = self.service.claim_next_task("test-worker", task["project"])
        self.assertNotEqual(
            "2000-01-01 00:00:00", self.service.get_task(task["id"])["status_started_at"],
        )
        self.service.bind_conversation(
            task["id"], "execution", "execution-token-thread", claimed["run"]["id"],
        )
        self.assertIsNotNone(self.service.get_run(claimed["run"]["id"])["started_at"])

        self.service.record_run_token_usage(claimed["run"]["id"], 1250)
        self.service.record_run_token_usage(claimed["run"]["id"], 1000)
        board_task = next(item for item in self.service.board()["tasks"] if item["id"] == task["id"])

        self.assertEqual(1250, board_task["token_used"])
        self.assertEqual(
            {
                "attempts": 1,
                "token_used": 1250,
                "effective_token_used": 1250,
                "input_tokens": 0,
                "cached_input_tokens": 0,
                "output_tokens": 0,
                "reasoning_output_tokens": 0,
            },
            board_task["token_by_stage"]["execution"],
        )
        with self.service.db.connection() as connection:
            events = connection.execute(
                """SELECT source_total, token_delta FROM token_usage_events
                   WHERE run_id=? ORDER BY id""",
                (claimed["run"]["id"],),
            ).fetchall()
        self.assertEqual([(1250, 1250)], [tuple(event) for event in events])

    def test_token_analytics_groups_incremental_usage_by_local_period(self):
        task = self.create_ready_task()
        claimed = self.service.claim_next_task("test-worker", task["project"])
        run_id = claimed["run"]["id"]
        self.service.record_run_token_usage(run_id, 100)
        self.service.record_run_token_usage(run_id, 300)
        self.service.record_run_token_usage(run_id, 600)
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE token_usage_events SET recorded_at='2026-08-24 01:00:00' WHERE run_id=? AND source_total=100",
                (run_id,),
            )
            connection.execute(
                "UPDATE token_usage_events SET recorded_at='2026-08-20 01:00:00' WHERE run_id=? AND source_total=300",
                (run_id,),
            )
            connection.execute(
                "UPDATE token_usage_events SET recorded_at='2026-08-01 01:00:00' WHERE run_id=? AND source_total=600",
                (run_id,),
            )

        analytics = self.service.token_analytics(datetime(2026, 8, 24, 12, tzinfo=timezone.utc))

        self.assertEqual({"today": 100, "week": 100, "month": 600}, analytics["periods"])
        self.assertEqual(300, next(item["token_used"] for item in analytics["daily"] if item["date"] == "2026-08-01"))
        self.assertEqual(200, next(item["token_used"] for item in analytics["daily"] if item["date"] == "2026-08-20"))
        self.assertEqual(100, analytics["daily"][-1]["token_used"])

    def test_execution_prompt_explicitly_invokes_lifecycle_skill(self):
        task = self.create_ready_task()

        claimed = self.service.claim_next_task("test-worker", task["project"])

        self.assertTrue(claimed["dispatch_prompt"].startswith("$codex-taskboard-lifecycle\n\n"))
        self.assertIn(task["id"], claimed["dispatch_prompt"])
        self.assertIn(claimed["run"]["id"], claimed["dispatch_prompt"])

    def test_location_requires_existing_absolute_project_directory(self):
        payload = {"title": "路径校验", "goal": "拒绝模糊路径", "modules": []}
        with self.assertRaisesRegex(ValueError, "absolute path"):
            self.service.prepare_location_analysis({**payload, "project": "patchx-ai-server"})
        missing = Path(self.temp.name) / "missing-project"
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.service.prepare_location_analysis({**payload, "project": str(missing)})
        with self.service.db.connection() as connection:
            count = connection.execute("SELECT COUNT(*) FROM location_analyses").fetchone()[0]
        self.assertEqual(0, count)

    def test_creation_rechecks_project_directory_before_consuming_location(self):
        payload = {
            "title": "创建前复核路径", "project": str(self.example_project),
            "goal": "目录消失时拒绝创建", "scope": [], "out_of_scope": [],
            "acceptance_criteria": ["任务未创建"],
        }
        self.service.report_location_status(
            payload["project"], True, "connected", "ok",
            {"tool": "codegraph_explore", "files": ["src/A.ts"]}, "agent",
        )
        analysis = self.service.prepare_location_analysis(payload)
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "context"},
            [{"file": "src/A.ts", "symbols": ["A"]}],
            [{
                "criterion": "任务未创建", "file": "src/A.ts", "symbol": "A",
                "method": "unit test", "expected": "reject",
            }],
        )
        self.example_project.rmdir()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.service.create_task({**payload, "status": "ready", "workflow_version": 2, "location_analysis_id": completed["id"]})
        self.assertIsNone(self.service.get_location_analysis(completed["id"])["consumed_at"])

    def test_full_delivery_with_rework(self):
        task = self.create_ready_task()
        task = self.submit_delivery(task, "execution-1")
        stable_run_id = task["primary_run_id"]
        stable_thread_id = task["codex_thread_id"]
        review_run = self.prepare_review(task, "review-1")
        self.assertNotEqual(stable_run_id, review_run["id"])
        self.assertEqual(stable_run_id, review_run["delivery_run_id"])
        task = self.service.review_task(
            task["id"], "fail", ["无权限用户仍可见"], ["有权限用户可见"],
            review_run["id"], ["无权限用户不可见"],
        )
        self.assertEqual("rework", task["status"])
        task = self.submit_delivery(task, "rework-1")
        self.assertNotEqual(stable_run_id, task["primary_run_id"])
        self.assertEqual(stable_thread_id, task["codex_thread_id"])
        rework_run_id = task["primary_run_id"]
        review_run = self.prepare_review(task, "review-2")
        self.assertNotEqual(rework_run_id, review_run["id"])
        self.assertEqual(rework_run_id, review_run["delivery_run_id"])
        task = self.service.review_task(
            task["id"], "pass", passed_items=task["acceptance_criteria"], run_id=review_run["id"],
        )
        self.assertEqual("done", task["status"])
        self.assertEqual(
            ["execution", "review", "rework", "review"],
            [run["run_type"] for run in self.service.list_runs(task["id"])],
        )

    def test_review_claim_requires_delivery_and_prepares_distinct_run(self):
        task = self.create_ready_task()
        with self.assertRaises(sqlite3.IntegrityError), self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET status='review' WHERE id=?", (task["id"],))
        self.assertIsNone(self.service.claim_next_review_task("reviewer"))
        task = self.submit_delivery(self.service.get_task(task["id"]), "execution-review-claim")
        claimed = self.service.claim_next_review_task("reviewer")
        self.assertEqual("review", claimed["run"]["run_type"])
        self.assertEqual("awaiting_thread", claimed["run"]["status"])
        self.assertTrue(claimed["dispatch_prompt"].startswith("$codex-taskboard-lifecycle\n\n"))
        self.assertIn("CodeGraph、GitNexus、直接源码匹配", claimed["dispatch_prompt"])

        analysis_id = claimed["run"]["context_snapshot"]["review_location_analysis_id"]
        completed = self.service.complete_location_analysis(
            analysis_id, {"query": "impact", "symbols": ["APage"]},
            [{"file": "src/APage.tsx", "symbols": ["APage"], "reason": "changed target"}],
            [{
                "criterion": criterion, "file": "src/APage.tsx", "symbol": "APage",
                "method": "focused review", "expected": criterion,
            } for criterion in task["acceptance_criteria"]],
        )
        prepared = self.service.prepare_review_run(task["id"], completed["id"])
        self.assertEqual(claimed["run"]["id"], prepared["run"]["id"])
        self.assertEqual("src/APage.tsx", prepared["run"]["context_snapshot"]["targets"][0]["file"])

    def test_review_uses_distinct_run_and_conversation_and_survives_reload(self):
        task = self.submit_delivery(self.create_ready_task(), "execution-thread")
        delivery_run_id = task["primary_run_id"]
        claimed = self.service.claim_next_review_task("reviewer", task["project"])
        self.assertNotEqual(delivery_run_id, claimed["run"]["id"])
        self.assertEqual(delivery_run_id, claimed["run"]["delivery_run_id"])
        self.assertEqual("", claimed["resume_thread_id"])
        with self.assertRaisesRegex(ValueError, "independent"):
            self.service.bind_conversation(
                task["id"], "review", task["codex_thread_id"], claimed["run"]["id"],
            )
        reloaded = TaskboardService(self.temp.name)
        self.assertEqual("review", reloaded.get_run(claimed["run"]["id"])["run_type"])
        self.service.bind_conversation(task["id"], "review", "independent-review-thread", claimed["run"]["id"])
        conversations = self.service.list_conversations(task["id"])
        self.assertIn("execution-thread", [item["thread_id"] for item in conversations])
        self.assertIn("independent-review-thread", [item["thread_id"] for item in conversations])

    def submit_delivery(self, task, thread_id):
        dispatched = self.service.claim_next_task("test-worker", task["project"])
        run = dispatched["run"]
        thread_id = task.get("codex_thread_id") or thread_id
        role = "rework" if run["run_type"] == "rework" else "execution"
        self.service.bind_conversation(task["id"], role, thread_id, run["id"])
        self.service.transition_task(task["id"], "implementing")
        result = self.service.submit_delivery(
            run["id"], "实现完成", "focused tests passed",
            [{"file": "src/APage.tsx", "symbols": ["APage"], "summary": "updated"}],
            [{"criterion": criterion, "evidence": "passed"} for criterion in task["acceptance_criteria"]],
        )
        return result["task"]

    def prepare_review(self, task, thread_id):
        analysis = self.service.prepare_review_location(task["id"])
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "impact", "symbols": ["APage"]},
            [{"file": "src/APage.tsx", "symbols": ["APage"], "reason": "changed target"}],
            [{
                "criterion": criterion, "file": "src/APage.tsx", "symbol": "APage",
                "method": "focused review", "expected": criterion,
            } for criterion in task["acceptance_criteria"]],
        )
        prepared = self.service.prepare_review_run(task["id"], completed["id"])
        self.service.bind_conversation(task["id"], "review", thread_id, prepared["run"]["id"])
        return self.service.get_run(prepared["run"]["id"])

    def test_relation_and_bounded_context(self):
        first = self.create_ready_task()
        second = self.create_located_task({
            "title": "调整导入按钮位置",
            "project": str(self.example_project),
            "goal": "把按钮放入批量操作菜单",
            "scope": ["移动导入按钮"],
            "out_of_scope": [],
            "acceptance_criteria": ["按钮位于批量操作菜单"],
        })
        result = self.service.add_relation(second["id"], first["id"], "changed_from")
        self.assertEqual("changed_from", result["relations"][0]["relation_type"])
        context = self.service.build_context(second["id"], str(self.example_project))
        self.assertEqual(first["id"], context["direct_relations"][0]["task_id"])
        self.assertEqual("implementation", context["mode"])
        self.assertIn(second["id"], context["instruction"])
        self.assertTrue(
            {"id", "title", "project", "type", "priority", "status", "goal", "scope", "acceptance_criteria"}
            <= set(context["task"])
        )
        self.assertNotIn("modules", context["task"])
        self.assertNotIn("out_of_scope", context["task"])
        self.assertNotIn("experiences", context)
        self.assertNotIn("budget", context)

    def test_configured_data_home_is_independent_from_plugin_code(self):
        configured_home = Path(self.temp.name) / "shared-data"
        previous_home = os.environ.get("CODEX_TASKBOARD_HOME")
        os.environ["CODEX_TASKBOARD_HOME"] = str(configured_home)
        try:
            service = TaskboardService()
            self.assertEqual(configured_home.resolve(), service.data_home)
            self.assertEqual(configured_home.resolve() / "data" / "taskboard.db", service.db.path)
        finally:
            if previous_home is None:
                os.environ.pop("CODEX_TASKBOARD_HOME", None)
            else:
                os.environ["CODEX_TASKBOARD_HOME"] = previous_home

    def test_integration_status_reports_missing_project_for_location(self):
        status = self.service.integration_status()
        self.assertIn("obsidian", status)
        self.assertFalse(status["location"]["available"])
        self.assertEqual("project_path_missing", status["location"]["reason"])

    def test_location_status_is_agent_reported_without_project_access(self):
        missing = self.service.integration_status("/path/that/does/not/exist")["location"]
        self.assertEqual("agent_report_missing", missing["reason"])
        with self.assertRaisesRegex(ValueError, "CodeGraph, GitNexus, or source_match"):
            self.service.report_location_status(
                "/path/that/does/not/exist", True, "connected", "invalid evidence",
                {"query": "Widget", "files": ["src/Widget.ts"]}, "codex-test",
            )
        reported = self.service.report_location_status(
            "/path/that/does/not/exist", True, "connected", "query returned symbols",
            {"tool": "codegraph_explore", "query": "Widget", "files": ["src/Widget.ts"], "symbols": ["Widget"]}, "codex-test",
        )
        self.assertTrue(reported["available"])
        self.assertEqual("agent_reported", reported["mode"])
        self.assertEqual("codex-agent", reported["source"])

    def test_location_status_accepts_bounded_cli_explore_evidence(self):
        project = str(self.example_project)
        evidence = {
            "tool": "codegraph_cli_explore",
            "command": ["codegraph", "explore", "--path", project, "Widget"],
            "exit_code": 0,
            "query": "Widget",
            "files": ["src/Widget.ts"],
            "symbols": ["Widget"],
        }
        reported = self.service.report_location_status(
            project, True, "connected", "CLI query returned symbols", evidence, "codex-test",
        )
        self.assertTrue(reported["available"])
        self.assertEqual("codegraph_cli_explore", reported["evidence"]["tool"])

    def test_location_status_accepts_gitnexus_and_direct_source_match_evidence(self):
        project = str(self.example_project)
        gitnexus = self.service.report_location_status(
            project, True, "connected", "GitNexus query returned symbols", {
                "tool": "gitnexus_query",
                "project_path": project,
                "query": "Widget route",
                "files": ["src/Widget.ts"],
                "symbols": ["Widget"],
            }, "codex-test",
        )
        self.assertTrue(gitnexus["available"])
        self.assertEqual("gitnexus_query", gitnexus["evidence"]["tool"])

        gitnexus_cli = self.service.report_location_status(
            project, True, "connected", "GitNexus CLI returned symbols", {
                "tool": "gitnexus_cli_query",
                "project_path": project,
                "command": ["gitnexus", "query", "--repo", "example", "Widget route"],
                "exit_code": 0,
                "query": "Widget route",
                "files": ["src/Widget.ts"],
                "symbols": ["Widget"],
            }, "codex-test",
        )
        self.assertTrue(gitnexus_cli["available"])
        self.assertEqual("gitnexus_cli_query", gitnexus_cli["evidence"]["tool"])

        source_match = self.service.report_location_status(
            project, True, "connected", "Direct source matching located the target", {
                "tool": "source_match",
                "project_path": project,
                "query": "Widget route",
                "commands": [["rg", "-n", "Widget|route", "src", "tests"]],
                "files": ["src/Widget.ts"],
                "symbols": ["Widget"],
            }, "codex-test",
        )
        self.assertTrue(source_match["available"])
        self.assertEqual("source_match", source_match["evidence"]["tool"])

    def test_location_completion_accepts_direct_source_match_report(self):
        project = str(self.example_project)
        evidence = {
            "tool": "source_match",
            "project_path": project,
            "query": "Widget route",
            "commands": [["rg", "-n", "Widget|route", "src", "tests"]],
            "files": ["src/Widget.ts"],
            "symbols": ["Widget"],
        }
        self.service.report_location_status(
            project, True, "connected", "Direct source matching located the target",
            evidence, "codex-test",
        )
        analysis = self.service.prepare_location_analysis({
            "title": "更新 Widget 路由", "project": project,
            "goal": "调整 Widget 路由行为", "modules": ["widget"],
        })
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], evidence,
            [{"file": "src/Widget.ts", "symbols": ["Widget"]}],
            [{
                "criterion": "路由行为更新", "file": "src/Widget.ts", "symbol": "Widget",
                "method": "focused test", "expected": "route test passes",
            }],
        )
        self.assertEqual("completed", completed["status"])
        self.assertEqual("source_match", completed["location_evidence"]["tool"])

    def test_location_status_rejects_unbounded_or_failed_cli_evidence(self):
        project = str(self.example_project)
        base = {
            "tool": "codegraph_cli_explore",
            "command": ["codegraph", "explore", "--max-files", "8", "Widget"],
            "exit_code": 0,
            "query": "Widget",
            "files": ["src/Widget.ts"],
        }
        with self.assertRaisesRegex(ValueError, "project path"):
            self.service.report_location_status(
                project, True, "connected", "unbounded", base, "codex-test",
            )
        with self.assertRaisesRegex(ValueError, "exit_code=0"):
            self.service.report_location_status(
                project, True, "connected", "failed", {
                    **base,
                    "command": ["codegraph", "explore", "--path", project, "Widget"],
                    "exit_code": 1,
                }, "codex-test",
            )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.report_location_status(
                project, True, "connected", "wrong project", {
                    **base,
                    "command": ["codegraph", "explore", "--path", str(self.unreported_project), "Widget"],
                }, "codex-test",
            )
        with self.assertRaisesRegex(ValueError, "bounded query"):
            self.service.report_location_status(
                project, True, "connected", "missing query", {
                    **base,
                    "command": ["codegraph", "explore", "--path", project, "Widget"],
                    "query": "",
                }, "codex-test",
            )

    def test_location_completion_requires_connected_agent_report(self):
        payload = {
            "title": "测试定位门禁", "project": str(self.unreported_project), "goal": "验证门禁", "modules": ["gate"],
        }
        analysis = self.service.prepare_location_analysis(payload)
        with self.assertRaisesRegex(ValueError, "connected location evidence"):
            self.service.complete_location_analysis(
                analysis["analysis_id"], {"query": "context"},
                [{"file": "src/gate.ts", "symbols": ["gate"]}],
                [{"criterion": "门禁生效", "method": "unit test", "expected": "reject"}],
            )

    def test_overlapping_target_is_visible_and_serialized(self):
        first = self.create_ready_task()
        second = self.create_ready_task()
        self.assertEqual(second["id"], self.service.get_task(first["id"])["target_conflicts"][0]["task_id"])
        self.assertEqual(first["id"], self.service.get_task(second["id"])["target_conflicts"][0]["task_id"])

        claimed = self.service.claim_next_task("worker-1", first["project"])
        self.assertIn(claimed["task"]["id"], {first["id"], second["id"]})
        self.assertIsNone(self.service.claim_next_task("worker-2", first["project"]))

        claimed_id = claimed["task"]["id"]
        waiting_id = second["id"] if claimed_id == first["id"] else first["id"]
        self.service.transition_task(claimed_id, "ready")
        self.service.transition_task(claimed_id, "cancelled")
        next_claim = self.service.claim_next_task("worker-2", first["project"])
        self.assertEqual(waiting_id, next_claim["task"]["id"])

    def test_different_symbols_do_not_conflict_but_project_runs_are_serialized(self):
        base = {
            "project": str(self.example_project), "modules": ["shared"], "scope": ["one symbol"],
            "out_of_scope": [], "acceptance_criteria": ["symbol updated"],
        }
        first = self.create_located_task(
            {**base, "title": "修改左侧组件", "goal": "调整 LeftPane"},
            [{"file": "src/Shared.tsx", "symbols": ["LeftPane"], "reason": "left"}],
        )
        second = self.create_located_task(
            {**base, "title": "修改右侧组件", "goal": "调整 RightPane"},
            [{"file": "src/Shared.tsx", "symbols": ["RightPane"], "reason": "right"}],
        )
        self.assertEqual([], self.service.get_task(first["id"])["target_conflicts"])
        self.assertEqual([], self.service.get_task(second["id"])["target_conflicts"])
        self.assertIsNotNone(self.service.claim_next_task("worker-1", first["project"]))
        self.assertIsNone(self.service.claim_next_task("worker-2", first["project"]))

    def test_existing_task_targets_are_backfilled_by_migration(self):
        first = self.create_ready_task()
        second = self.create_ready_task()
        with self.service.db.transaction() as connection:
            connection.execute("DELETE FROM task_targets")
        Database(self.service.db.path)
        self.assertEqual(second["id"], self.service.get_task(first["id"])["target_conflicts"][0]["task_id"])

    def test_current_database_repairs_stale_run_validation_trigger(self):
        with self.service.db.transaction() as connection:
            connection.executescript(
                """
                DROP TRIGGER validate_run_update;
                CREATE TRIGGER validate_run_update BEFORE UPDATE ON task_runs
                WHEN NEW.run_type NOT IN ('execution','rework','review')
                BEGIN SELECT RAISE(ABORT, 'invalid task run values'); END;
                """
            )

        Database(self.service.db.path)

        with self.service.db.connection() as connection:
            trigger = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='validate_run_update'"
            ).fetchone()["sql"]
        self.assertIn("'bugfix'", trigger)
        self.assertIn("'code_review'", trigger)
        self.assertIn("'acceptance'", trigger)

    def test_notification_schema_is_removed_from_existing_databases(self):
        with self.service.db.transaction() as connection:
            connection.execute("CREATE TABLE notification_channels(project TEXT PRIMARY KEY, thread_id TEXT)")
            connection.execute("CREATE TABLE notifications(id TEXT PRIMARY KEY, task_id TEXT)")
            connection.execute("INSERT INTO id_counters(prefix, value) VALUES('NOTICE', 3)")

        Database(self.service.db.path)

        with self.service.db.connection() as connection:
            tables = {
                row["name"] for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            notice_counter = connection.execute(
                "SELECT 1 FROM id_counters WHERE prefix='NOTICE'"
            ).fetchone()
        self.assertNotIn("notifications", tables)
        self.assertNotIn("notification_channels", tables)
        self.assertIsNone(notice_counter)
        self.assertNotIn("notifications", self.service.board())
        self.assertNotIn("undelivered_notifications", self.service.board()["counts"])

    def test_equivalent_target_paths_conflict(self):
        base = {
            "project": str(self.example_project), "modules": ["shared"], "scope": ["same target"],
            "out_of_scope": [], "acceptance_criteria": ["target updated"],
        }
        first = self.create_located_task(
            {**base, "title": "路径形式一", "goal": "修改共享目标"},
            [{"file": "./src/Shared.tsx", "symbols": ["Shared"], "reason": "first"}],
        )
        second = self.create_located_task(
            {**base, "title": "路径形式二", "goal": "修改共享目标"},
            [{"file": "src/Shared.tsx", "symbols": ["Shared"], "reason": "second"}],
        )
        self.assertEqual(second["id"], self.service.get_task(first["id"])["target_conflicts"][0]["task_id"])

    def test_event_cursor_advances_after_task_creation(self):
        before = self.service.latest_event_id()
        self.create_ready_task()
        self.assertGreater(self.service.latest_event_id(), before)

    def test_read_connection_context_closes_connection(self):
        with self.service.db.connection() as connection:
            connection.execute("SELECT 1").fetchone()
        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_active_execution_run_prevents_duplicate_claim(self):
        task = self.create_ready_task()
        first = self.service.claim_next_task("worker-1", task["project"])
        self.assertIsNotNone(first)
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET status='ready', active_run_id=NULL WHERE id=?",
                (task["id"],),
            )
        self.assertIsNone(self.service.claim_next_task("worker-2", task["project"]))

    def test_pause_is_persistent_and_resume_requeues(self):
        task = self.create_ready_task()
        paused = self.service.pause_all_tasks("test pause")
        self.assertFalse(paused["dispatcher_enabled"])
        self.assertEqual("paused", self.service.get_task(task["id"])["status"])
        self.assertIsNone(self.service.claim_next_task("worker", task["project"]))
        reloaded = TaskboardService(self.temp.name)
        self.assertFalse(reloaded.dispatcher_enabled())
        resumed = reloaded.resume_all_tasks()
        self.assertTrue(resumed["dispatcher_enabled"])
        self.assertEqual("ready", reloaded.get_task(task["id"])["status"])

    def test_repeated_failed_reviews_return_to_rework_queue(self):
        task = self.submit_delivery(self.create_ready_task(), "execution-1")
        review = self.prepare_review(task, "review-1")
        task = self.service.review_task(
            task["id"], "fail", ["first failure"], ["有权限用户可见"],
            review["id"], ["无权限用户不可见"],
        )
        self.assertEqual("rework", task["status"])
        task = self.submit_delivery(task, "rework-1")
        review = self.prepare_review(task, "review-2")
        task = self.service.review_task(
            task["id"], "fail", ["first failure"], ["有权限用户可见"],
            review["id"], ["无权限用户不可见"],
        )
        self.assertEqual("rework", task["status"])
        self.assertFalse(task["auto_dispatch"])
        self.assertIsNone(self.service.claim_next_task("worker", task["project"]))
        self.service.transition_task(task["id"], "rework", auto_dispatch=True)
        claimed = self.service.claim_next_task("worker", task["project"])
        self.assertEqual("rework", claimed["run"]["run_type"])

    def test_interrupted_execution_is_failed_and_not_auto_requeued(self):
        task = self.create_ready_task()
        claimed = self.service.claim_next_task("worker", task["project"])
        run = claimed["run"]
        self.service.bind_conversation(task["id"], "execution", "failed-thread", run["id"])
        result = self.service.interrupt_unsubmitted_run(run["id"], "process exited")
        self.assertEqual("failed", result["task"]["status"])
        self.assertEqual("interrupted", result["run"]["status"])
        self.assertIsNone(self.service.claim_next_task("worker-2", task["project"]))

    def test_review_interruption_stays_in_review(self):
        task = self.submit_delivery(self.create_ready_task(), "execution-review-interrupt")
        claimed = self.service.claim_next_review_task("reviewer")
        self.service.bind_conversation(task["id"], "review", "review-interrupt-thread", claimed["run"]["id"])
        result = self.service.interrupt_unsubmitted_run(claimed["run"]["id"], "review app disconnected")
        self.assertEqual("review", result["task"]["status"])
        self.assertEqual("interrupted", result["run"]["status"])
        self.assertEqual("waiting_review", self.service.get_run(task["primary_run_id"])["status"])
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET review_retry_after=datetime('now','-1 second') WHERE id=?", (task["id"],),
            )
        retried = self.service.claim_next_review_task("reviewer-2", task["project"])
        self.assertNotEqual(claimed["run"]["id"], retried["run"]["id"])
        self.assertEqual("review-interrupt-thread", retried["resume_thread_id"])
        self.assertNotEqual(task["codex_thread_id"], retried["resume_thread_id"])

    def test_review_runs_are_serialized_within_project(self):
        first = self.submit_delivery(self.create_ready_task(), "execution-review-one")
        second = self.create_located_task({
            "title": "第二个验收", "project": str(self.example_project), "goal": "修改其他文件", "scope": ["other"],
            "out_of_scope": [], "acceptance_criteria": ["other updated"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        second_claim = self.service.claim_next_task("worker-2", second["project"])
        self.service.bind_conversation(second["id"], "execution", "execution-review-two", second_claim["run"]["id"])
        self.service.transition_task(second["id"], "implementing")
        second = self.service.submit_delivery(
            second_claim["run"]["id"], "实现完成", "focused tests passed",
            [{"file": "src/Other.tsx", "symbols": ["Other"], "summary": "updated"}],
            [{"criterion": "other updated", "evidence": "passed"}],
        )["task"]
        first_review = self.service.claim_next_review_task("reviewer-1", first["project"])
        self.assertIsNotNone(first_review)
        self.assertIsNone(self.service.claim_next_review_task("reviewer-2", second["project"]))

    def test_multiple_rework_tasks_do_not_deadlock_each_other(self):
        first = self.create_located_task({
            "title": "返工左侧", "project": str(self.example_project), "goal": "修复左侧", "scope": ["left"],
            "out_of_scope": [], "acceptance_criteria": ["left fixed"],
        }, [{"file": "src/Shared.tsx", "symbols": ["LeftPane"]}])
        second = self.create_located_task({
            "title": "返工右侧", "project": str(self.example_project), "goal": "修复右侧", "scope": ["right"],
            "out_of_scope": [], "acceptance_criteria": ["right fixed"],
        }, [{"file": "src/Shared.tsx", "symbols": ["RightPane"]}])
        with self.service.db.transaction() as connection:
            connection.execute("UPDATE tasks SET status='rework' WHERE id IN (?, ?)", (first["id"], second["id"]))
        claimed = self.service.claim_next_task("worker", first["project"])
        self.assertIsNotNone(claimed)
        self.assertIsNone(self.service.claim_next_task("worker-2", first["project"]))

    def test_failed_task_locks_project_and_retry_is_prioritized(self):
        first = self.create_ready_task()
        second = self.create_located_task({
            "title": "其他任务", "project": str(self.example_project), "goal": "修改其他文件", "scope": ["other"],
            "out_of_scope": [], "acceptance_criteria": ["other updated"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        claimed = self.service.claim_next_task("worker", first["project"])
        self.service.bind_conversation(first["id"], "execution", "failed-thread", claimed["run"]["id"])
        self.service.interrupt_unsubmitted_run(claimed["run"]["id"], "process exited")
        self.assertIsNone(self.service.claim_next_task("worker-2", second["project"]))
        self.service.transition_task(first["id"], "ready")
        retried = self.service.claim_next_task("worker-3", first["project"])
        self.assertEqual(first["id"], retried["task"]["id"])
        self.assertEqual("failed-thread", retried["resume_thread_id"])

    def test_actual_git_delta_outside_lock_is_rejected(self):
        project = Path(self.temp.name) / "project"
        (project / "src").mkdir(parents=True)
        (project / "src" / "APage.tsx").write_text("export const APage = 1;\n")
        (project / "src" / "Outside.tsx").write_text("export const Outside = 1;\n")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "init",
        ], check=True)
        task = self.create_located_task({
            "title": "真实差异校验", "project": str(project), "goal": "修改 APage", "scope": ["APage"],
            "out_of_scope": [], "acceptance_criteria": ["APage updated"],
        }, [{"file": "src/APage.tsx", "symbols": ["APage"]}])
        claimed = self.service.claim_next_task("worker", task["project"])
        self.service.bind_conversation(task["id"], "execution", "thread", claimed["run"]["id"])
        self.service.transition_task(task["id"], "implementing")
        (project / "src" / "APage.tsx").write_text("export const APage = 2;\n")
        (project / "src" / "Outside.tsx").write_text("export const Outside = 2;\n")
        with self.assertRaisesRegex(ValueError, "Actual Git changes are outside"):
            self.service.submit_delivery(
                claimed["run"]["id"], "done", "passed",
                [{"file": "src/APage.tsx", "symbols": ["APage"]}],
                [{"criterion": "APage updated", "evidence": "passed"}],
            )

    def test_execution_retry_preserves_the_original_workspace_baseline(self):
        project = Path(self.temp.name) / "retry-project"
        (project / "src").mkdir(parents=True)
        (project / "src" / "A.ts").write_text("export const A = 1;\n")
        (project / "src" / "B.ts").write_text("export const B = 1;\n")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "init",
        ], check=True)
        task = self.create_located_task({
            "title": "累计重试差异", "project": str(project), "goal": "修改 A 和 B", "scope": ["A", "B"],
            "out_of_scope": [], "acceptance_criteria": ["A and B updated"],
        }, [
            {"file": "src/A.ts", "symbols": ["A"]},
            {"file": "src/B.ts", "symbols": ["B"]},
        ])

        first = self.service.claim_next_task("worker-1", task["project"])
        self.service.bind_conversation(task["id"], "execution", "stable-thread", first["run"]["id"])
        self.service.transition_task(task["id"], "implementing")
        (project / "src" / "A.ts").write_text("export const A = 2;\n")
        self.service.interrupt_unsubmitted_run(first["run"]["id"], "worker crashed")
        self.service.transition_task(task["id"], "ready")

        second = self.service.claim_next_task("worker-2", task["project"])
        self.assertEqual("stable-thread", second["resume_thread_id"])
        self.assertEqual(
            first["run"]["context_snapshot"]["workspace_baseline"],
            second["run"]["context_snapshot"]["workspace_baseline"],
        )
        self.service.bind_conversation(task["id"], "execution", "stable-thread", second["run"]["id"])
        self.service.transition_task(task["id"], "implementing")
        (project / "src" / "B.ts").write_text("export const B = 2;\n")
        with self.assertRaisesRegex(ValueError, "src/A.ts"):
            self.service.submit_delivery(
                second["run"]["id"], "done", "passed",
                [{"file": "src/B.ts", "symbols": ["B"]}],
                [{"criterion": "A and B updated", "evidence": "passed"}],
            )
        delivered = self.service.submit_delivery(
            second["run"]["id"], "done", "passed",
            [
                {"file": "src/A.ts", "symbols": ["A"]},
                {"file": "src/B.ts", "symbols": ["B"]},
            ],
            [{"criterion": "A and B updated", "evidence": "passed"}],
        )
        self.assertEqual("review", delivered["task"]["status"])
        with self.service.db.connection() as connection:
            mapping = connection.execute(
                "SELECT run_id FROM task_conversations WHERE task_id=? AND thread_id=? AND role='execution'",
                (task["id"], "stable-thread"),
            ).fetchone()
        self.assertEqual(second["run"]["id"], mapping["run_id"])

    def test_task_creation_and_location_consumption_are_atomic(self):
        payload = {
            "title": "原子创建", "project": str(self.example_project), "goal": "验证原子性", "scope": ["atomic"],
            "out_of_scope": [], "acceptance_criteria": ["created"],
        }
        self.service.report_location_status(
            payload["project"], True, "connected", "ok", {"tool": "codegraph_explore", "query": "Atomic", "files": ["src/Atomic.ts"]}, "test-agent",
        )
        analysis = self.service.prepare_location_analysis(payload)
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "context"}, [{"file": "src/Atomic.ts", "symbols": ["Atomic"]}],
            [{"criterion": "created", "file": "src/Atomic.ts", "symbol": "Atomic", "method": "test", "expected": "created"}],
        )
        original_next_id = self.service.db.next_id

        def fail_next_id(connection, prefix):
            if prefix == "TASK":
                raise RuntimeError("insert failed")
            return original_next_id(connection, prefix)

        self.service.db.next_id = fail_next_id
        try:
            with self.assertRaisesRegex(RuntimeError, "insert failed"):
                self.service.create_task({
                    **payload,
                    "status": "ready",
                    "workflow_version": 1,
                    "location_analysis_id": completed["id"],
                    "dependency_analysis": completed["dependency_analysis"],
                    "implementation_contract": completed["implementation_contract"],
                    "review_contract": completed["review_contract"],
                })
        finally:
            self.service.db.next_id = original_next_id
        self.assertIsNone(self.service.get_location_analysis(completed["id"])["consumed_at"])

    def test_delivery_rejects_changes_outside_target_lock(self):
        task = self.create_ready_task()
        dispatched = self.service.claim_next_task("worker", task["project"])
        run = dispatched["run"]
        self.service.bind_conversation(task["id"], "execution", "thread", run["id"])
        self.service.transition_task(task["id"], "implementing")
        with self.assertRaisesRegex(ValueError, "outside the task target lock"):
            self.service.submit_delivery(
                run["id"], "done", "tests passed",
                [{"file": "src/Outside.tsx", "symbols": ["Outside"], "summary": "changed"}],
                [{"criterion": criterion, "evidence": "passed"} for criterion in task["acceptance_criteria"]],
            )

    def test_expired_location_report_is_stale(self):
        project = str(self.example_project)
        self.service.report_location_status(
            project, True, "connected", "ok", {"tool": "codegraph_explore", "query": "APage", "files": ["src/APage.tsx"]}, "test-agent",
        )
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE location_reports SET checked_at=datetime('now','-2 hours') WHERE project=?",
                (str(Path(project).resolve()),),
            )
        status = self.service.location_status(project)
        self.assertFalse(status["available"])
        self.assertEqual("stale", status["state"])
        self.assertEqual("agent_report_expired", status["reason"])

    def test_cancelling_failed_task_clears_retry_lock(self):
        failed = self.create_ready_task()
        other = self.create_located_task({
            "title": "独立后续任务", "project": str(self.example_project), "goal": "修改其他位置",
            "scope": ["other"], "out_of_scope": [], "acceptance_criteria": ["other updated"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        claim = self.service.claim_next_task("worker", failed["project"])
        self.service.bind_conversation(failed["id"], "execution", "failed-thread", claim["run"]["id"])
        self.service.interrupt_unsubmitted_run(claim["run"]["id"], "process exited")
        cancelled = self.service.transition_task(failed["id"], "cancelled")
        self.assertFalse(cancelled["retry_required"])
        self.assertIsNone(cancelled["retry_run_type"])
        self.assertEqual(other["id"], self.service.claim_next_task("next-worker", other["project"])["task"]["id"])

    def test_waiting_confirmation_requeues_through_dispatcher(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("worker", task["project"])
        run = claim["run"]
        self.service.bind_conversation(task["id"], "execution", "question-thread", run["id"])
        self.service.record_run_token_usage(run["id"], task["token_budget"])
        self.service.transition_task(task["id"], "waiting_confirmation", "需要确认最小边界")
        stopped = self.service.interrupt_unsubmitted_run(run["id"], "等待用户确认")
        self.assertEqual("waiting_confirmation", stopped["task"]["status"])
        self.assertTrue(stopped["task"]["retry_required"])
        self.assertEqual("需要确认最小边界", stopped["task"]["last_failure_reason"])
        with self.assertRaisesRegex(ValueError, "Invalid task transition"):
            self.service.transition_task(task["id"], "investigating")
        increased_budget = task["token_budget"] * 2
        restarted = self.service.transition_task(
            task["id"], "ready", token_budget=increased_budget, auto_dispatch=True,
        )
        self.assertEqual(increased_budget, restarted["token_budget"])
        self.assertTrue(restarted["auto_dispatch"])
        resumed = self.service.claim_next_task("worker-2", task["project"])
        self.assertEqual("question-thread", resumed["resume_thread_id"])

    def test_token_budget_cannot_change_outside_budget_restart(self):
        task = self.create_ready_task()
        with self.assertRaisesRegex(ValueError, "budget-exhausted"):
            self.service.transition_task(
                task["id"], "ready", token_budget=task["token_budget"] * 2,
            )

    def test_creation_rejects_acceptance_plan_mismatch_without_consuming_location(self):
        payload = {
            "title": "验收契约", "project": str(self.example_project), "goal": "验证契约", "scope": ["contract"],
            "out_of_scope": [], "acceptance_criteria": ["标准 B"],
        }
        self.service.report_location_status(payload["project"], True, "connected", "ok", {"tool": "codegraph_explore", "query": "Contract", "files": ["src/Contract.ts"]}, "agent")
        analysis = self.service.prepare_location_analysis(payload)
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "context"},
            [{"file": "src/Contract.ts", "symbols": ["Contract"]}],
            [{"criterion": "标准 A", "file": "src/Contract.ts", "symbol": "Contract", "method": "test", "expected": "pass"}],
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.service.create_task({**payload, "status": "ready", "workflow_version": 2, "location_analysis_id": completed["id"]})
        self.assertIsNone(self.service.get_location_analysis(completed["id"])["consumed_at"])

    def test_committed_change_since_claim_is_valid_delivery_delta(self):
        project = Path(self.temp.name) / "commit-project"
        (project / "src").mkdir(parents=True)
        target = project / "src" / "APage.tsx"
        target.write_text("export const APage = 1;\n")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        commit = ["git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm"]
        subprocess.run([*commit, "init"], check=True)
        task = self.create_located_task({
            "title": "提交后交付", "project": str(project), "goal": "修改 APage", "scope": ["APage"],
            "out_of_scope": [], "acceptance_criteria": ["APage updated"],
        })
        claim = self.service.claim_next_task("worker", task["project"])
        self.service.bind_conversation(task["id"], "execution", "commit-thread", claim["run"]["id"])
        self.service.transition_task(task["id"], "implementing")
        target.write_text("export const APage = 2;\n")
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run([*commit, "task change"], check=True)
        delivered = self.service.submit_delivery(
            claim["run"]["id"], "done", "tests passed",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": "APage updated", "evidence": "passed"}],
        )
        self.assertEqual("review", delivered["task"]["status"])

    def test_reused_execution_thread_preserves_every_run_mapping(self):
        task = self.create_ready_task()
        first = self.service.claim_next_task("worker", task["project"])
        self.service.bind_conversation(task["id"], "execution", "same-thread", first["run"]["id"])
        self.service.interrupt_unsubmitted_run(first["run"]["id"], "process exited")
        self.service.transition_task(task["id"], "ready")
        second = self.service.claim_next_task("worker-2", task["project"])
        self.service.bind_conversation(task["id"], "execution", "same-thread", second["run"]["id"])
        self.service.interrupt_unsubmitted_run(second["run"]["id"], "second exit")
        conversation = next(item for item in self.service.list_conversations(task["id"]) if item["thread_id"] == "same-thread")
        self.assertEqual([first["run"]["id"], second["run"]["id"]], conversation["run_ids"])
        self.assertEqual("interrupted", conversation["status"])
        self.assertEqual(["same-thread", "same-thread"], [run["conversation_thread_id"] for run in self.service.list_runs(task["id"])])

    def test_project_blocker_is_visible_on_waiting_task(self):
        blocker = self.create_ready_task()
        waiting = self.create_located_task({
            "title": "等待项目解锁", "project": str(self.example_project), "goal": "修改其他位置", "scope": ["other"],
            "out_of_scope": [], "acceptance_criteria": ["other updated"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        claim = self.service.claim_next_task("worker", blocker["project"])
        self.service.bind_conversation(blocker["id"], "execution", "blocked-project-thread", claim["run"]["id"])
        self.service.interrupt_unsubmitted_run(claim["run"]["id"], "crashed")
        visible = self.service.get_task(waiting["id"])["project_blockers"]
        self.assertEqual(blocker["id"], visible[0]["task_id"])
        self.assertEqual("retry_pending", visible[0]["blocker_type"])

    def test_location_report_becomes_stale_when_workspace_changes(self):
        project = Path(self.temp.name) / "location-project"
        project.mkdir()
        target = project / "target.txt"
        target.write_text("one\n")
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(["git", "-C", str(project), "add", "."], check=True)
        subprocess.run([
            "git", "-C", str(project), "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-qm", "init",
        ], check=True)
        self.service.report_location_status(str(project), True, "connected", "ok", {"tool": "codegraph_explore", "query": "tracked", "files": ["tracked.txt"]}, "agent")
        target.write_text("two\n")
        status = self.service.location_status(str(project))
        self.assertFalse(status["available"])
        self.assertEqual("repository_workspace_changed", status["reason"])

    def test_orphaned_dispatcher_execution_is_recovered_immediately(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("codex-taskboard-dispatcher", task["project"])
        self.service.bind_conversation(task["id"], "execution", "orphan-thread", claim["run"]["id"])
        self.assertEqual(1, self.service.recover_orphaned_dispatcher_runs("codex-taskboard-dispatcher"))
        self.assertEqual("failed", self.service.get_task(task["id"])["status"])

    def test_resume_single_paused_execution_restores_ready_retry(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("worker", task["project"])
        self.service.bind_conversation(task["id"], "execution", "paused-thread", claim["run"]["id"])
        self.service.pause_all_tasks("operator pause")
        resumed = self.service.resume_task(task["id"])
        self.assertEqual("ready", resumed["status"])
        self.assertTrue(resumed["retry_required"])

    def test_generic_transition_cannot_bypass_delivery_or_review(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("worker", task["project"])
        self.service.bind_conversation(task["id"], "execution", "execution-thread", claim["run"]["id"])
        self.service.transition_task(task["id"], "implementing")
        with self.assertRaisesRegex(ValueError, "Invalid task transition"):
            self.service.transition_task(task["id"], "review")
        task = self.service.submit_delivery(
            claim["run"]["id"], "implemented", "tests passed",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": criterion, "evidence": "passed"} for criterion in task["acceptance_criteria"]],
        )["task"]
        review = self.prepare_review(task, "review-thread")
        with self.assertRaisesRegex(ValueError, "Unknown task status"):
            self.service.transition_task(task["id"], "completion")
        self.assertEqual("running", self.service.get_run(review["id"])["status"])

    def test_review_interruptions_back_off_and_pause_after_three_attempts(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        for attempt in range(1, 4):
            claim = self.service.claim_next_review_task("reviewer")
            self.assertIsNotNone(claim)
            self.service.bind_conversation(task["id"], "review", "review-retry-thread", claim["run"]["id"])
            result = self.service.interrupt_unsubmitted_run(claim["run"]["id"], "turn ended")
            self.assertEqual("review", result["task"]["status"])
            self.assertIsNone(self.service.claim_next_review_task("reviewer"))
            if attempt < 3:
                with self.service.db.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET review_retry_after=datetime('now','-1 second') WHERE id=?",
                        (task["id"],),
                    )
        paused = self.service.get_task(task["id"])
        self.assertFalse(paused["auto_dispatch"])
        self.assertEqual(3, paused["review_interrupt_count"])
        self.service.transition_task(task["id"], "review", auto_dispatch=True)
        self.assertIsNotNone(self.service.claim_next_review_task("reviewer"))

    def test_blocking_review_clears_run_and_returns_to_review(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        claim = self.service.claim_next_review_task("reviewer")
        self.service.bind_conversation(task["id"], "review", "blocking-review-thread", claim["run"]["id"])
        blocked = self.service.transition_task(task["id"], "blocked", "external dependency")
        self.assertIsNone(blocked["active_run_id"])
        self.assertEqual("interrupted", self.service.get_run(claim["run"]["id"])["status"])
        resumed = self.service.transition_task(task["id"], "review")
        self.assertEqual("review", resumed["status"])

    def test_third_execution_retry_still_resumes_original_thread(self):
        task = self.create_ready_task()
        run_ids = []
        for attempt in range(2):
            claim = self.service.claim_next_task(f"worker-{attempt}", task["project"])
            run_ids.append(claim["run"]["id"])
            expected = "" if attempt == 0 else "stable-thread"
            self.assertEqual(expected, claim["resume_thread_id"])
            self.service.bind_conversation(task["id"], "execution", "stable-thread", claim["run"]["id"])
            self.service.interrupt_unsubmitted_run(claim["run"]["id"], "crashed")
            self.service.transition_task(task["id"], "ready")
        third = self.service.claim_next_task("worker-3", task["project"])
        run_ids.append(third["run"]["id"])
        self.assertEqual("stable-thread", third["resume_thread_id"])
        self.assertEqual(3, len(set(run_ids)))

    def test_target_paths_cannot_escape_project(self):
        payload = {
            "title": "unsafe", "project": str(self.example_project), "goal": "unsafe", "modules": [],
            "scope": [], "out_of_scope": [], "acceptance_criteria": ["safe"],
        }
        self.service.report_location_status(
            payload["project"], True, "connected", "ok", {"tool": "codegraph_explore", "query": "safe", "files": ["src/Safe.ts"]}, "agent",
        )
        for unsafe in ("../../outside.txt", "/tmp/outside.txt", "C:\\outside.txt"):
            analysis = self.service.prepare_location_analysis(payload)
            with self.assertRaisesRegex(ValueError, "project|relative|escapes"):
                self.service.complete_location_analysis(
                    analysis["analysis_id"], {"query": "context"},
                    [{"file": unsafe, "symbols": ["X"]}],
                    [{"criterion": "safe", "file": unsafe, "symbol": "X", "method": "test", "expected": "safe"}],
                )

    def test_review_location_is_limited_to_current_delivery(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        analysis = self.service.prepare_review_location(task["id"])
        with self.assertRaisesRegex(ValueError, "outside delivered changes and location evidence"):
            self.service.complete_location_analysis(
                analysis["analysis_id"], {"query": "impact"},
                [{"file": "src/Unrelated.tsx", "symbols": ["Other"]}],
                [{
                    "criterion": criterion, "file": "src/Unrelated.tsx", "symbol": "Other",
                    "method": "review", "expected": criterion,
                } for criterion in task["acceptance_criteria"]],
            )

        related = self.service.prepare_review_location(task["id"])
        completed = self.service.complete_location_analysis(
            related["analysis_id"], {"query": "impact", "files": ["tests/APage.test.tsx"]},
            [{"file": "tests/APage.test.tsx", "symbols": ["APageTest"]}],
            [{
                "criterion": criterion, "file": "tests/APage.test.tsx", "symbol": "APageTest",
                "method": "review existing direct test", "expected": criterion,
            } for criterion in task["acceptance_criteria"]],
        )
        self.assertEqual("tests/APage.test.tsx", completed["targets"][0]["file"])

    def test_claim_context_failure_releases_task_and_run(self):
        task = self.create_ready_task()
        self.service.build_execution_context = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("context failed"))
        with self.assertRaisesRegex(RuntimeError, "context failed"):
            self.service.claim_next_task("worker", task["project"])
        self.assertEqual("ready", self.service.get_task(task["id"])["status"])
        self.assertEqual("expired", self.service.list_runs(task["id"])[-1]["status"])

    def test_context_preparation_failure_backs_off_then_blocks(self):
        task = self.create_ready_task()
        self.service.build_execution_context = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("context failed"))
        for attempt in range(1, 4):
            with self.assertRaisesRegex(RuntimeError, "context failed"):
                self.service.claim_next_task("worker", task["project"])
            current = self.service.get_task(task["id"])
            self.assertEqual(attempt, current["dispatch_failure_count"])
            if attempt < 3:
                self.assertEqual("ready", current["status"])
                self.assertIsNotNone(current["dispatch_retry_after"])
                self.assertIsNone(self.service.claim_next_task("worker", task["project"]), "backoff must prevent a hot loop")
                with self.service.db.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET dispatch_retry_after=datetime('now','-1 second') WHERE id=?",
                        (task["id"],),
                    )
            else:
                self.assertEqual("blocked", current["status"])
                self.assertFalse(current["auto_dispatch"])
                self.assertIn("context failed", current["last_dispatch_error"])

    def test_review_preparation_failure_backs_off_then_pauses_auto_dispatch(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        self.service.prepare_review_location = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("review prep failed"))
        for attempt in range(1, 4):
            with self.assertRaisesRegex(RuntimeError, "review prep failed"):
                self.service.claim_next_review_task("reviewer", task["project"])
            current = self.service.get_task(task["id"])
            self.assertEqual(attempt, current["review_interrupt_count"])
            if attempt < 3:
                self.assertTrue(current["auto_dispatch"])
                self.assertIsNotNone(current["review_retry_after"])
                self.assertIsNone(self.service.claim_next_review_task("reviewer", task["project"]))
                with self.service.db.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET review_retry_after=datetime('now','-1 second') WHERE id=?",
                        (task["id"],),
                    )
            else:
                self.assertFalse(current["auto_dispatch"])
                self.assertIsNone(current["active_run_id"])

    def test_individual_pause_resumes_review_without_reimplementation(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        review = self.service.claim_next_review_task("reviewer", task["project"])
        self.service.bind_conversation(task["id"], "review", "paused-review-thread", review["run"]["id"])
        paused = self.service.transition_task(task["id"], "paused", "manual pause")
        self.assertEqual("review", paused["paused_from_status"])
        resumed = self.service.resume_task(task["id"])
        self.assertEqual("review", resumed["status"])
        self.assertFalse(resumed["retry_required"])

    def test_review_cannot_complete_before_location_gate_or_with_partial_results(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        claim = self.service.claim_next_review_task("reviewer", task["project"])
        self.service.bind_conversation(task["id"], "review", "located-review-thread", claim["run"]["id"])
        with self.assertRaisesRegex(ValueError, "location analysis"):
            self.service.review_task(
                task["id"], "pass", passed_items=task["acceptance_criteria"], run_id=claim["run"]["id"],
            )

        analysis_id = claim["run"]["context_snapshot"]["review_location_analysis_id"]
        completed = self.service.complete_location_analysis(
            analysis_id, {"query": "impact"},
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{
                "criterion": criterion, "file": "src/APage.tsx", "symbol": "APage",
                "method": "focused review", "expected": criterion,
            } for criterion in task["acceptance_criteria"]],
        )
        prepared = self.service.prepare_review_run(task["id"], completed["id"])
        with self.assertRaisesRegex(ValueError, "exactly cover"):
            self.service.review_task(
                task["id"], "pass", passed_items=[task["acceptance_criteria"][0]],
                run_id=prepared["run"]["id"],
            )
        done = self.service.review_task(
            task["id"], "pass", passed_items=task["acceptance_criteria"], run_id=prepared["run"]["id"],
        )
        self.assertEqual("done", done["status"])

    def test_required_automated_acceptance_check_blocks_pass_until_command_succeeds(self):
        task = self.submit_delivery(self.create_ready_task(), "execution-acceptance-check")
        claim = self.service.claim_next_review_task("reviewer", task["project"])
        analysis_id = claim["run"]["context_snapshot"]["review_location_analysis_id"]
        completed = self.service.complete_location_analysis(
            analysis_id, {"query": "impact", "files": ["src/APage.tsx"]},
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{
                "criterion": criterion, "file": "src/APage.tsx", "symbol": "APage",
                "method": "automated check", "check_type": "automated",
                "command": "test -f acceptance.ok", "expected": "acceptance.ok exists",
            } for criterion in task["acceptance_criteria"]],
        )
        prepared = self.service.prepare_review_run(task["id"], completed["id"])
        self.service.bind_conversation(
            task["id"], "review", "automated-review-thread", prepared["run"]["id"],
        )
        with self.assertRaisesRegex(ValueError, "were not run"):
            self.service.review_task(
                task["id"], "pass", passed_items=task["acceptance_criteria"], run_id=prepared["run"]["id"],
            )
        failed = self.service.run_acceptance_checks(task["id"], prepared["run"]["id"])
        self.assertFalse(failed["all_required_passed"])
        self.assertEqual(["failed", "failed"], [item["status"] for item in failed["checks"]])
        with self.assertRaisesRegex(ValueError, "did not pass"):
            self.service.review_task(
                task["id"], "pass", passed_items=task["acceptance_criteria"], run_id=prepared["run"]["id"],
            )
        (self.example_project / "acceptance.ok").write_text("ok\n")
        passed = self.service.run_acceptance_checks(task["id"], prepared["run"]["id"])
        self.assertTrue(passed["all_required_passed"])
        done = self.service.review_task(
            task["id"], "pass", passed_items=task["acceptance_criteria"], run_id=prepared["run"]["id"],
        )
        self.assertEqual("done", done["status"])
        self.assertEqual(4, len(self.service.list_acceptance_checks(task["id"])))

    def test_run_conversation_role_must_match_run_type(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("worker", task["project"])
        with self.assertRaisesRegex(ValueError, "must use conversation role execution"):
            self.service.bind_conversation(task["id"], "review", "wrong-thread", claim["run"]["id"])
        with self.assertRaisesRegex(ValueError, "cannot be bound"):
            self.service.bind_conversation(task["id"], "source", "wrong-thread", claim["run"]["id"])

    def test_dependency_cycles_are_rejected_and_blocks_are_enforced(self):
        first = self.create_ready_task()
        second = self.create_located_task({
            "title": "second", "project": str(self.example_project), "goal": "second", "scope": [],
            "out_of_scope": [], "acceptance_criteria": ["second"],
        }, [{"file": "src/Second.tsx", "symbols": ["Second"]}])
        self.service.add_relation(first["id"], second["id"], "depends_on")
        with self.assertRaisesRegex(ValueError, "cycle"):
            self.service.add_relation(second["id"], first["id"], "depends_on")

        other = self.create_located_task({
            "title": "blocked by first", "project": str(self.other_project), "goal": "other", "scope": [],
            "out_of_scope": [], "acceptance_criteria": ["other"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        self.service.add_relation(first["id"], other["id"], "blocks")
        self.assertIsNone(self.service.claim_next_task("other-worker", other["project"]))

    def test_obsidian_failure_is_deferred_without_failing_task_creation(self):
        payload = {
            "title": "outbox", "project": str(self.example_project), "goal": "outbox", "modules": [],
            "scope": [], "out_of_scope": [], "acceptance_criteria": ["created"],
        }
        self.service.report_location_status(
            payload["project"], True, "connected", "ok", {"tool": "codegraph_explore", "query": "atomic", "files": ["src/Atomic.ts"]}, "agent",
        )
        analysis = self.service.prepare_location_analysis(payload)
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "context"},
            [{"file": "src/A.ts", "symbols": ["A"]}],
            [{"criterion": "created", "file": "src/A.ts", "symbol": "A", "method": "test", "expected": "created"}],
        )
        self.service.obsidian.sync_task = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("vault unavailable"))
        task = self.service.create_task({
            **payload,
            "status": "ready",
            "workflow_version": 1,
            "location_analysis_id": completed["id"],
            "dependency_analysis": completed["dependency_analysis"],
            "implementation_contract": completed["implementation_contract"],
            "review_contract": completed["review_contract"],
        })
        self.assertEqual("ready", task["status"])
        with self.service.db.connection() as connection:
            pending = connection.execute(
                "SELECT attempts, last_error FROM integration_outbox WHERE entity_type='task' AND entity_id=?",
                (task["id"],),
            ).fetchone()
        self.assertEqual(1, pending["attempts"])
        self.assertIn("vault unavailable", pending["last_error"])

    def test_old_review_location_cannot_be_used_for_new_delivery(self):
        task = self.submit_delivery(self.create_ready_task(), "execution")
        analysis = self.service.prepare_review_location(task["id"])
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"], {"query": "impact"},
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{
                "criterion": criterion, "file": "src/APage.tsx", "symbol": "APage",
                "method": "review", "expected": criterion,
            } for criterion in task["acceptance_criteria"]],
        )
        review = self.service.prepare_review_run(task["id"], completed["id"])
        self.service.bind_conversation(task["id"], "review", "old-review-thread", review["run"]["id"])
        task = self.service.review_task(
            task["id"], "fail", ["failed"], [task["acceptance_criteria"][1]],
            review["run"]["id"], [task["acceptance_criteria"][0]],
        )
        task = self.submit_delivery(task, "rework")
        with self.assertRaisesRegex(ValueError, "older delivery"):
            self.service.prepare_review_run(task["id"], completed["id"])

    def test_explicit_conflict_relation_blocks_only_while_peer_is_active(self):
        first = self.create_ready_task()
        second = self.create_located_task({
            "title": "cross project conflict", "project": str(self.second_project), "goal": "conflict",
            "scope": [], "out_of_scope": [], "acceptance_criteria": ["conflict"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        self.service.add_relation(first["id"], second["id"], "conflicts_with")
        claim = self.service.claim_next_task("worker", first["project"])
        self.assertEqual(first["id"], claim["task"]["id"])
        self.assertIsNone(self.service.claim_next_task("other-worker", second["project"]))


if __name__ == "__main__":
    unittest.main()
