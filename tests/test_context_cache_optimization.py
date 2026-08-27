from __future__ import annotations

import hashlib
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskboard.mcp_server import handle
from core.run_context import LIFECYCLE_TOOL_NAMES, TOOL_PROFILES, model_run_context
from core.service import TaskboardService


class ContextCacheOptimizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        (self.project / "src").mkdir(parents=True)
        (self.project / "src" / "APage.tsx").write_text(
            "export function APage() { return '智慧幼儿园' }\n", encoding="utf-8",
        )
        subprocess.run(["git", "init", "-q", str(self.project)], check=True)
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.project), "config", "user.name", "Test"],
            check=True,
        )
        subprocess.run(["git", "-C", str(self.project), "add", "."], check=True)
        subprocess.run(["git", "-C", str(self.project), "commit", "-qm", "initial"], check=True)
        self.previous_vault = os.environ.get("CODEX_TASKBOARD_OBSIDIAN_VAULT")
        os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = str(Path(self.temp.name) / "vault")
        self.service = TaskboardService(self.temp.name)
        self.service.set_dispatcher_enabled(True)

    def tearDown(self) -> None:
        if self.previous_vault is None:
            os.environ.pop("CODEX_TASKBOARD_OBSIDIAN_VAULT", None)
        else:
            os.environ["CODEX_TASKBOARD_OBSIDIAN_VAULT"] = self.previous_vault
        self.temp.cleanup()

    def create_task(
        self, *, separate_acceptance_session: bool = False, mechanical: bool = False,
        duplicate_acceptance_command: bool = False,
    ) -> dict:
        project = str(self.project)
        target = {"file": "src/APage.tsx", "symbols": ["APage"]}
        implementation_contract = {
            "targets": [target],
            "ordered_steps": [{
                "file": "src/APage.tsx", "symbol": "APage",
                "action": "将智慧幼儿园替换为快乐智慧园" if mechanical else "修改组件",
            }],
        }
        review_contract = {
            "checks": ["遵守项目规范"],
            "separate_acceptance_session": separate_acceptance_session,
        }
        acceptance_plan = [{
            "criterion": "功能可用",
            "file": "src/APage.tsx",
            "symbol": "APage",
            "method": "运行聚焦检查",
            "command": "rg -q '快乐智慧园' src/APage.tsx" if mechanical else "test -f src/APage.tsx",
            "expected": "文案已替换" if mechanical else "目标文件存在",
            "check_type": "automated",
        }]
        if duplicate_acceptance_command:
            acceptance_plan.append({
                "criterion": "页面状态正确",
                "file": "src/APage.tsx",
                "symbol": "APage",
                "method": "运行同一聚焦检查",
                "command": acceptance_plan[0]["command"],
                "expected": "同一次检查覆盖页面状态",
                "check_type": "automated",
            })
        self.service.report_location_status(
            project, True, "connected", "ok",
            {"tool": "codegraph_explore", "files": ["src/APage.tsx"], "symbols": ["APage"]},
        )
        analysis = self.service.prepare_location_analysis({
            "title": "上下文缓存", "project": project, "goal": "实现功能", "modules": ["a-page"],
        })
        completed = self.service.complete_location_analysis(
            analysis["analysis_id"],
            {"tool": "codegraph_explore", "files": ["src/APage.tsx"], "symbols": ["APage"]},
            [target], acceptance_plan, {"decision": "independent"},
            implementation_contract, review_contract,
        )
        return self.service.create_task({
            "title": "上下文缓存",
            "project": project,
            "goal": "实现功能",
            "scope": ["组件"],
            "out_of_scope": [],
            "acceptance_criteria": [item["criterion"] for item in acceptance_plan],
            "modules": ["a-page"],
            "location_analysis_id": completed["id"],
            "dependency_analysis": {"decision": "independent"},
            "implementation_contract": implementation_contract,
            "review_contract": review_contract,
            "status": "ready",
            "workflow_version": 2,
        })

    def test_execution_context_groups_duplicate_acceptance_commands(self) -> None:
        self.create_task(duplicate_acceptance_command=True)
        claim = self.service.claim_next_task("worker")

        groups = claim["run"]["context_snapshot"]["acceptance_commands"]
        self.assertEqual(1, len(groups))
        self.assertEqual("automated", groups[0]["check_type"])
        self.assertEqual("test -f src/APage.tsx", groups[0]["command"])
        self.assertEqual(
            ["功能可用", "页面状态正确"],
            [item["criterion"] for item in groups[0]["criteria"]],
        )
        self.assertEqual(1, claim["dispatch_prompt"].count("test -f src/APage.tsx"))

    def test_model_context_compacts_legacy_flat_acceptance_commands(self) -> None:
        command = "test -f src/APage.tsx"
        context = model_run_context({
            "stage": "execution",
            "acceptance_commands": [
                {
                    "criterion": "功能可用", "expected": "目标文件存在",
                    "check_type": "automated", "command": command,
                },
                {
                    "criterion": "页面状态正确", "expected": "页面状态正确",
                    "check_type": "automated", "command": command,
                },
            ],
        })

        self.assertEqual(1, len(context["acceptance_commands"]))
        self.assertEqual(
            ["功能可用", "页面状态正确"],
            [item["criterion"] for item in context["acceptance_commands"][0]["criteria"]],
        )

    def claim_and_deliver(self, task: dict) -> dict:
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(
            task["id"], claim["run"]["run_type"], "development-thread", claim["run"]["id"],
        )
        target = self.project / "src" / "APage.tsx"
        target.write_text(target.read_text(encoding="utf-8") + "// delivery\n", encoding="utf-8")
        return self.service.submit_delivery(
            claim["run"]["id"], "完成", "聚焦检查通过",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": "功能可用", "evidence": "聚焦检查通过"}],
        )

    def test_run_context_is_embedded_and_reused_without_rebuilding(self) -> None:
        task = self.create_task(mechanical=True)
        claim = self.service.claim_next_task("worker")
        run_id = claim["run"]["id"]

        self.assertIn("RUN_CONTEXT_JSON=", claim["dispatch_prompt"])
        self.assertIn("submit_task_delivery", claim["dispatch_prompt"])
        self.assertIn("cache_metadata", claim["run"]["context_snapshot"])
        self.assertEqual("implementing", claim["task"]["status"])
        self.assertEqual("low", claim["run"]["context_snapshot"]["execution_profile"]["risk"])
        for excluded in (
            "location_evidence", "dependency_analysis", "review_contract", "conversation_summaries",
        ):
            self.assertNotIn(excluded, claim["run"]["context_snapshot"])
        self.assertNotIn('"workspace_baseline"', claim["dispatch_prompt"])
        snippet = claim["run"]["context_snapshot"]["target_snippet"]
        self.assertEqual("src/APage.tsx", snippet["file"])
        self.assertEqual("APage", snippet["symbol"])
        self.assertEqual(
            hashlib.sha256((self.project / "src" / "APage.tsx").read_bytes()).hexdigest(),
            snippet["sha256"],
        )
        original_build_context = self.service.build_context
        self.service.build_context = lambda *_args, **_kwargs: self.fail("cache hit rebuilt context")
        try:
            cached = self.service.get_run_context(task["id"], run_id)
        finally:
            self.service.build_context = original_build_context

        self.assertTrue(cached["cache"]["hit"])
        self.assertEqual(run_id, cached["cache"]["run_id"])
        for server_only in (
            "workspace_baseline", "execution_profile", "cache_metadata", "tool_contract",
        ):
            self.assertNotIn(server_only, cached["context_snapshot"])
        persisted = self.service.get_run(run_id)["context_snapshot"]
        self.assertEqual(
            ["report_run_blocked", "submit_task_delivery"],
            persisted["tool_contract"]["allowed_completion_tools"],
        )

    def test_delivery_artifact_checks_and_verifier_thread_are_reused(self) -> None:
        task = self.create_task()
        delivery = self.claim_and_deliver(task)
        artifact = delivery["run"]["artifact_snapshot"]
        self.assertTrue(artifact["diff"]["available"])
        self.assertIn("// delivery", artifact["diff"]["content"])
        self.assertEqual(64, len(artifact["diff"]["sha256"]))

        review = self.service.claim_next_code_review_task("reviewer")
        review_context = review["run"]["context_snapshot"]
        self.assertEqual(artifact["diff"]["sha256"], review_context["delivery"]["diff"]["sha256"])
        self.assertEqual(["遵守项目规范"], review_context["review_checks"])
        for excluded in (
            "location_evidence", "direct_relations", "conversation_summaries",
            "dependency_analysis", "delivery_artifact",
        ):
            self.assertNotIn(excluded, review_context)
        self.service.bind_conversation(task["id"], "code_review", "verifier-thread", review["run"]["id"])
        self.service.review_code(
            task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"],
        )
        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.assertEqual("verifier-thread", acceptance["resume_thread_id"])
        acceptance_context = acceptance["run"]["context_snapshot"]
        self.assertIn("acceptance", acceptance_context)
        self.assertNotIn("diff", acceptance_context["delivery"])
        self.assertNotIn("implementation", acceptance_context)
        self.service.bind_conversation(
            task["id"], "acceptance", "verifier-thread", acceptance["run"]["id"],
        )

        first = self.service.run_acceptance_checks(task["id"], acceptance["run"]["id"])
        second = self.service.run_acceptance_checks(task["id"], acceptance["run"]["id"])
        forced = self.service.run_acceptance_checks(task["id"], acceptance["run"]["id"], force=True)
        self.assertEqual(0, first["cache_hits"])
        self.assertEqual(1, second["cache_hits"])
        self.assertEqual(1, second["checks"][0]["cache_hit"])
        self.assertEqual(0, forced["cache_hits"])

    def test_high_risk_contract_keeps_acceptance_in_a_separate_session(self) -> None:
        task = self.create_task(separate_acceptance_session=True)
        claim = self.service.claim_next_task("worker")
        self.assertEqual("standard", claim["run"]["context_snapshot"]["execution_profile"]["risk"])
        self.assertNotIn("target_snippet", claim["run"]["context_snapshot"])
        self.service.bind_conversation(task["id"], "execution", "development-thread", claim["run"]["id"])
        target = self.project / "src" / "APage.tsx"
        target.write_text(target.read_text(encoding="utf-8") + "// delivery\n", encoding="utf-8")
        self.service.submit_delivery(
            claim["run"]["id"], "完成", "聚焦检查通过",
            [{"file": "src/APage.tsx", "symbols": ["APage"]}],
            [{"criterion": "功能可用", "evidence": "聚焦检查通过"}],
        )
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "review-thread", review["run"]["id"])
        self.service.review_code(
            task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"],
        )

        acceptance = self.service.claim_next_acceptance_task("acceptance")
        self.assertEqual("", acceptance["resume_thread_id"])

    def test_token_breakdown_remains_monotonic_and_is_reported_by_stage(self) -> None:
        task = self.create_task()
        run = self.service.claim_next_task("worker")["run"]
        self.service.record_run_token_usage(run["id"], 1200, {
            "input_tokens": 900,
            "cached_input_tokens": 700,
            "output_tokens": 250,
            "reasoning_output_tokens": 50,
        })
        self.service.record_run_token_usage(run["id"], 1000, {
            "input_tokens": 800,
            "cached_input_tokens": 600,
            "output_tokens": 200,
            "reasoning_output_tokens": 40,
        })

        saved = self.service.get_run(run["id"])
        self.assertEqual(1200, saved["token_used"])
        self.assertEqual(520, saved["effective_token_used"])
        self.assertEqual(700, saved["cached_input_tokens"])
        board_task = next(item for item in self.service.list_tasks() if item["id"] == task["id"])
        self.assertEqual(520, board_task["effective_token_used"])
        self.assertEqual(900, board_task["token_by_stage"]["execution"]["input_tokens"])
        self.assertEqual(250, board_task["token_by_stage"]["execution"]["output_tokens"])
        self.assertEqual(520, board_task["token_by_stage"]["execution"]["effective_token_used"])

    def test_lifecycle_profile_exposes_only_cached_run_tools(self) -> None:
        with patch.dict(os.environ, {"CODEX_TASKBOARD_TOOL_PROFILE": "lifecycle"}):
            response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
            names = {item["name"] for item in response["result"]["tools"]}
            rejected = handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "list_board", "arguments": {}},
            })

        self.assertEqual(set(LIFECYCLE_TOOL_NAMES), names)
        self.assertTrue(rejected["result"]["isError"])
        self.assertIn("unavailable in lifecycle profile", rejected["result"]["content"][0]["text"])

    def test_stage_profiles_expose_only_the_current_stage_tools(self) -> None:
        for profile in ("execution", "verifier", "code_review", "acceptance", "legacy_review"):
            with self.subTest(profile=profile), patch.dict(
                os.environ, {"CODEX_TASKBOARD_TOOL_PROFILE": profile}, clear=False,
            ):
                response = handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
                names = {item["name"] for item in response["result"]["tools"]}
                self.assertEqual(set(TOOL_PROFILES[profile]), names)

    def test_execution_worker_has_a_bounded_blocking_exit(self) -> None:
        task = self.create_task()
        run = self.service.claim_next_task("worker")["run"]

        blocked = self.service.report_run_blocked(
            task["id"], run["id"], "waiting_confirmation", "需要产品确认边界",
        )

        self.assertEqual("waiting_confirmation", blocked["status"])
        self.assertEqual("interrupted", self.service.get_run(run["id"])["status"])
        with self.assertRaisesRegex(ValueError, "status must be"):
            self.service.report_run_blocked(task["id"], run["id"], "done", "invalid")

    def test_fully_automated_acceptance_completes_without_a_conversation(self) -> None:
        task = self.create_task()
        self.claim_and_deliver(task)
        review = self.service.claim_next_code_review_task("reviewer")
        self.service.bind_conversation(task["id"], "code_review", "verifier-thread", review["run"]["id"])
        self.service.review_code(
            task["id"], review["run"]["id"], "pass", passed_items=["遵守项目规范"],
        )
        acceptance = self.service.claim_next_acceptance_task("acceptance")

        result = self.service.auto_accept_automated_task(task["id"], acceptance["run"]["id"])

        self.assertTrue(result["eligible"])
        self.assertTrue(result["completed"])
        self.assertEqual("done", result["task"]["status"])
        self.assertEqual("completed", self.service.get_run(acceptance["run"]["id"])["status"])
        self.assertFalse(self.service.get_run(acceptance["run"]["id"]).get("conversation_thread_id"))
        verifier = next(
            item for item in self.service.list_conversations(task["id"])
            if item["thread_id"] == "verifier-thread"
        )
        self.assertEqual("completed", verifier["status"])

    def test_delivery_reuses_one_workspace_snapshot(self) -> None:
        task = self.create_task()
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(
            task["id"], "execution", "development-thread", claim["run"]["id"],
        )
        target = self.project / "src" / "APage.tsx"
        target.write_text(target.read_text(encoding="utf-8") + "// once\n", encoding="utf-8")
        with patch.object(self.service, "_workspace_state", wraps=self.service._workspace_state) as workspace_state:
            self.service.submit_delivery(
                claim["run"]["id"], "完成", "检查通过",
                [{"file": "src/APage.tsx", "symbols": ["APage"]}],
                [{"criterion": "功能可用", "evidence": "检查通过"}],
            )
        self.assertEqual(1, workspace_state.call_count)

    def test_task_budget_can_pause_an_active_run_for_confirmation(self) -> None:
        task = self.create_task()
        claim = self.service.claim_next_task("worker")
        self.service.bind_conversation(
            task["id"], "execution", "development-thread", claim["run"]["id"],
        )
        self.service.record_run_token_usage(claim["run"]["id"], task["token_budget"])

        result = self.service.pause_run_for_budget(claim["run"]["id"], "预算已用尽")

        self.assertTrue(result["changed"])
        self.assertEqual("waiting_confirmation", result["task"]["status"])
        self.assertEqual(0, result["task"]["auto_dispatch"])
        self.assertEqual("execution", result["task"]["retry_run_type"])


if __name__ == "__main__":
    unittest.main()
