from __future__ import annotations

import unittest
from unittest.mock import patch

from taskboard.mcp_server import LOCATION_EVIDENCE_SCHEMA, TOOL_HANDLERS, TOOLS, handle


class TaskboardMcpServerTest(unittest.TestCase):
    def test_initialize_reports_dotasks_brand(self):
        result = handle({
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-03-26"},
        })["result"]
        self.assertEqual("dotasks", result["serverInfo"]["name"])

    def test_every_declared_tool_has_exactly_one_handler(self):
        self.assertEqual({tool["name"] for tool in TOOLS}, set(TOOL_HANDLERS))

    def test_only_confirmed_task_creation_is_exposed(self):
        names = {tool["name"] for tool in TOOLS}
        self.assertIn("finalize_task_intake", names)
        self.assertNotIn("create_confirmed_task", names)
        self.assertIn("report_location_status", names)
        self.assertNotIn("report_codegraph_status", names)
        self.assertIn("run_acceptance_checks", names)
        self.assertIn("report_run_blocked", names)
        self.assertIn("detect_task_change", names)
        self.assertIn("prepare_task_change_confirmation", names)
        self.assertIn("resolve_task_change_confirmation", names)
        self.assertIn("get_requirement", names)
        self.assertIn("submit_requirement_decomposition", names)
        self.assertIn("report_requirement_decomposition_failed", names)
        self.assertTrue({
            "claim_next_dispatch", "claim_dispatch_batch", "mark_dispatch_pending", "bind_native_dispatch",
            "renew_dispatch_lease", "get_dispatch_status", "report_dispatch_failed",
        }.issubset(names))
        self.assertNotIn("create_requirement", names)
        self.assertNotIn("update_requirement", names)
        self.assertNotIn("create_task", names)
        self.assertNotIn("register_notification_thread", names)
        self.assertNotIn("list_pending_notifications", names)
        self.assertNotIn("mark_notifications_delivered", names)

    def test_execution_contract_omits_model_reported_tokens_and_generic_transition(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        self.assertNotIn(
            "token_used", tools["submit_task_delivery"]["inputSchema"]["properties"],
        )
        self.assertIn(
            "retry instead of reporting the run blocked",
            tools["submit_task_delivery"]["description"],
        )
        self.assertIn(
            "workspace_path", tools["submit_task_delivery"]["inputSchema"]["properties"],
        )
        self.assertEqual(
            ["waiting_confirmation", "blocked"],
            tools["report_run_blocked"]["inputSchema"]["properties"]["status"]["enum"],
        )

    def test_project_contract_requires_an_absolute_existing_directory(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        for name in ("prepare_task_location", "finalize_task_intake"):
            description = tools[name]["inputSchema"]["properties"]["project"]["description"]
            self.assertIn("Absolute", description)
            self.assertIn("existing project directory", description)

    def test_location_report_contract_documents_all_fallbacks(self):
        tools = {item["name"]: item for item in TOOLS}
        evidence = tools["report_location_status"]["inputSchema"]["properties"]["evidence"]
        self.assertEqual(LOCATION_EVIDENCE_SCHEMA, evidence)
        cli = next(
            variant for variant in evidence["oneOf"]
            if variant["properties"]["tool"]["enum"] == ["codegraph_cli_explore"]
        )
        self.assertIn("command", cli["required"])
        self.assertIn("exit_code", cli["required"])
        self.assertNotIn("argv", cli["properties"])
        self.assertEqual("array", cli["properties"]["command"]["type"])
        self.assertEqual("string", cli["properties"]["command"]["items"]["type"])

        properties = tools["complete_location_analysis"]["inputSchema"]["properties"]
        self.assertIn("location_evidence", properties)
        self.assertNotIn("codegraph_evidence", properties)
        self.assertEqual(LOCATION_EVIDENCE_SCHEMA, properties["location_evidence"])
        self.assertEqual(
            LOCATION_EVIDENCE_SCHEMA,
            tools["finalize_task_intake"]["inputSchema"]["properties"]["location_evidence"],
        )
        child = tools["submit_requirement_decomposition"]["inputSchema"]["properties"][
            "tasks"
        ]["items"]["properties"]
        self.assertEqual(LOCATION_EVIDENCE_SCHEMA, child["location_evidence"])

    def test_implementation_contract_schema_requires_object_items(self):
        tools = {tool["name"]: tool for tool in TOOLS}
        contract = tools["complete_location_analysis"]["inputSchema"]["properties"]["implementation_contract"]
        self.assertEqual("object", contract["properties"]["targets"]["items"]["type"])
        self.assertEqual(
            "object",
            contract["properties"]["targets"]["items"]["properties"]["tasks"]["items"]["type"],
        )
        intake = tools["finalize_task_intake"]["inputSchema"]["properties"]
        self.assertEqual("object", intake["targets"]["items"]["type"])
        self.assertNotIn("ordered_steps", intake)

    def test_native_dispatch_schema_exposes_independent_stage_lanes(self):
        tool = next(item for item in TOOLS if item["name"] == "claim_next_dispatch")
        stage = tool["inputSchema"]["properties"]["stage"]

        self.assertEqual(
            ["development", "code_review"], stage["enum"]
        )
        self.assertIn("independent", tool["description"])

        with patch(
            "taskboard.mcp_server.SERVICE.claim_next_native_dispatch",
            return_value=None,
        ) as claim:
            handle({
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "claim_next_dispatch",
                    "arguments": {
                        "worker_id": "codex-native-controller",
                        "stage": "development",
                    },
                },
            })
        claim.assert_called_once_with(
            "codex-native-controller", None, 1800, "development"
        )

        batch_tool = next(item for item in TOOLS if item["name"] == "claim_dispatch_batch")
        self.assertEqual(
            ["development", "code_review"],
            batch_tool["inputSchema"]["properties"]["stage"]["enum"],
        )

    def test_finalize_intake_schema_contains_each_authored_fact_once(self):
        tool = next(item for item in TOOLS if item["name"] == "finalize_task_intake")
        properties = tool["inputSchema"]["properties"]
        required = tool["inputSchema"]["required"]
        task_branch = tool["inputSchema"]["anyOf"][1]["required"]

        self.assertEqual(["requirement", "task"], properties["intake_kind"]["enum"])
        self.assertIn("targets", task_branch)
        self.assertNotIn("ordered_steps", task_branch)
        self.assertIn("tasks", properties["targets"]["items"]["properties"])
        self.assertIn("mode", properties["targets"]["items"]["properties"])
        self.assertIn("review_checks", task_branch)
        self.assertIn("quality_gates", task_branch)
        self.assertIn("acceptance_plan", task_branch)
        gates = properties["quality_gates"]
        self.assertEqual(["code_review"], gates["required"])
        self.assertNotIn("acceptance", gates["properties"])
        self.assertNotIn("accept_task", {item["name"] for item in TOOLS})
        self.assertEqual(
            ["required", "reason"], gates["properties"]["code_review"]["required"]
        )
        self.assertIn("acceptance_criteria", properties)
        self.assertNotIn("implementation_contract", properties)
        self.assertNotIn("review_contract", properties)

    def test_requirement_and_task_intake_results_have_explicit_kinds(self):
        requirement = {
            "status": "created", "intake_kind": "requirement",
            "requirement_id": "REQ-0001", "requirement_status": "ready",
        }
        task = {
            "status": "created", "intake_kind": "task",
            "task_id": "TASK-0001", "task_status": "ready",
        }
        with patch.dict(TOOL_HANDLERS, {"finalize_task_intake": lambda arguments: requirement if arguments["intake_kind"] == "requirement" else task}, clear=False):
            requirement_result = handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "finalize_task_intake", "arguments": {"intake_kind": "requirement"}},
            })["result"]["structuredContent"]
            task_result = handle({
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "finalize_task_intake", "arguments": {"intake_kind": "task"}},
            })["result"]["structuredContent"]
        self.assertEqual(("requirement", "REQ-0001"), (requirement_result["intake_kind"], requirement_result["requirement_id"]))
        self.assertEqual(("task", "TASK-0001"), (task_result["intake_kind"], task_result["task_id"]))

    def test_decomposition_contract_requires_ready_task_location_fields(self):
        tool = next(item for item in TOOLS if item["name"] == "submit_requirement_decomposition")
        task_items = tool["inputSchema"]["properties"]["tasks"]["items"]
        required = set(task_items["required"])
        self.assertTrue({
            "analysis_id", "location_evidence", "targets",
            "review_checks", "acceptance_plan",
        }.issubset(required))
        self.assertEqual(1, task_items["properties"]["targets"]["minItems"])
        self.assertNotIn("ordered_steps", required)
        self.assertIn("tasks", task_items["properties"]["targets"]["items"]["properties"])

    def test_mutation_results_are_compact(self):
        result = {
            "task": {"id": "TASK-1", "status": "code_review", "active_run_id": None, "goal": "x" * 5000},
            "run": {"id": "RUN-1", "artifact_snapshot": {"diff": "x" * 5000}},
            "review_dispatch_required": True,
        }
        with patch.dict(
            TOOL_HANDLERS, {"submit_task_delivery": lambda _arguments: result}, clear=False,
        ):
            response = handle({
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "submit_task_delivery", "arguments": {}},
            })["result"]

        self.assertEqual("TASK-1", response["structuredContent"]["task_id"])
        self.assertEqual("RUN-1", response["structuredContent"]["run_id"])
        self.assertLess(len(response["content"][0]["text"]), 300)
        self.assertNotIn("\n", response["content"][0]["text"])


if __name__ == "__main__":
    unittest.main()
