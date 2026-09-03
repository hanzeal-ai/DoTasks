from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

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
        self.previous_vault = os.environ.get("DOTASKS_OBSIDIAN_VAULT")
        os.environ["DOTASKS_OBSIDIAN_VAULT"] = str(Path(self.temp.name) / "vault")
        Path("/tmp/example").mkdir(parents=True, exist_ok=True)
        for path in ("/tmp/other-project", "/tmp/second-project", "/tmp/unreported"):
            Path(path).mkdir(parents=True, exist_ok=True)
        self.service = TaskboardService(self.temp.name)
        self.service.set_dispatcher_enabled(True)

    def tearDown(self) -> None:
        if self.previous_vault is None:
            os.environ.pop("DOTASKS_OBSIDIAN_VAULT", None)
        else:
            os.environ["DOTASKS_OBSIDIAN_VAULT"] = self.previous_vault
        self.temp.cleanup()

    def test_dispatcher_is_disabled_by_default(self):
        with tempfile.TemporaryDirectory() as home:
            service = TaskboardService(home)
            self.assertFalse(service.dispatcher_enabled())

    def test_native_dispatch_is_persisted_and_binds_only_a_real_thread(self):
        task = self.create_ready_task()
        dispatch = self.service._claim_next_native_dispatch("codex-native-controller", stage="development")

        self.assertEqual((task["id"], "claimed"), (dispatch["entity_id"], dispatch["status"]))
        self.assertTrue(dispatch["dispatch_attempt_id"])
        self.assertEqual("claimed", self.service.get_task(task["id"])["status"])
        self.assertEqual("awaiting_thread", self.service.get_run(dispatch["run_id"])["status"])
        self.assertEqual(f"[DoTaks] {task['id']} 开发", dispatch["dispatch_title"])
        self.assertTrue(
            dispatch["dispatch_prompt"].startswith(
                "[$dotasks:dotasks-lifecycle]("
            )
        )
        self.assertIn("/skills/dotasks-lifecycle/SKILL.md)", dispatch["dispatch_prompt"])
        self.assertIn(
            "DoTasks lifecycle CLI fallback:", dispatch["dispatch_prompt"]
        )
        self.assertIn("/scripts/mcp-server`", dispatch["dispatch_prompt"])
        self.assertIn("$dotasks-lifecycle", dispatch["dispatch_prompt"])
        self.assertLess(
            dispatch["dispatch_prompt"].index("请完成以下任务："),
            dispatch["dispatch_prompt"].index("$dotasks-lifecycle"),
        )
        self.assertGreater(
            dispatch["dispatch_prompt"].index("DoTasks lifecycle CLI fallback:"),
            dispatch["dispatch_prompt"].index("RUN_CONTEXT_JSON="),
        )
        self.assertEqual(
            dispatch["run_id"],
            self.service._claim_next_native_dispatch("codex-native-controller", stage="development")["run_id"],
        )
        with self.assertRaisesRegex(ValueError, "client_thread_id is required"):
            self.service.mark_native_dispatch_pending(
                dispatch["run_id"], "",
                dispatch_attempt_id=dispatch["dispatch_attempt_id"],
            )
        with self.assertRaisesRegex(ValueError, "attempt is stale"):
            self.service.bind_native_dispatch(
                dispatch["run_id"], "stale-thread",
                dispatch_attempt_id="stale-attempt",
            )

        pending = self.service.mark_native_dispatch_pending(
            dispatch["run_id"], "client-pending-1", "local", "project-1",
            dispatch_attempt_id=dispatch["dispatch_attempt_id"],
        )
        self.assertEqual(("pending_thread", "client-pending-1"), (pending["status"], pending["client_thread_id"]))
        self.assertEqual("claimed", self.service.get_task(task["id"])["status"])
        bound = self.service.bind_native_dispatch(
            dispatch["run_id"], "native-thread-1", "local", "project-1",
            dispatch_attempt_id=dispatch["dispatch_attempt_id"],
        )
        self.assertEqual(("bound", "native-thread-1"), (bound["status"], bound["thread_id"]))
        bound_task = self.service.get_task(task["id"])
        self.assertEqual(("implementing", "native-thread-1"), (bound_task["status"], bound_task["codex_thread_id"]))
        self.assertEqual("running", self.service.get_run(dispatch["run_id"])["status"])

    def test_native_dispatch_binding_rolls_back_every_record_on_failure(self):
        task = self.create_ready_task()
        dispatch = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="development",
        )
        original_event = self.service._event

        def fail_after_binding(connection, entity_type, entity_id, event_type, payload):
            if event_type == "native_thread_bound":
                raise RuntimeError("simulated event failure")
            return original_event(
                connection, entity_type, entity_id, event_type, payload,
            )

        with patch.object(self.service, "_event", side_effect=fail_after_binding):
            with self.assertRaisesRegex(RuntimeError, "simulated event failure"):
                self.service.bind_native_dispatch(
                    dispatch["run_id"], "rollback-thread",
                    dispatch_attempt_id=dispatch["dispatch_attempt_id"],
                )

        persisted = self.service.get_native_dispatch(dispatch["run_id"])
        self.assertEqual("claimed", persisted["status"])
        self.assertEqual("", persisted["thread_id"])
        self.assertEqual("claimed", self.service.get_task(task["id"])["status"])
        self.assertEqual("awaiting_thread", self.service.get_run(dispatch["run_id"])["status"])
        self.assertEqual([], self.service.list_conversations(task["id"]))

    def test_native_dispatch_titles_use_entity_id_and_stage_label(self):
        title = self.service._native_dispatch_title
        cases = (
            ("execution", "TASK-0001", "[DoTaks] TASK-0001 开发"),
            ("rework", "TASK-0001", "[DoTaks] TASK-0001 返工"),
            ("bugfix", "BUG-0001", "[DoTaks] BUG-0001 Bug 修复"),
            ("code_review", "TASK-0001", "[DoTaks] TASK-0001 Code Review"),
            ("requirement_decomposition", "REQ-0001", "[DoTaks] REQ-0001 需求拆解"),
        )
        for role, entity_id, expected in cases:
            entity_key = "requirement" if role == "requirement_decomposition" else "task"
            with self.subTest(role=role):
                self.assertEqual(
                    expected,
                    title({"run": {"run_type": role}, entity_key: {"id": entity_id}}),
                )

        self.assertEqual(
            "检查本机性能",
            title({
                "run": {
                    "run_type": "execution",
                    "execution_environment": "projectless",
                },
                "task": {
                    "id": "TASK-0002",
                    "title": "检查本机性能",
                    "project": None,
                },
            }),
        )

    def test_native_controller_claim_is_serialized_across_service_processes(self):
        self.create_ready_task()
        self.create_located_task({
            "title": "实现第二项目入口",
            "project": str(self.other_project),
            "modules": ["b-page"],
            "goal": "增加第二项目入口",
            "scope": ["入口"],
            "out_of_scope": [],
            "acceptance_criteria": ["入口可用"],
        }, [{"file": "src/BPage.tsx", "symbols": ["BPage"], "reason": "second target"}])
        second_service = TaskboardService(self.temp.name)
        barrier = threading.Barrier(2)

        def claim(service: TaskboardService) -> dict:
            barrier.wait()
            return service._claim_next_native_dispatch("codex-native-controller", stage="development")

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(claim, (self.service, second_service)))

        self.assertEqual(1, len({result["run_id"] for result in results}))
        with self.service.db.connection() as connection:
            active_dispatches = connection.execute(
                """SELECT COUNT(*) value FROM native_dispatches
                   WHERE worker_id='codex-native-controller:development'
                     AND status IN ('claimed','pending_thread','bound')"""
            ).fetchone()["value"]
            active_runs = connection.execute(
                """SELECT COUNT(*) value FROM task_runs
                   WHERE claimed_by='codex-native-controller:development'
                     AND status IN ('awaiting_thread','running')"""
            ).fetchone()["value"]
        self.assertEqual((1, 1), (active_dispatches, active_runs))

    def test_native_controller_claims_independent_stage_lanes_concurrently(self):
        development_task = self.create_ready_task()
        review_task = self.create_located_task({
            "title": "审查第二项目入口",
            "project": str(self.other_project),
            "modules": ["review-page"],
            "goal": "验证第二项目入口",
            "scope": ["入口审查"],
            "out_of_scope": [],
            "acceptance_criteria": ["入口审查通过"],
        })
        review_task = self.submit_delivery(review_task, "review-delivery-thread")

        review_dispatch = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="code_review"
        )
        development_dispatch = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="development"
        )

        self.assertEqual(review_task["id"], review_dispatch["entity_id"])
        self.assertEqual("code_review", review_dispatch["role"])
        self.assertEqual(
            "codex-native-controller:code_review", review_dispatch["worker_id"]
        )
        self.assertEqual(development_task["id"], development_dispatch["entity_id"])
        self.assertEqual("execution", development_dispatch["role"])
        self.assertEqual(
            "codex-native-controller:development",
            development_dispatch["worker_id"],
        )
        self.assertEqual(
            review_dispatch["run_id"],
            self.service._claim_next_native_dispatch(
                "codex-native-controller", stage="code_review"
            )["run_id"],
        )
        self.assertEqual(
            development_dispatch["run_id"],
            self.service._claim_next_native_dispatch(
                "codex-native-controller", stage="development"
            )["run_id"],
        )
        with self.assertRaisesRegex(ValueError, "stage must be"):
            self.service._claim_next_native_dispatch(
                "codex-native-controller", stage="acceptance"
            )
        with self.assertRaisesRegex(ValueError, "stage must be"):
            self.service._claim_next_native_dispatch(
                "codex-native-controller", stage="review"
            )

        with self.service.db.connection() as connection:
            active = connection.execute(
                """SELECT COUNT(*) value FROM native_dispatches
                   WHERE worker_id LIKE 'codex-native-controller:%'
                     AND status IN ('claimed','pending_thread','bound')"""
            ).fetchone()["value"]
        self.assertEqual(2, active)

    def test_database_rejects_two_active_dispatches_for_one_worker(self):
        self.create_ready_task()
        dispatch = self.service._claim_next_native_dispatch("codex-native-controller", stage="development")
        with self.assertRaisesRegex(
            sqlite3.IntegrityError,
            "active native dispatch already exists for worker",
        ), self.service.db.transaction() as connection:
            connection.execute(
                """INSERT INTO native_dispatches(
                       run_id, entity_type, entity_id, role, worker_id,
                       project_path, dispatch_title, dispatch_prompt,
                       dispatch_attempt_id
                   ) VALUES(?, 'task', 'TASK-OTHER', 'execution', ?, '',
                            'duplicate', 'duplicate', 'ATTEMPT-DUPLICATE')""",
                ("RUN-DUPLICATE", dispatch["worker_id"]),
            )

    def test_native_retry_prefers_original_thread_and_records_safe_fallback(self):
        task = self.create_ready_task()
        first = self.service._claim_next_native_dispatch("codex-native-controller", stage="development")
        self.service.bind_native_dispatch(
            first["run_id"], "native-thread-1",
            dispatch_attempt_id=first["dispatch_attempt_id"],
        )
        self.service.fail_native_dispatch(first["run_id"], "worker ended without callback")
        self.service.transition_task(task["id"], "ready")

        retry = self.service._claim_next_native_dispatch("codex-native-controller", stage="development")
        self.assertNotEqual(first["run_id"], retry["run_id"])
        self.assertEqual("native-thread-1", retry["resume_thread_id"])
        replacement = self.service.bind_native_dispatch(
            retry["run_id"], "native-thread-2",
            resume_fallback_reason="native-thread-1 is unavailable",
            dispatch_attempt_id=retry["dispatch_attempt_id"],
        )
        self.assertEqual("native-thread-1 is unavailable", replacement["resume_fallback_reason"])
        self.assertEqual("native-thread-2", self.service.get_task(task["id"])["codex_thread_id"])

    def test_task_token_budget_setting_controls_new_tasks(self):
        existing = self.create_ready_task()
        self.assertEqual(60000, existing["token_budget"])
        self.assertEqual(
            {
                "task_token_budget": 60000,
                "max_batch_appended_tasks": 3,
                "parallel_development_enabled": False,
                "max_parallel_development": 2,
            },
            self.service.task_settings(),
        )

        self.assertEqual(
            {
                "task_token_budget": 120000,
                "max_batch_appended_tasks": 3,
                "parallel_development_enabled": False,
                "max_parallel_development": 2,
            },
            self.service.update_task_settings({"task_token_budget": 120000}),
        )
        created = self.create_ready_task()
        self.assertEqual(120000, created["token_budget"])
        self.assertEqual(60000, self.service.get_task(existing["id"])["token_budget"])
        self.assertEqual(
            {
                "task_token_budget": 120000,
                "max_batch_appended_tasks": 3,
                "parallel_development_enabled": False,
                "max_parallel_development": 2,
            },
            TaskboardService(self.temp.name).task_settings(),
        )

        self.assertEqual(
            {
                "task_token_budget": 120000,
                "max_batch_appended_tasks": 5,
                "parallel_development_enabled": False,
                "max_parallel_development": 2,
            },
            self.service.update_task_settings(
                {"task_token_budget": 120000, "max_batch_appended_tasks": 5}
            ),
        )

    def test_task_token_budget_setting_rejects_invalid_values(self):
        for value in (0, -1, True, "120000"):
            with self.subTest(value=value), self.assertRaisesRegex(
                ValueError, "positive integer"
            ):
                self.service.update_task_settings({"task_token_budget": value})

        for value in (-1, 21, True, "3"):
            with self.subTest(batch_value=value), self.assertRaisesRegex(
                ValueError, "integer between 0 and 20"
            ):
                self.service.update_task_settings(
                    {"task_token_budget": 60000, "max_batch_appended_tasks": value}
                )

        for value in (0, 9, True, "2"):
            with self.subTest(parallel_value=value), self.assertRaisesRegex(
                ValueError, "integer between 1 and 8"
            ):
                self.service.update_task_settings(
                    {"task_token_budget": 60000, "max_parallel_development": value}
                )
        with self.assertRaisesRegex(ValueError, "must be boolean"):
            self.service.update_task_settings(
                {"task_token_budget": 60000, "parallel_development_enabled": 1}
            )

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
        project = payload["project"]
        located_targets = targets or [{"file": "src/APage.tsx", "symbols": ["APage"], "reason": "primary target"}]
        located_targets = [
            {
                **target,
                "mode": target.get("mode", "modify"),
                "tasks": target.get("tasks") or [{
                    "symbol": (target.get("symbols") or [""])[0],
                    "action": "apply the focused test change",
                }],
            }
            for target in located_targets
        ]
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
        }
        review_contract = {"checks": [{
            "id": "focused-review",
            "description": "Verify the focused change and acceptance criteria",
            "kind": "code",
        }], "quality_gates": {
            "code_review": {"required": True, "reason": "test code change"},
        }}
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
            {"targets": [{**target, "mode": "modify", "tasks": [{"symbol": "APage", "action": "replace addition with subtraction"}]}]},
            {
                "checks": [{"id": "subtraction-review", "description": "Verify subtraction behavior and regression coverage", "kind": "code"}],
                "quality_gates": {"code_review": {"required": True, "reason": "code change"}},
            },
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

    def complete_current_location(self, analysis_id, evidence, targets, acceptance_plan):
        normalized_targets = [
            {
                **target,
                "mode": target.get("mode", "modify"),
                "tasks": target.get("tasks") or [{
                    "symbol": (target.get("symbols") or [""])[0],
                    "action": "apply the focused test change",
                }],
            }
            for target in targets
        ]
        return self.service.complete_location_analysis(
            analysis_id,
            evidence,
            normalized_targets,
            acceptance_plan,
            {"decision": "independent"},
            {"targets": normalized_targets},
            {
                "checks": [{
                    "id": "focused-review",
                    "description": "Verify the focused change",
                    "kind": "code",
                }],
                "quality_gates": {
                    "code_review": {"required": True, "reason": "code change"},
                },
            },
        )

    def test_ready_task_requires_goal_project_and_acceptance(self):
        with self.assertRaisesRegex(ValueError, "missing"):
            self.service.create_task({
                "title": "信息不完整",
                "status": "ready",
            })

    def test_confirmed_task_enters_ready_queue_without_requirement(self):
        task = self.create_ready_task()
        self.assertEqual("ready", task["status"])
        self.assertIsNone(task["requirement_id"])
        board = self.service.board()
        self.assertEqual([], board["requirements"])
        self.assertEqual([str(self.example_project.resolve())], board["projects"])
        self.assertEqual(0, board["counts"]["attention"])

        self.service.transition_task(task["id"], "ready", auto_dispatch=False)
        self.assertEqual(1, self.service.board()["counts"]["attention"])

    def test_intake_result_requires_immediate_controller_kickoff_when_enabled(self):
        enabled = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "自动调度需求",
            "original_content": "创建后立即交给 Controller。",
            "project": str(self.example_project),
            "goal": "验证 Intake 到 Controller 的交接契约",
            "modules": ["planning"],
            "scope": ["调度交接"],
            "out_of_scope": [],
            "acceptance_criteria": ["返回强制 kickoff 指令"],
        })
        paused = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "暂停调度需求",
            "original_content": "仅创建，不自动调度。",
            "project": str(self.example_project),
            "goal": "验证关闭自动调度时不要求 kickoff",
            "modules": ["planning"],
            "scope": ["调度交接"],
            "out_of_scope": [],
            "acceptance_criteria": ["不返回 kickoff 指令"],
            "auto_dispatch": False,
        })

        self.assertTrue(enabled["controller_kickoff_required"])
        self.assertEqual(
            {
                "mode": "kickoff",
                "tool": "claim_schedule_cycle",
                "arguments": {
                    "worker_id": "codex-native-controller",
                    "force": True,
                    "lease_seconds": 7200,
                },
            },
            enabled["controller_kickoff"],
        )
        self.assertFalse(paused["controller_kickoff_required"])
        self.assertIsNone(paused["controller_kickoff"])

    def test_requirement_intake_allows_no_project(self):
        result = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "无项目需求",
            "goal": "先记录需求，暂不归属代码项目",
            "project": "",
            "auto_dispatch": True,
        })

        requirement = self.service.get_requirement(result["requirement_id"])["requirement"]
        self.assertIsNone(requirement["project"])
        self.assertFalse(requirement["auto_dispatch"])
        self.assertFalse(result["controller_kickoff_required"])
        self.assertEqual([], self.service.board()["projects"])

    def test_page_task_enters_queue_and_reuses_its_id_after_location(self):
        queued = self.service.enqueue_task_intake({
            "title": "页面新增任务",
            "type": "feature",
            "project": str(self.example_project),
            "goal": "从页面加入任务队列",
            "priority": "P1",
            "auto_dispatch": True,
        })
        task_id = queued["task_id"]
        requirement_id = queued["requirement_id"]
        task = self.service.get_task(task_id)
        self.assertEqual("draft", task["status"])
        self.assertEqual(requirement_id, task["requirement_id"])
        self.assertEqual([], self.service.board()["requirements"])
        self.assertEqual([task_id], [item["id"] for item in self.service.board()["tasks"]])
        self.assertTrue(queued["controller_kickoff_required"])

        claim = self.service.claim_next_task("planner", str(self.example_project))
        self.assertEqual(requirement_id, claim["requirement"]["id"])
        self.assertEqual("web_task", claim["requirement"]["source_type"])
        analysis = self.service.prepare_location_analysis({
            "title": "页面新增任务",
            "goal": "从页面加入任务队列",
            "project": str(self.example_project),
            "modules": [],
        })
        completed = self.service.submit_requirement_decomposition(
            requirement_id,
            claim["run"]["id"],
            [{
                "key": "direct",
                "title": "模型不得改写此标题",
                "goal": "模型不得改写此目标",
                "analysis_id": analysis["analysis_id"],
                "location_evidence": {
                    "tool": "codegraph_explore",
                    "query": "页面新增任务",
                    "files": ["src/APage.tsx"],
                    "symbols": ["APage"],
                },
                "targets": [{
                    "file": "src/APage.tsx",
                    "mode": "modify",
                    "symbols": ["APage"],
                    "tasks": [{"symbol": "APage", "action": "实现页面新增任务"}],
                }],
                "quality_gates": {
                    "code_review": {"required": True, "reason": "code change"},
                },
                "acceptance_plan": [{
                    "criterion": "页面任务可执行",
                    "file": "src/APage.tsx",
                    "symbol": "APage",
                    "method": "focused test",
                    "expected": "页面任务可执行",
                }],
            }],
        )

        self.assertEqual([task_id], [item["id"] for item in completed["tasks"]])
        ready = self.service.get_task(task_id)
        self.assertEqual("ready", ready["status"])
        self.assertEqual("页面新增任务", ready["title"])
        self.assertEqual("从页面加入任务队列", ready["goal"])
        self.assertTrue(ready["auto_dispatch"])

    def test_page_task_without_project_dispatches_as_projectless_codex_task(self):
        queued = self.service.enqueue_task_intake({
            "title": "检查本机性能",
            "goal": "检查本机是否存在性能问题",
            "project": "",
            "auto_dispatch": True,
        })

        task = self.service.get_task(queued["task_id"])
        self.assertEqual("ready", task["status"])
        self.assertIsNone(task["project"])
        self.assertTrue(task["auto_dispatch"])
        self.assertIsNone(task["requirement_id"])
        self.assertEqual("projectless", task["location_context"]["mode"])
        self.assertTrue(queued["controller_kickoff_required"])

        claimed = self.service.claim_next_task("planner")
        self.assertEqual(task["id"], claimed["task"]["id"])
        self.assertEqual(
            "projectless", claimed["run"]["execution_environment"]
        )
        self.assertIn("请完成以下无项目任务", claimed["dispatch_prompt"])
        self.assertIn("changed_locations 必须传空数组", claimed["dispatch_prompt"])

        run = claimed["run"]
        self.service.bind_conversation(
            task["id"], "execution", "projectless-thread", run["id"]
        )
        self.service.transition_task(task["id"], "implementing")
        delivered = self.service.submit_delivery(
            run["id"],
            "已完成本机性能检查",
            "检查结果已在 Codex 会话中给出",
            [],
            [{
                "criterion": "检查本机是否存在性能问题",
                "status": "passed",
                "evidence": "已完成检查并说明结论",
            }],
        )
        self.assertEqual("done", delivered["task"]["status"])

    def test_page_task_without_project_can_be_saved_without_auto_dispatch(self):
        queued = self.service.enqueue_task_intake({
            "title": "稍后执行",
            "goal": "先记录无项目任务",
            "project": "",
            "auto_dispatch": False,
        })

        task = self.service.get_task(queued["task_id"])
        self.assertEqual("ready", task["status"])
        self.assertFalse(task["auto_dispatch"])
        self.assertFalse(queued["controller_kickoff_required"])
        self.assertIsNone(self.service.claim_next_task("planner"))

    def test_project_read_only_task_allows_optional_contract_fields_to_be_empty(self):
        payload = {
            "title": "检查页面标题",
            "project": str(self.example_project),
            "goal": "确认页面标题内容",
            "auto_dispatch": True,
        }
        analysis = self.service.prepare_location_analysis(payload)
        evidence = {
            "tool": "codegraph_explore",
            "query": "页面标题",
            "files": ["index.html"],
            "symbols": [],
        }

        created = self.service.finalize_task_intake({
            "intake_kind": "task",
            "analysis_id": analysis["analysis_id"],
            **payload,
            "location_evidence": evidence,
            "quality_gates": {
                "code_review": {
                    "required": False,
                    "reason": "只读取项目信息，不修改代码",
                },
            },
            "acceptance_plan": [{
                "criterion": "确认页面标题内容",
                "method": "读取页面标题并核对",
                "expected": "返回明确的页面标题",
                "check_type": "static_review",
            }],
        })

        task = self.service.get_task(created["task_id"])
        self.assertEqual([], task["implementation_contract"]["targets"])
        self.assertEqual([], task["review_contract"]["checks"])
        self.assertFalse(
            task["review_contract"]["quality_gates"]["code_review"]["required"]
        )
        self.assertEqual([], task["scope"])
        self.assertEqual([], task["out_of_scope"])
        self.assertNotIn("file", task["acceptance_plan"][0])

        claimed = self.service.claim_next_task("read-only-worker", task["project"])
        self.assertIn("请完成以下项目只读任务", claimed["dispatch_prompt"])
        self.assertIn("changed_locations 必须传空数组", claimed["dispatch_prompt"])
        run_context = json.loads(
            claimed["dispatch_prompt"].split("RUN_CONTEXT_JSON=", 1)[1]
        )
        self.assertEqual({"requires_changes": False}, run_context["delivery"])
        self.assertNotIn("targets", run_context)

        run = claimed["run"]
        self.service.bind_conversation(
            task["id"], "execution", "read-only-thread", run["id"]
        )
        self.service.transition_task(task["id"], "implementing")
        delivered = self.service.submit_delivery(
            run["id"],
            "已确认页面标题",
            "只读核对完成，未修改项目文件",
            [],
            [{
                "criterion": "确认页面标题内容",
                "status": "passed",
                "evidence": "已读取并返回明确标题",
            }],
        )
        self.assertEqual("done", delivered["task"]["status"])
        self.assertEqual([], delivered["run"]["changed_locations"])

    def test_code_changing_task_still_requires_changed_locations(self):
        task = self.create_ready_task()
        claimed = self.service.claim_next_task("code-worker", task["project"])
        run = claimed["run"]
        self.service.bind_conversation(
            task["id"], "execution", "code-thread", run["id"]
        )
        self.service.transition_task(task["id"], "implementing")

        with self.assertRaisesRegex(
            ValueError, "Code-changing deliveries require changed_locations"
        ):
            self.service.submit_delivery(
                run["id"],
                "完成代码修改",
                "验证通过",
                [],
                [{
                    "criterion": criterion,
                    "status": "passed",
                    "evidence": "验证通过",
                } for criterion in task["acceptance_criteria"]],
            )

    def test_page_task_dispatch_prompt_requires_one_direct_task(self):
        prompt = self.service._native_dispatch_prompt({
            "kind": "requirement_decomposition",
            "requirement": {"id": "REQ-0001", "source_type": "web_task"},
            "run": {"id": "RDRUN-0001", "run_type": "requirement_decomposition"},
        })
        self.assertIn("只提交一个 key 为 direct 的任务", prompt)
        self.assertIn("使现有草稿进入 ready 队列", prompt)

    def test_requirement_is_queryable_and_decomposition_is_idempotent(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "导入能力升级",
            "original_content": "先增加解析，再接入导入入口。",
            "project": str(self.example_project),
            "goal": "提供完整导入能力",
            "modules": ["import"],
            "scope": ["解析", "入口"],
            "out_of_scope": ["历史回填"],
            "acceptance_criteria": ["用户可完成导入"],
        })
        self.assertEqual("requirement", intake["intake_kind"])
        requirement_id = intake["requirement_id"]
        board_requirement = self.service.board()["requirements"][0]
        self.assertEqual(requirement_id, board_requirement["id"])
        self.assertEqual(0, board_requirement["child_task_count"])
        self.assertTrue(board_requirement["auto_dispatch"])
        self.assertEqual([str(self.example_project.resolve())], self.service.board()["projects"])
        initial = self.service.get_requirement(requirement_id)
        self.assertEqual("ready", initial["requirement"]["status"])
        self.assertEqual([], initial["tasks"])

        first = self.service.claim_next_task("planner", str(self.example_project))
        self.assertEqual("requirement_decomposition", first["kind"])
        self.service.fail_requirement_decomposition(
            requirement_id, first["run"]["id"], "temporary failure",
        )
        retry = self.service.claim_next_task("planner", str(self.example_project))
        self.assertEqual(first["requirement"]["id"], retry["requirement"]["id"])

        with self.assertRaisesRegex(ValueError, "requires analysis_id"):
            self.service.submit_requirement_decomposition(
                requirement_id, retry["run"]["id"],
                [{"key": "invalid", "title": "不可执行子任务", "goal": "缺少定位契约"}],
            )

        incomplete = self.service.get_requirement(requirement_id)
        self.assertEqual("decomposing", incomplete["requirement"]["status"])
        self.assertEqual([], incomplete["tasks"])

        def child_spec(key, title, goal, criterion, file, depends_on=None):
            analysis = self.service.prepare_location_analysis({
                "title": title, "goal": goal,
                "project": str(self.example_project), "modules": ["import"],
            })
            target = {"file": file, "symbols": [key], "reason": "decomposed task target"}
            return {
                "key": key, "title": title, "goal": goal,
                "acceptance_criteria": [criterion],
                "analysis_id": analysis["analysis_id"],
                "location_evidence": {
                    "tool": "codegraph_explore", "query": key,
                    "files": [file], "symbols": [key],
                },
                "targets": [{**target, "mode": "modify", "tasks": [{"symbol": key, "action": goal}]}],
                "quality_gates": {
                    "code_review": {"required": True, "reason": "code change"},
                },
                "acceptance_plan": [{
                    "criterion": criterion, "file": file, "symbol": key,
                    "method": "focused test", "expected": criterion,
                    "check_type": "static_review",
                }],
                "depends_on": depends_on or [],
            }

        child_specs = [
            child_spec("parse", "实现解析", "解析导入文件", "文件可解析", "src/Parser.py"),
            child_spec("entry", "接入入口", "从页面启动导入", "入口可用", "src/Entry.py", ["parse"]),
        ]
        invalid_specs = [
            child_specs[0],
            {
                **child_specs[1],
                "location_evidence": {
                    "tool": "codegraph_cli_explore",
                    "argv": [
                        "codegraph", "explore", "--path",
                        str(self.example_project), "entry",
                    ],
                    "exit_code": 0,
                    "query": "entry",
                    "files": ["src/Entry.py"],
                    "symbols": ["entry"],
                },
            },
        ]
        with self.assertRaisesRegex(
            ValueError, r"Decomposed task entry.*location_evidence.*evidence\.command",
        ):
            self.service.submit_requirement_decomposition(
                requirement_id, retry["run"]["id"], invalid_specs,
            )
        preflight_failure = self.service.get_requirement(requirement_id)
        self.assertEqual([], preflight_failure["tasks"])

        completed = self.service.submit_requirement_decomposition(
            requirement_id, retry["run"]["id"], child_specs,
        )
        repeated = self.service.submit_requirement_decomposition(
            requirement_id, retry["run"]["id"], child_specs,
        )
        self.assertEqual("decomposed", completed["requirement"]["status"])
        self.assertEqual(2, len(completed["tasks"]))
        self.assertEqual(
            [item["id"] for item in completed["tasks"]],
            [item["id"] for item in repeated["tasks"]],
        )
        self.assertTrue(all(item["requirement_id"] == requirement_id for item in completed["tasks"]))
        expected_children = {
            "parse": ("解析导入文件", "文件可解析"),
            "entry": ("从页面启动导入", "入口可用"),
        }
        for item in completed["tasks"]:
            expected_goal, expected_criterion = expected_children[item["requirement_task_key"]]
            self.assertEqual(expected_goal, item["goal"])
            self.assertEqual(["import"], item["modules"])
            self.assertEqual([expected_goal], item["scope"])
            self.assertIn("历史回填", item["out_of_scope"])
            self.assertEqual([expected_criterion], item["acceptance_criteria"])
            self.assertNotIn("用户可完成导入", item["acceptance_criteria"])
            self.assertEqual([expected_criterion], [plan["criterion"] for plan in item["acceptance_plan"]])
            self.assertTrue(item["location_context"]["targets"])
            self.assertTrue(item["implementation_contract"]["targets"][0]["tasks"])
            self.assertTrue(all(plan["file"] for plan in item["acceptance_plan"]))
            self.assertEqual(
                ["code-quality", "security-vulnerabilities", "cohesion-coupling"],
                [check["id"] for check in item["review_contract"]["checks"]],
            )
            self.assertTrue(item["auto_dispatch"])
        self.assertEqual(1, len(completed["relations"]))
        self.assertEqual("depends_on", completed["relations"][0]["relation_type"])

    def test_requirement_decomposition_accepts_minimal_read_only_child(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "核对页面信息",
            "project": str(self.example_project),
            "goal": "确认页面当前展示信息",
        })
        claimed = self.service.claim_next_task("planner", str(self.example_project))
        analysis = self.service.prepare_location_analysis({
            "title": "核对页面标题",
            "project": str(self.example_project),
            "goal": "读取并返回页面标题",
        })
        evidence = {
            "tool": "codegraph_explore",
            "query": "页面标题",
            "files": ["index.html"],
            "symbols": [],
        }

        decomposed = self.service.submit_requirement_decomposition(
            intake["requirement_id"],
            claimed["run"]["id"],
            [{
                "key": "read-title",
                "title": "核对页面标题",
                "goal": "读取并返回页面标题",
                "analysis_id": analysis["analysis_id"],
                "location_evidence": evidence,
                "quality_gates": {
                    "code_review": {
                        "required": False,
                        "reason": "只读取项目信息，不修改代码",
                    },
                },
                "acceptance_plan": [{
                    "criterion": "读取并返回页面标题",
                    "method": "读取页面标题并核对",
                    "expected": "返回明确标题",
                    "check_type": "static_review",
                }],
            }],
        )

        child = decomposed["tasks"][0]
        self.assertEqual([], child["implementation_contract"]["targets"])
        self.assertEqual([], child["review_contract"]["checks"])
        self.assertFalse(
            child["review_contract"]["quality_gates"]["code_review"]["required"]
        )
        self.assertEqual("ready", child["status"])

    def test_requirement_board_exposes_latest_bound_decomposition_thread(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "打开拆解会话",
            "project": str(self.example_project),
            "goal": "从需求卡片跳转 Codex",
        })
        dispatch = self.service._claim_next_native_dispatch(
            "codex-native-controller", stage="development"
        )
        self.assertEqual(intake["requirement_id"], dispatch["entity_id"])
        self.service.bind_native_dispatch(
            dispatch["run_id"], "thread-requirement-latest",
            dispatch_attempt_id=dispatch["dispatch_attempt_id"],
        )

        requirement = next(
            item for item in self.service.board()["requirements"]
            if item["id"] == intake["requirement_id"]
        )
        self.assertEqual("thread-requirement-latest", requirement["codex_thread_id"])

    def test_requirement_can_be_deleted_without_deleting_child_tasks(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "可删除需求",
            "project": str(self.example_project),
            "goal": "验证需求删除",
        })
        task = self.create_ready_task()
        with self.service.db.transaction() as connection:
            connection.execute(
                """UPDATE tasks SET requirement_id=?, requirement_task_key='child'
                   WHERE id=?""",
                (intake["requirement_id"], task["id"]),
            )

        result = self.service.delete_requirement(intake["requirement_id"])

        self.assertEqual("deleted", result["status"])
        self.assertEqual(1, result["preserved_task_count"])
        with self.assertRaisesRegex(KeyError, "Requirement not found"):
            self.service.get_requirement(intake["requirement_id"])
        preserved = self.service.get_task(task["id"])
        self.assertIsNone(preserved["requirement_id"])
        self.assertIsNone(preserved["requirement_task_key"])

    def test_completed_task_can_be_deleted_while_codex_thread_is_left_external(self):
        queued = self.service.enqueue_task_intake({
            "title": "删除已完成任务",
            "goal": "验证完成任务删除",
            "project": "",
            "auto_dispatch": True,
        })
        task_id = queued["task_id"]
        claim = self.service.claim_next_task("delete-test-worker")
        run_id = claim["run"]["id"]
        self.service.bind_conversation(
            task_id, "execution", "external-codex-thread", run_id
        )
        self.service.transition_task(task_id, "implementing")
        self.service.submit_delivery(
            run_id,
            "删除功能验证完成",
            "结果已确认",
            [],
            [{
                "criterion": "验证完成任务删除",
                "status": "passed",
                "evidence": "已完成",
            }],
        )

        result = self.service.delete_task(task_id)

        self.assertEqual({"status": "deleted", "task_id": task_id}, result)
        with self.assertRaisesRegex(KeyError, "Task not found"):
            self.service.get_task(task_id)
        with self.service.db.connection() as connection:
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_runs WHERE task_id=?", (task_id,)
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                connection.execute(
                    "SELECT COUNT(*) FROM task_conversations WHERE task_id=?",
                    (task_id,),
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                connection.execute(
                    """SELECT COUNT(*) FROM events
                       WHERE entity_type='task' AND entity_id=?
                         AND event_type='deleted'""",
                    (task_id,),
                ).fetchone()[0],
            )

    def test_non_completed_task_cannot_be_deleted(self):
        task = self.create_ready_task()

        with self.assertRaisesRegex(ValueError, "Only completed tasks"):
            self.service.delete_task(task["id"])
        self.assertEqual(task["id"], self.service.get_task(task["id"])["id"])

    def test_failed_requirement_can_be_manually_redecomposed(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "手动重新拆解",
            "project": str(self.example_project),
            "goal": "重置拆解失败次数并重新入队",
        })
        requirement_id = intake["requirement_id"]
        for attempt in range(1, 4):
            claim = self.service.claim_next_task("planner", str(self.example_project))
            self.service.fail_requirement_decomposition(
                requirement_id, claim["run"]["id"], f"failure {attempt}",
            )

        result = self.service.redecompose_requirement(requirement_id)

        self.assertEqual("ready", result["requirement"]["status"])
        self.assertEqual(0, result["requirement"]["decomposition_attempts"])
        self.assertEqual([], result["decomposition_runs"])
        self.assertTrue(result["controller_kickoff_required"])
        retry = self.service.claim_next_task("planner", str(self.example_project))
        self.assertEqual(requirement_id, retry["requirement"]["id"])
        self.assertEqual(1, retry["requirement"]["decomposition_attempts"])

    def test_redecomposition_rejects_started_child_task(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "不可重复需求",
            "project": str(self.example_project),
            "goal": "避免重复执行任务",
        })
        task = self.create_ready_task()
        with self.service.db.transaction() as connection:
            connection.execute(
                """UPDATE tasks SET requirement_id=?, requirement_task_key='child',
                   status='implementing' WHERE id=?""",
                (intake["requirement_id"], task["id"]),
            )

        with self.assertRaisesRegex(ValueError, "already started"):
            self.service.redecompose_requirement(intake["requirement_id"])

    def test_redecomposition_cancels_and_detaches_unstarted_child_tasks(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "替换未执行拆分任务",
            "project": str(self.example_project),
            "goal": "重新生成拆分任务",
        })
        task = self.create_ready_task()
        with self.service.db.transaction() as connection:
            connection.execute(
                """UPDATE tasks SET requirement_id=?, requirement_task_key='old-child'
                   WHERE id=?""",
                (intake["requirement_id"], task["id"]),
            )

        self.service.redecompose_requirement(intake["requirement_id"])

        replaced = self.service.get_task(task["id"])
        self.assertEqual("cancelled", replaced["status"])
        self.assertFalse(replaced["auto_dispatch"])
        self.assertIsNone(replaced["requirement_id"])
        self.assertIsNone(replaced["requirement_task_key"])

    def test_requirement_decomposition_failures_stop_after_three_attempts(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "拆解失败熔断",
            "original_content": "拆成多个任务。",
            "project": str(self.example_project),
            "goal": "验证需求拆解重试熔断",
            "modules": ["planning"],
            "scope": ["拆解"],
            "out_of_scope": [],
            "acceptance_criteria": ["不会无限重试"],
        })
        requirement_id = intake["requirement_id"]

        for attempt in range(1, 4):
            claim = self.service.claim_next_task("planner", str(self.example_project))
            self.assertEqual(attempt, claim["requirement"]["decomposition_attempts"])
            result = self.service.fail_requirement_decomposition(
                requirement_id, claim["run"]["id"], "persistent failure",
            )
            expected_status = "failed" if attempt == 3 else "ready"
            self.assertEqual(expected_status, result["requirement"]["status"])

        self.assertFalse(result["requirement"]["auto_dispatch"])
        self.assertIsNone(
            self.service.claim_next_task("planner", str(self.example_project))
        )

    def test_requirement_state_changes_create_scheduler_wakeups(self):
        intake = self.service.finalize_task_intake({
            "intake_kind": "requirement",
            "title": "调度唤醒需求",
            "original_content": "验证需求状态流转触发调度。",
            "project": str(self.example_project),
            "goal": "触发持久化调度检测",
            "modules": ["planning"],
            "scope": ["调度"],
            "out_of_scope": [],
            "acceptance_criteria": ["状态变化后调度器收到唤醒"],
        })
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE scheduler_state SET pending=0, handled_generation=generation WHERE id=1"
            )

        claim = self.service.claim_next_task("planner", str(self.example_project))
        self.assertTrue(self.service.scheduler_snapshot()["pending"])
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE scheduler_state SET pending=0, handled_generation=generation WHERE id=1"
            )
        self.service.fail_requirement_decomposition(
            intake["requirement_id"], claim["run"]["id"], "temporary failure",
        )
        self.assertTrue(self.service.scheduler_snapshot()["pending"])

    def test_schema_upgrade_preserves_tasks_and_adds_requirement_tracking(self):
        task = self.create_ready_task()
        db_path = self.service.db.path
        with sqlite3.connect(db_path) as connection:
            connection.execute("CREATE TABLE acceptance_check_runs(id INTEGER)")
            connection.execute("CREATE TABLE acceptance_results(id INTEGER)")
            connection.execute("CREATE TABLE batch_steer_events(id INTEGER)")
            connection.execute(
                "INSERT OR REPLACE INTO system_settings(key, value) VALUES('workspace_projects', '[]')"
            )
            connection.execute("PRAGMA user_version=12")
        upgraded = Database(db_path)
        with upgraded.connection() as connection:
            self.assertIsNotNone(connection.execute("SELECT 1 FROM tasks WHERE id=?", (task["id"],)).fetchone())
            tables = {row["name"] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
            retired_setting = connection.execute(
                "SELECT 1 FROM system_settings WHERE key='workspace_projects'"
            ).fetchone()
        self.assertIn("requirement_decomposition_runs", tables)
        self.assertIn("requirement_task_key", task_columns)
        self.assertTrue({
            "acceptance_check_runs", "acceptance_results", "batch_steer_events",
        }.isdisjoint(tables))
        self.assertIsNone(retired_setting)

    def test_task_ids_use_type_specific_prefixes_and_board_preserves_ids(self):
        task = self.create_ready_task()
        bug = self.create_located_task({
            "title": "修复导入入口", "type": " BUG ",
            "project": str(self.example_project), "modules": ["a-page"],
            "goal": "修复导入入口", "scope": ["修复按钮"],
            "out_of_scope": [], "acceptance_criteria": ["入口可用"],
        })

        self.assertRegex(task["id"], r"^TASK-\d+$")
        self.assertRegex(bug["id"], r"^BUG-\d+$")
        self.assertEqual("bug", bug["type"])
        self.assertEqual(task["id"], self.service.get_task(task["id"])["id"])
        self.assertEqual(bug["id"], self.service.get_task(bug["id"])["id"])
        board_ids = {item["id"] for item in self.service.board()["tasks"]}
        self.assertIn(task["id"], board_ids)
        self.assertIn(bug["id"], board_ids)

    def test_task_card_renders_persisted_task_id_without_rewriting_prefix(self):
        source = (
            Path(__file__).parents[1] / "web" / "src" / "taskboard-app.js"
        ).read_text(encoding="utf-8")
        task_card = source[
            source.index("function taskCard(task)"):
            source.index("function traceSection", source.index("function taskCard(task)"))
        ]

        self.assertIn("<span>${task.id}</span>", task_card)
        self.assertNotIn("TASK-", task_card)
        self.assertNotIn("BUG-", task_card)

    def test_failed_batch_review_opens_one_regroup_window_then_rework_seals_it(self):
        owner = self.create_located_task({
            "title": "返工批次任务一", "project": str(self.example_project),
            "goal": "实现任务一", "scope": ["任务一"], "out_of_scope": [],
            "acceptance_criteria": ["任务一通过"],
        })
        claim = self.service.claim_next_task("batch-worker", owner["project"])
        self.service.bind_conversation(owner["id"], "execution", "batch-thread", claim["run"]["id"])
        self.service.transition_task(owner["id"], "implementing")
        second = self.create_located_task({
            "title": "返工批次任务二", "project": str(self.example_project),
            "goal": "实现任务二", "scope": ["任务二"], "out_of_scope": [],
            "acceptance_criteria": ["任务二通过"],
        })
        batch = self.service.execution_batch(owner["id"])
        criteria = [f"[{owner['id']}] 任务一通过", f"[{second['id']}] 任务二通过"]
        self.service.submit_delivery(
            claim["run"]["id"], "实现完成", "tests passed",
            [{"file": "src/APage.tsx", "symbols": ["APage"], "summary": "batch"}],
            [{"criterion": item, "evidence": "passed"} for item in criteria],
            batch_revision=batch["revision"],
        )
        review = self.service.claim_next_code_review_task("review-worker", owner["project"])
        self.service.bind_conversation(owner["id"], "code_review", "review-thread", review["run"]["id"])
        checks = review["run"]["context_snapshot"]["review_checks"]
        self.service.review_code(
            owner["id"], review["run"]["id"], "fail", reasons=["需要修复"],
            passed_items=checks[1:], failed_criteria=checks[:1],
        )
        self.assertEqual("regrouping", self.service.execution_batch(owner["id"])["state"])
        third = self.create_located_task({
            "title": "返工批次任务三", "project": str(self.example_project),
            "goal": "实现任务三", "scope": ["任务三"], "out_of_scope": [],
            "acceptance_criteria": ["任务三通过"],
        })
        self.assertEqual(owner["id"], self.service.execution_batch(third["id"])["owner_task_id"])
        rework = self.service.claim_next_task("rework-worker", owner["project"])
        self.assertIsNotNone(rework, self.service.board())
        self.assertEqual("rework", rework["run"]["run_type"])
        sealed = self.service.execution_batch(owner["id"])
        self.assertEqual("development", sealed["state"])
        self.assertFalse(sealed["admission_open"])

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
        }
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE task_change_requests SET proposed_task=? WHERE id=?",
                (json.dumps(proposed, ensure_ascii=False), change["id"]),
            )

        with self.assertRaisesRegex(ValueError, "target must be an object"):
            self.service.resolve_task_change_confirmation(change["id"], "revise")

    def test_location_completion_rejects_string_contracts(self):
        task = self.create_ready_task()
        target = {"file": "src/APage.tsx", "symbols": ["APage"]}
        proposed = {
            "title": "A 页面修订", "project": task["project"],
            "goal": "调整 A 页面", "modules": ["a-page"],
        }
        analysis = self.service.prepare_location_analysis(proposed, "change", task["id"])

        with self.assertRaisesRegex(ValueError, "target must be an object"):
            self.service.complete_location_analysis(
                analysis["analysis_id"], {"query": "APage", "symbols": ["APage"]}, [target],
                [{
                    "criterion": "功能可用", "file": target["file"], "symbol": "APage",
                    "method": "focused test", "expected": "功能可用",
                }],
                {"decision": "independent"},
                {
                    "targets": ["src/APage.tsx::APage"],
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

        prompt = claimed["dispatch_prompt"]
        self.assertTrue(prompt.startswith("请完成以下任务：\n\n"))
        self.assertIn(f"标题：{task['title']}", prompt)
        self.assertIn(f"目标：{task['goal']}", prompt)
        self.assertIn("验收标准：", prompt)
        self.assertLess(prompt.index("请完成以下任务："), prompt.index("$dotasks-lifecycle"))
        self.assertLess(prompt.index("$dotasks-lifecycle"), prompt.index("RUN_CONTEXT_JSON="))
        self.assertIn(task["id"], prompt)
        self.assertIn(claimed["run"]["id"], prompt)
        self.assertIn("完成：调用 submit_task_delivery", prompt)
        self.assertIn("无法继续：调用 report_run_blocked", prompt)
        self.assertNotIn("以 RUN_CONTEXT_JSON 为唯一任务输入", prompt)
        self.assertNotIn("static_review/manual_runtime", prompt)
        run_context = json.loads(prompt.split("RUN_CONTEXT_JSON=", 1)[1].split("\n\n", 1)[0])
        self.assertEqual(
            {"execution_environment", "targets", "verify", "delivery"},
            set(run_context),
        )
        self.assertEqual({"requires_changes": True}, run_context["delivery"])
        self.assertEqual(
            {"file", "mode", "symbols"}, set(run_context["targets"][0])
        )
        self.assertNotIn("tasks", run_context["targets"][0])
        self.assertNotIn("reason", run_context["targets"][0])

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
        completed = self.complete_current_location(
            analysis["analysis_id"], {"query": "context"},
            [{"file": "src/A.ts", "symbols": ["A"]}],
            [{
                "criterion": "任务未创建", "file": "src/A.ts", "symbol": "A",
                "method": "unit test", "expected": "reject",
            }],
        )
        self.example_project.rmdir()
        with self.assertRaisesRegex(ValueError, "does not exist"):
            self.service.create_task({**payload, "status": "ready", "location_analysis_id": completed["id"]})
        self.assertIsNone(self.service.get_location_analysis(completed["id"])["consumed_at"])

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
        previous_home = os.environ.get("DOTASKS_HOME")
        os.environ["DOTASKS_HOME"] = str(configured_home)
        try:
            service = TaskboardService()
            self.assertEqual(configured_home.resolve(), service.data_home)
            self.assertEqual(configured_home.resolve() / "data" / "taskboard.db", service.db.path)
        finally:
            if previous_home is None:
                os.environ.pop("DOTASKS_HOME", None)
            else:
                os.environ["DOTASKS_HOME"] = previous_home

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
        completed = self.complete_current_location(
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
        with self.assertRaisesRegex(ValueError, r"evidence\.command"):
            self.service.report_location_status(
                project, True, "connected", "wrong field", {
                    **base,
                    "argv": ["codegraph", "explore", "--path", project, "Widget"],
                    "command": None,
                }, "codex-test",
            )
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
            self.complete_current_location(
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

    def test_parallel_development_claims_different_files_in_clean_git_worktrees(self):
        (self.example_project / "src").mkdir()
        (self.example_project / "src" / "A.ts").write_text("export const A = 1;\n")
        (self.example_project / "src" / "B.ts").write_text("export const B = 1;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(
            ["git", "config", "user.email", "dotasks@example.invalid"],
            cwd=self.example_project, check=True,
        )
        subprocess.run(
            ["git", "config", "user.name", "DoTasks Test"],
            cwd=self.example_project, check=True,
        )
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(
            ["git", "commit", "-m", "base"], cwd=self.example_project,
            check=True, capture_output=True,
        )
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        base = {
            "project": str(self.example_project), "modules": ["parallel"],
            "scope": ["one file"], "out_of_scope": [],
            "acceptance_criteria": ["updated"],
        }
        first = self.create_located_task(
            {**base, "title": "修改 A", "goal": "调整 A"},
            [{"file": "src/A.ts", "symbols": ["A"], "reason": "A"}],
        )
        second = self.create_located_task(
            {**base, "title": "修改 B", "goal": "调整 B"},
            [{"file": "src/B.ts", "symbols": ["B"], "reason": "B"}],
        )

        cycle = self.service.claim_schedule_cycle(
            "codex-native-controller", first["project"], force=True,
        )
        batch = cycle["development"]
        dispatches = batch["dispatches"]

        self.assertEqual(2, batch["capacity"])
        self.assertEqual(2, len(dispatches))
        self.assertEqual(
            {
                "codex-native-controller:development",
                "codex-native-controller:slot-2:development",
            },
            {dispatch["worker_id"] for dispatch in dispatches},
        )
        self.assertEqual({"worktree"}, {
            dispatch["execution_environment"] for dispatch in dispatches
        })
        self.assertTrue(all(dispatch["base_revision"] for dispatch in dispatches))
        self.assertTrue(all(
            dispatch["base_ref"].startswith("refs/heads/codex/dotasks-run-")
            for dispatch in dispatches
        ))
        self.assertEqual(
            {first["id"], second["id"]},
            {dispatch["entity_id"] for dispatch in dispatches},
        )
        self.assertIsNone(self.service.claim_next_task("worker-3", first["project"]))

    def test_parallel_development_keeps_same_file_serial(self):
        (self.example_project / "src").mkdir()
        (self.example_project / "src" / "Shared.ts").write_text("export const A = 1;\nexport const B = 2;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        base = {
            "project": str(self.example_project), "modules": ["parallel"],
            "scope": ["one symbol"], "out_of_scope": [],
            "acceptance_criteria": ["updated"],
        }
        first = self.create_located_task(
            {**base, "title": "修改 A", "goal": "调整 A"},
            [{"file": "src/Shared.ts", "symbols": ["A"], "reason": "A"}],
        )
        self.create_located_task(
            {**base, "title": "修改 B", "goal": "调整 B"},
            [{"file": "src/Shared.ts", "symbols": ["B"], "reason": "B"}],
        )
        self.assertIsNotNone(self.service.claim_next_task("worker-1", first["project"]))
        self.assertIsNone(self.service.claim_next_task("worker-2", first["project"]))

    def test_parallel_development_ignores_unrelated_dirty_workspace_files(self):
        (self.example_project / "src").mkdir()
        (self.example_project / "src" / "A.ts").write_text("export const A = 1;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        (self.example_project / "notes.txt").write_text("user change\n")
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        first = self.create_located_task({
            "title": "修改 A", "project": str(self.example_project),
            "modules": ["parallel"], "goal": "调整 A", "scope": ["A"],
            "out_of_scope": [], "acceptance_criteria": ["A updated"],
        }, [{"file": "src/A.ts", "symbols": ["A"], "reason": "A"}])
        second = self.create_located_task({
            "title": "增加 B", "project": str(self.example_project),
            "modules": ["parallel"], "goal": "增加 B", "scope": ["B"],
            "out_of_scope": [], "acceptance_criteria": ["B added"],
        }, [{"file": "src/B.ts", "mode": "create", "symbols": ["B"], "reason": "B"}])
        first_claim = self.service.claim_next_task("worker-1", first["project"])
        second_claim = self.service.claim_next_task("worker-2", second["project"])
        self.assertEqual("worktree", first_claim["run"]["execution_environment"])
        self.assertEqual("worktree", second_claim["run"]["execution_environment"])
        self.assertTrue(first_claim["run"]["base_revision"])
        self.assertTrue(
            first_claim["run"]["base_ref"].startswith(
                "refs/heads/codex/dotasks-run-"
            )
        )

    def test_parallel_development_skips_only_task_with_dirty_target(self):
        (self.example_project / "src").mkdir()
        (self.example_project / "src" / "A.ts").write_text("export const A = 1;\n")
        (self.example_project / "src" / "B.ts").write_text("export const B = 1;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        (self.example_project / "src" / "A.ts").write_text("export const A = 99;\n")
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        common = {
            "project": str(self.example_project), "modules": ["parallel"],
            "scope": ["one file"], "out_of_scope": [],
            "acceptance_criteria": ["updated"],
        }
        blocked = self.create_located_task(
            {**common, "title": "修改 A", "goal": "调整 A"},
            [{"file": "src/A.ts", "symbols": ["A"], "reason": "A"}],
        )
        eligible = self.create_located_task(
            {**common, "title": "修改 B", "goal": "调整 B"},
            [{"file": "src/B.ts", "symbols": ["B"], "reason": "B"}],
        )

        claim = self.service.claim_next_task("worker-1", str(self.example_project))

        self.assertEqual(eligible["id"], claim["task"]["id"])
        self.assertEqual("worktree", claim["run"]["execution_environment"])
        self.assertEqual("ready", self.service.get_task(blocked["id"])["status"])
        self.assertIsNone(
            self.service.claim_next_task("worker-2", str(self.example_project))
        )

    def test_project_exclusive_task_ignores_unrelated_dirty_workspace_file(self):
        (self.example_project / "migrations").mkdir()
        migration = self.example_project / "migrations" / "001.sql"
        migration.write_text("CREATE TABLE example(id INTEGER);\n")
        notes = self.example_project / "notes.txt"
        notes.write_text("clean\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        notes.write_text("unrelated user change\n")
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        task = self.create_located_task({
            "title": "更新迁移", "project": str(self.example_project),
            "modules": ["database"], "goal": "更新表结构", "scope": ["migration"],
            "out_of_scope": [], "acceptance_criteria": ["迁移可执行"],
        }, [{"file": "migrations/001.sql", "symbols": [], "reason": "schema"}])

        claim = self.service.claim_next_task("worker-1", task["project"])

        self.assertEqual(task["id"], claim["task"]["id"])
        self.assertEqual("worktree", claim["run"]["execution_environment"])

    def test_project_exclusive_task_reports_dirty_target_file(self):
        (self.example_project / "migrations").mkdir()
        migration = self.example_project / "migrations" / "001.sql"
        migration.write_text("CREATE TABLE example(id INTEGER);\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        migration.write_text("CREATE TABLE example(id INTEGER, name TEXT);\n")
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        task = self.create_located_task({
            "title": "更新迁移", "project": str(self.example_project),
            "modules": ["database"], "goal": "更新表结构", "scope": ["migration"],
            "out_of_scope": [], "acceptance_criteria": ["迁移可执行"],
        }, [{"file": "migrations/001.sql", "symbols": [], "reason": "schema"}])

        blockers = self.service.get_task(task["id"])["dispatch_blockers"]

        self.assertEqual(["workspace_target_dirty"], [item["code"] for item in blockers])
        self.assertEqual(["migrations/001.sql"], blockers[0]["files"])
        self.assertIsNone(self.service.claim_next_task("worker-1", task["project"]))

    def test_board_preview_does_not_initialize_git_integration_state(self):
        (self.example_project / "src").mkdir()
        (self.example_project / "src" / "A.ts").write_text("export const A = 1;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        self.create_located_task({
            "title": "读取看板", "project": str(self.example_project),
            "modules": ["app"], "goal": "验证只读预览", "scope": ["A"],
            "out_of_scope": [], "acceptance_criteria": ["看板可读取"],
        }, [{"file": "src/A.ts", "symbols": ["A"], "reason": "code"}])

        self.service.board()

        refs = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/heads/codex/dotasks-integration/"],
            cwd=self.example_project, check=True, capture_output=True, text=True,
        ).stdout.strip()
        with self.service.db.connection() as connection:
            integration_count = connection.execute(
                "SELECT COUNT(*) FROM project_integration_states"
            ).fetchone()[0]
        self.assertEqual("", refs)
        self.assertEqual(0, integration_count)

    def test_project_exclusive_lock_is_scoped_to_same_project(self):
        for project in (self.example_project, self.other_project):
            (project / "src").mkdir()
            (project / "src" / "A.ts").write_text("export const A = 1;\n")
            (project / "migrations").mkdir()
            (project / "migrations" / "001.sql").write_text("SELECT 1;\n")
            subprocess.run(["git", "init"], cwd=project, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=project, check=True)
            subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=project, check=True)
            subprocess.run(["git", "add", "."], cwd=project, check=True)
            subprocess.run(["git", "commit", "-m", "base"], cwd=project, check=True, capture_output=True)
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        exclusive = self.create_located_task({
            "title": "更新项目一迁移", "project": str(self.example_project),
            "modules": ["database"], "goal": "更新迁移", "scope": ["migration"],
            "out_of_scope": [], "acceptance_criteria": ["迁移可执行"],
        }, [{"file": "migrations/001.sql", "symbols": [], "reason": "schema"}])
        other = self.create_located_task({
            "title": "更新项目二代码", "project": str(self.other_project),
            "modules": ["app"], "goal": "更新代码", "scope": ["A"],
            "out_of_scope": [], "acceptance_criteria": ["代码已更新"],
        }, [{"file": "src/A.ts", "symbols": ["A"], "reason": "code"}])
        same_project = self.create_located_task({
            "title": "更新项目一代码", "project": str(self.example_project),
            "modules": ["app"], "goal": "更新代码", "scope": ["A"],
            "out_of_scope": [], "acceptance_criteria": ["代码已更新"],
        }, [{"file": "src/A.ts", "symbols": ["A"], "reason": "code"}])

        first_claim = self.service.claim_next_task("worker-1", exclusive["project"])
        second_claim = self.service.claim_next_task("worker-2", other["project"])

        self.assertEqual(exclusive["id"], first_claim["task"]["id"])
        self.assertEqual(other["id"], second_claim["task"]["id"])
        self.assertIsNone(
            self.service.claim_next_task("worker-3", same_project["project"])
        )
        self.assertIn(
            "project_exclusive_lock",
            {
                item["code"]
                for item in self.service.get_task(same_project["id"])["dispatch_blockers"]
            },
        )

    def test_ready_task_exposes_controller_pending_reason(self):
        task = self.create_ready_task()

        blockers = self.service.get_task(task["id"])["dispatch_blockers"]

        self.assertEqual(["controller_pending"], [item["code"] for item in blockers])
        self.assertIn("等待 Controller", blockers[0]["message"])

    def test_worktree_delivery_is_reviewed_then_integrated_into_project(self):
        (self.example_project / "src").mkdir()
        source = self.example_project / "src" / "A.ts"
        source.write_text("export const A = 1;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        (self.example_project / "notes.txt").write_text("keep user change\n")
        task = self.create_located_task({
            "title": "修改 A", "project": str(self.example_project),
            "modules": ["parallel"], "goal": "调整 A", "scope": ["A"],
            "out_of_scope": [], "acceptance_criteria": ["A updated"],
        }, [{"file": "src/A.ts", "symbols": ["A"], "reason": "A"}])
        claim = self.service.claim_next_task("worker-1", task["project"])
        worktree = Path(self.temp.name) / "worktree-a"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), claim["run"]["base_ref"]],
            cwd=self.example_project, check=True, capture_output=True,
        )
        (worktree / "src" / "A.ts").write_text("export const A = 2;\n")
        self.service.bind_conversation(
            task["id"], "execution", "parallel-thread", claim["run"]["id"]
        )
        self.service.transition_task(task["id"], "implementing")
        delivered = self.service.submit_delivery(
            claim["run"]["id"], "A updated", "focused check passed",
            [{"file": "src/A.ts", "symbols": ["A"], "summary": "updated"}],
            [{"criterion": "A updated", "status": "pending", "evidence": "review required"}],
            workspace_path=str(worktree),
        )
        self.assertEqual("code_review", delivered["task"]["status"])
        self.assertEqual("export const A = 1;\n", source.read_text())
        delivery_run = delivered["run"]
        self.assertEqual("pending", delivery_run["integration_status"])
        self.assertTrue(Path(delivery_run["artifact_path"]).is_file())

        review = self.service.claim_next_code_review_task("review-worker", task["project"])
        self.service.bind_conversation(
            task["id"], "code_review", "parallel-review", review["run"]["id"]
        )
        checks = ["focused-review"]
        completed = self.service.review_code(
            task["id"], review["run"]["id"], "pass",
            passed_items=checks, failed_criteria=[],
        )
        self.assertEqual("done", completed["status"])
        self.assertEqual("export const A = 2;\n", source.read_text())
        self.assertEqual(
            "keep user change\n", (self.example_project / "notes.txt").read_text()
        )
        self.assertEqual(
            "integrated",
            self.service.get_run(claim["run"]["id"])["integration_status"],
        )
        self.assertEqual(
            "synced",
            self.service.get_run(claim["run"]["id"])["workspace_sync_status"],
        )
        integrated_run = self.service.get_run(claim["run"]["id"])
        integration_ref = integrated_run["context_snapshot"][
            "workspace_baseline"
        ]["integration_ref"]
        integration_head = subprocess.run(
            ["git", "rev-parse", integration_ref],
            cwd=self.example_project, check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(integrated_run["integration_revision"], integration_head)

        next_task = self.create_located_task({
            "title": "增加 B", "project": str(self.example_project),
            "modules": ["parallel"], "goal": "增加 B", "scope": ["B"],
            "out_of_scope": [], "acceptance_criteria": ["B added"],
        }, [{"file": "src/B.ts", "mode": "create", "symbols": ["B"], "reason": "B"}])
        next_claim = self.service.claim_next_task("worker-2", next_task["project"])
        self.assertEqual("worktree", next_claim["run"]["execution_environment"])
        self.assertEqual(
            integrated_run["integration_revision"], next_claim["run"]["base_revision"]
        )

    def test_two_parallel_worktree_deliveries_integrate_serially(self):
        (self.example_project / "src").mkdir()
        for name in ("A", "B"):
            (self.example_project / "src" / f"{name}.ts").write_text(
                f"export const {name} = 1;\n"
            )
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        tasks = []
        for name in ("A", "B"):
            tasks.append(self.create_located_task({
                "title": f"修改 {name}", "project": str(self.example_project),
                "modules": ["parallel"], "goal": f"调整 {name}",
                "scope": [name], "out_of_scope": [],
                "acceptance_criteria": [f"{name} updated"],
            }, [{"file": f"src/{name}.ts", "symbols": [name], "reason": name}]))
        claims = [
            self.service.claim_next_task(f"worker-{index}", str(self.example_project))
            for index in (1, 2)
        ]
        for task, claim, name in zip(tasks, claims, ("A", "B")):
            worktree = Path(self.temp.name) / f"worktree-{name.lower()}"
            subprocess.run(
                ["git", "worktree", "add", "--detach", str(worktree), claim["run"]["base_ref"]],
                cwd=self.example_project, check=True, capture_output=True,
            )
            (worktree / "src" / f"{name}.ts").write_text(
                f"export const {name} = 2;\n"
            )
            self.service.bind_conversation(
                task["id"], "execution", f"thread-{name}", claim["run"]["id"]
            )
            self.service.transition_task(task["id"], "implementing")
            self.service.submit_delivery(
                claim["run"]["id"], f"{name} updated", "focused check passed",
                [{"file": f"src/{name}.ts", "symbols": [name], "summary": "updated"}],
                [{"criterion": f"{name} updated", "status": "pending", "evidence": "review required"}],
                workspace_path=str(worktree),
            )

        for task, name in zip(tasks, ("A", "B")):
            review = self.service.claim_next_code_review_task(
                f"review-{name}", str(self.example_project)
            )
            self.assertEqual(task["id"], review["task"]["id"])
            self.service.bind_conversation(
                task["id"], "code_review", f"review-thread-{name}", review["run"]["id"]
            )
            completed = self.service.review_code(
                task["id"], review["run"]["id"], "pass",
                passed_items=["focused-review"],
                failed_criteria=[],
            )
            self.assertEqual("done", completed["status"])

        self.assertEqual(
            "export const A = 2;\n",
            (self.example_project / "src" / "A.ts").read_text(),
        )
        self.assertEqual(
            "export const B = 2;\n",
            (self.example_project / "src" / "B.ts").read_text(),
        )

    def test_review_integrates_branch_but_defers_overlapping_workspace_sync(self):
        (self.example_project / "src").mkdir()
        source = self.example_project / "src" / "A.ts"
        source.write_text("export const A = 1;\n")
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(["git", "add", "."], cwd=self.example_project, check=True)
        subprocess.run(["git", "commit", "-m", "base"], cwd=self.example_project, check=True, capture_output=True)
        self.service.update_task_settings({
            "task_token_budget": 60000,
            "parallel_development_enabled": True,
            "max_parallel_development": 2,
            "max_batch_appended_tasks": 0,
        })
        task = self.create_located_task({
            "title": "修改 A", "project": str(self.example_project),
            "modules": ["parallel"], "goal": "调整 A", "scope": ["A"],
            "out_of_scope": [], "acceptance_criteria": ["A updated"],
        }, [{"file": "src/A.ts", "symbols": ["A"], "reason": "A"}])
        claim = self.service.claim_next_task("worker-1", task["project"])
        worktree = Path(self.temp.name) / "worktree-sync-pending"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), claim["run"]["base_revision"]],
            cwd=self.example_project, check=True, capture_output=True,
        )
        (worktree / "src" / "A.ts").write_text("export const A = 2;\n")
        self.service.bind_conversation(
            task["id"], "execution", "delivery-thread", claim["run"]["id"]
        )
        self.service.transition_task(task["id"], "implementing")
        self.service.submit_delivery(
            claim["run"]["id"], "A updated", "focused check passed",
            [{"file": "src/A.ts", "symbols": ["A"], "summary": "updated"}],
            [{"criterion": "A updated", "status": "pending", "evidence": "review required"}],
            workspace_path=str(worktree),
        )
        source.write_text("export const A = 99;\n")
        review = self.service.claim_next_code_review_task(
            "review-worker", str(self.example_project)
        )
        self.service.bind_conversation(
            task["id"], "code_review", "review-thread", review["run"]["id"]
        )

        completed = self.service.review_code(
            task["id"], review["run"]["id"], "pass",
            passed_items=["focused-review"], failed_criteria=[],
        )
        delivery_run = self.service.get_run(claim["run"]["id"])

        self.assertEqual("done", completed["status"])
        self.assertEqual("integrated", delivery_run["integration_status"])
        self.assertEqual("pending", delivery_run["workspace_sync_status"])
        self.assertIn("src/A.ts", delivery_run["workspace_sync_error"])
        self.assertEqual("export const A = 99;\n", source.read_text())
        integrated = subprocess.run(
            ["git", "show", f"{delivery_run['integration_revision']}:src/A.ts"],
            cwd=self.example_project, check=True, capture_output=True, text=True,
        ).stdout
        self.assertEqual("export const A = 2;\n", integrated)

    def test_isolated_delivery_patch_supports_new_files(self):
        subprocess.run(["git", "init"], cwd=self.example_project, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "dotasks@example.invalid"], cwd=self.example_project, check=True)
        subprocess.run(["git", "config", "user.name", "DoTasks Test"], cwd=self.example_project, check=True)
        subprocess.run(
            ["git", "commit", "--allow-empty", "-m", "base"],
            cwd=self.example_project, check=True, capture_output=True,
        )
        base = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=self.example_project,
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        worktree = Path(self.temp.name) / "worktree-new-file"
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(worktree), "HEAD"],
            cwd=self.example_project, check=True, capture_output=True,
        )
        (worktree / "src").mkdir()
        (worktree / "src" / "New.ts").write_text("export const New = true;\n")
        artifact_path, artifact_sha256 = self.service._capture_delivery_patch(
            "RUN-NEW", str(worktree), base, ["src/New.ts"]
        )
        self.assertTrue(artifact_sha256)
        checked = subprocess.run(
            ["git", "apply", "--check", artifact_path],
            cwd=self.example_project, capture_output=True, text=True,
        )
        self.assertEqual(0, checked.returncode, checked.stderr)

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
        self.assertNotIn("'acceptance'", trigger)

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

    def test_pausing_dispatcher_preserves_tasks_and_active_runs(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("worker", task["project"])
        self.service.bind_conversation(
            task["id"], "execution", "active-thread", claim["run"]["id"]
        )

        paused = self.service.pause_dispatcher()

        self.assertEqual({"dispatcher_enabled": False}, paused)
        self.assertFalse(self.service.dispatcher_enabled())
        self.assertEqual("implementing", self.service.get_task(task["id"])["status"])
        self.assertEqual("running", self.service.get_run(claim["run"]["id"])["status"])
        reloaded = TaskboardService(self.temp.name)
        self.assertFalse(reloaded.dispatcher_enabled())
        self.assertEqual("implementing", reloaded.get_task(task["id"])["status"])

    def test_interrupted_execution_is_auto_requeued_and_resumes_thread(self):
        task = self.create_ready_task()
        claimed = self.service.claim_next_task("worker", task["project"])
        run = claimed["run"]
        self.service.bind_conversation(task["id"], "execution", "failed-thread", run["id"])
        result = self.service.interrupt_unsubmitted_run(run["id"], "process exited")
        self.assertEqual("ready", result["task"]["status"])
        self.assertEqual("interrupted", result["run"]["status"])
        retried = self.service.claim_next_task("worker-2", task["project"])
        self.assertEqual(task["id"], retried["task"]["id"])
        self.assertEqual("failed-thread", retried["resume_thread_id"])

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

    def test_auto_requeued_retry_is_prioritized(self):
        first = self.create_ready_task()
        second = self.create_located_task({
            "title": "其他任务", "project": str(self.example_project), "goal": "修改其他文件", "scope": ["other"],
            "out_of_scope": [], "acceptance_criteria": ["other updated"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        claimed = self.service.claim_next_task("worker", first["project"])
        self.service.bind_conversation(first["id"], "execution", "failed-thread", claimed["run"]["id"])
        self.service.interrupt_unsubmitted_run(claimed["run"]["id"], "process exited")
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
        self.assertEqual("code_review", delivered["task"]["status"])
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
        completed = self.complete_current_location(
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

    def test_delivery_symbol_mismatch_is_actionable_and_retriable(self):
        task = self.create_located_task({
            "title": "新增 Token 格式化函数",
            "project": str(self.example_project),
            "goal": "新增紧凑格式化函数",
            "scope": ["新增 formatMetricTokenCount"],
            "out_of_scope": [],
            "acceptance_criteria": ["格式化函数可用"],
        }, [{
            "file": "web/src/ui-core.js",
            "symbols": ["formatMetricTokenCount"],
            "reason": "new formatter",
        }])
        dispatched = self.service.claim_next_task("worker", task["project"])
        run = dispatched["run"]
        self.service.bind_conversation(task["id"], "execution", "thread", run["id"])
        self.service.transition_task(task["id"], "implementing")

        with self.assertRaises(ValueError) as raised:
            self.service.submit_delivery(
                run["id"], "done", "tests passed",
                [{
                    "file": "web/src/ui-core.js",
                    "symbols": ["formatCompactTokenCount"],
                    "summary": "changed",
                }],
                [{"criterion": "格式化函数可用", "evidence": "passed"}],
            )

        message = str(raised.exception)
        self.assertIn('submitted=["formatCompactTokenCount"]', message)
        self.assertIn('allowed=["formatMetricTokenCount"]', message)
        self.assertIn("retry the same active run", message)
        self.assertEqual("implementing", self.service.get_task(task["id"])["status"])
        self.assertEqual("running", self.service.get_run(run["id"])["status"])

        delivered = self.service.submit_delivery(
            run["id"], "done", "tests passed",
            [{
                "file": "web/src/ui-core.js",
                "symbols": ["formatMetricTokenCount"],
                "summary": "changed",
            }],
            [{"criterion": "格式化函数可用", "evidence": "passed"}],
        )
        self.assertEqual("code_review", delivered["task"]["status"])
        self.assertEqual("waiting_review", delivered["run"]["status"])

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

    def test_stage_budget_preflight_pauses_before_creating_run(self):
        task = self.create_ready_task()
        with self.service.db.transaction() as connection:
            connection.execute(
                "UPDATE tasks SET effective_token_used=? WHERE id=?",
                (task["token_budget"] - 1, task["id"]),
            )
        with self.service.db.connection() as connection:
            before = connection.execute(
                "SELECT COUNT(*) count FROM task_runs WHERE task_id=?", (task["id"],)
            ).fetchone()["count"]

        self.assertIsNone(self.service.claim_next_task("worker", task["project"]))

        paused = self.service.get_task(task["id"])
        self.assertEqual("waiting_confirmation", paused["status"])
        self.assertFalse(paused["auto_dispatch"])
        self.assertIn("阶段预算预检未通过", paused["last_failure_reason"])
        with self.service.db.connection() as connection:
            after = connection.execute(
                "SELECT COUNT(*) count FROM task_runs WHERE task_id=?", (task["id"],)
            ).fetchone()["count"]
        self.assertEqual(before, after)

    def test_visual_reference_is_copied_into_managed_artifacts(self):
        source = Path(self.temp.name) / "temporary-reference.png"
        source.write_bytes(b"png-reference")

        managed = self.service._manage_visual_references(
            "LOC-visual", [{"path": str(source), "purpose": "match selector layout"}]
        )

        self.assertEqual(1, len(managed))
        self.assertTrue(managed[0]["artifact_id"].startswith("artifact://intake/LOC-visual/"))
        self.assertTrue(Path(managed[0]["path"]).is_file())
        source.unlink()
        self.assertTrue(Path(managed[0]["path"]).is_file())

    def test_creation_rejects_acceptance_plan_mismatch_without_consuming_location(self):
        payload = {
            "title": "验收契约", "project": str(self.example_project), "goal": "验证契约", "scope": ["contract"],
            "out_of_scope": [], "acceptance_criteria": ["标准 B"],
        }
        self.service.report_location_status(payload["project"], True, "connected", "ok", {"tool": "codegraph_explore", "query": "Contract", "files": ["src/Contract.ts"]}, "agent")
        analysis = self.service.prepare_location_analysis(payload)
        completed = self.complete_current_location(
            analysis["analysis_id"], {"query": "context"},
            [{"file": "src/Contract.ts", "symbols": ["Contract"]}],
            [{"criterion": "标准 A", "file": "src/Contract.ts", "symbol": "Contract", "method": "test", "expected": "pass"}],
        )
        with self.assertRaisesRegex(ValueError, "exactly match"):
            self.service.create_task({**payload, "status": "ready", "location_analysis_id": completed["id"]})
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
        self.assertEqual("code_review", delivered["task"]["status"])

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

    def test_auto_recovered_task_is_prioritized_before_project_queue(self):
        blocker = self.create_ready_task()
        waiting = self.create_located_task({
            "title": "等待项目解锁", "project": str(self.example_project), "goal": "修改其他位置", "scope": ["other"],
            "out_of_scope": [], "acceptance_criteria": ["other updated"],
        }, [{"file": "src/Other.tsx", "symbols": ["Other"]}])
        claim = self.service.claim_next_task("worker", blocker["project"])
        self.service.bind_conversation(blocker["id"], "execution", "blocked-project-thread", claim["run"]["id"])
        self.service.interrupt_unsubmitted_run(claim["run"]["id"], "crashed")
        resumed_queue = self.service.claim_next_task("worker-2", waiting["project"])
        self.assertEqual(blocker["id"], resumed_queue["task"]["id"])

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
        with self.assertRaisesRegex(ValueError, "Unknown task status"):
            self.service.transition_task(task["id"], "review")
        task = self.service.submit_delivery(
            claim["run"]["id"], "implemented", "tests passed",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": criterion, "evidence": "passed"} for criterion in task["acceptance_criteria"]],
        )["task"]
        review = self.service.claim_next_code_review_task("reviewer", task["project"])
        self.service.bind_conversation(
            task["id"], "code_review", "review-thread", review["run"]["id"]
        )
        with self.assertRaisesRegex(ValueError, "Unknown task status"):
            self.service.transition_task(task["id"], "completion")
        self.assertEqual("running", self.service.get_run(review["run"]["id"])["status"])

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
                self.complete_current_location(
                    analysis["analysis_id"], {"query": "context"},
                    [{"file": unsafe, "symbols": ["X"]}],
                    [{"criterion": "safe", "file": unsafe, "symbol": "X", "method": "test", "expected": "safe"}],
                )

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

    def test_run_conversation_role_must_match_run_type(self):
        task = self.create_ready_task()
        claim = self.service.claim_next_task("worker", task["project"])
        with self.assertRaisesRegex(ValueError, "must use conversation role execution"):
            self.service.bind_conversation(task["id"], "code_review", "wrong-thread", claim["run"]["id"])
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
        completed = self.complete_current_location(
            analysis["analysis_id"], {"query": "context"},
            [{"file": "src/A.ts", "symbols": ["A"]}],
            [{"criterion": "created", "file": "src/A.ts", "symbol": "A", "method": "test", "expected": "created"}],
        )
        self.service.obsidian.sync_task = lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("vault unavailable"))
        task = self.service.create_task({
            **payload,
            "status": "ready",
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
