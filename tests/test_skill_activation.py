from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillActivationTest(unittest.TestCase):
    def test_dotasks_skills_require_explicit_invocation(self):
        manual_skill = (ROOT / "skills/dotasks/SKILL.md").read_text(encoding="utf-8")
        lifecycle_skill = (ROOT / "skills/dotasks-lifecycle/SKILL.md").read_text(encoding="utf-8")
        manual_agent = (ROOT / "skills/dotasks/agents/openai.yaml").read_text(encoding="utf-8")
        lifecycle_agent = (
            ROOT / "skills/dotasks-lifecycle/agents/openai.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("allow_implicit_invocation: false", manual_agent)
        self.assertIn("allow_implicit_invocation: false", lifecycle_agent)
        self.assertIn("$dotasks-lifecycle", lifecycle_skill)
        self.assertIn("TASK-*", lifecycle_skill)
        self.assertIn("RUN-*", lifecycle_skill)
        self.assertIn("call `prepare_task_location`", lifecycle_skill)
        self.assertIn("persist the connected evidence with `report_location_status`", lifecycle_skill)
        self.assertIn("Do not call `complete_location_analysis` during requirement decomposition", lifecycle_skill)
        self.assertIn("prepared location analyses", lifecycle_skill)
        self.assertIn("preflight every `changed_locations` entry", lifecycle_skill)
        self.assertIn("retry the same run", lifecycle_skill)
        self.assertIn("never call `report_run_blocked`", lifecycle_skill)
        self.assertIn("obtain the complete diff only with existing Git commands", lifecycle_skill)
        self.assertIn("git diff --no-index", lifecycle_skill)
        self.assertIn("use every relevant tool already available", lifecycle_skill)
        self.assertIn("Do not install tools, write temporary scripts or custom scanners", lifecycle_skill)
        self.assertNotIn("verify its SHA-256 and review that persisted patch", lifecycle_skill)
        self.assertIn("Do not use", manual_skill.split("---", 2)[1])

    def test_new_task_skill_keeps_management_details_progressive(self):
        manual_skill = (ROOT / "skills/dotasks/SKILL.md").read_text(encoding="utf-8")
        reference = ROOT / "skills/dotasks/references/management-and-review.md"
        self.assertTrue(reference.is_file())
        self.assertIn("references/management-and-review.md", manual_skill)
        self.assertIn("Do not load that reference during ordinary new-task intake", manual_skill)
        self.assertIn("codegraph_cli_explore", manual_skill)
        self.assertIn("gitnexus_cli_query", manual_skill)
        self.assertIn('"tool": "source_match"', manual_skill)
        self.assertIn("Missing CodeGraph and GitNexus are normal fallback conditions", manual_skill)
        self.assertIn("60-second creation budget", manual_skill)
        self.assertIn("Do not load implementation-domain skills", manual_skill)
        self.assertIn("Use one graph query only", manual_skill)
        self.assertIn("generic-module-only", manual_skill)

    def test_auto_dispatch_uses_event_driven_native_handoffs(self):
        manual_skill = (ROOT / "skills/dotasks/SKILL.md").read_text(encoding="utf-8")
        lifecycle_skill = (
            ROOT / "skills/dotasks-lifecycle/SKILL.md"
        ).read_text(encoding="utf-8")
        controller_skill = (
            ROOT / "skills/dotasks-controller/SKILL.md"
        ).read_text(encoding="utf-8")
        controller_agent = (
            ROOT / "skills/dotasks-controller/agents/openai.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("Automatic dispatch kickoff", manual_skill)
        self.assertIn("auto_dispatch=true", manual_skill)
        self.assertIn("controller_kickoff_required=true", manual_skill)
        self.assertIn("Local DoTasks Agent", manual_skill)
        self.assertIn("Codex CLI/App Server", manual_skill)
        self.assertIn("Do not call `claim_schedule_cycle`", manual_skill)
        self.assertIn("Never call `claim_schedule_cycle`", controller_skill)
        self.assertIn("WSS notification", controller_skill)
        management_reference = (
            ROOT / "skills/dotasks/references/management-and-review.md"
        ).read_text(encoding="utf-8")
        self.assertIn("configured parallel slot count", management_reference)
        self.assertIn("set_dispatcher_enabled", management_reference)
        self.assertNotIn("calls `claim_next_dispatch`", management_reference)
        self.assertIn("real CLI thread ID", management_reference)
        self.assertIn("Event-driven native handoff", lifecycle_skill)
        self.assertIn("workspace_path", lifecycle_skill)
        self.assertIn("parent Local Agent monitors", lifecycle_skill)
        self.assertIn("Never create scheduled work", lifecycle_skill)
        self.assertIn("Never create a heartbeat", controller_skill)
        self.assertNotIn("heartbeat", manual_skill.lower())
        self.assertIn("Never create a scheduled automation", manual_skill)
        self.assertNotIn("create_thread", controller_skill)
        self.assertIn("allow_implicit_invocation: false", controller_agent)
        self.assertIn("$dotasks-controller", controller_agent)

    def test_plugin_exposes_only_current_skill_names(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("dotasks", manifest["name"])
        self.assertEqual("DoTasks", manifest["interface"]["displayName"])
        for name in ("dotasks", "dotasks-controller", "dotasks-lifecycle"):
            skill = (ROOT / "skills" / name / "SKILL.md").read_text(encoding="utf-8")
            agent = (ROOT / "skills" / name / "agents/openai.yaml").read_text(encoding="utf-8")
            self.assertIn(f"name: {name}", skill)
            self.assertIn("DoTasks", agent)
            self.assertIn("allow_implicit_invocation: false", agent)
        for retired in ("codex-taskboard", "codex-taskboard-controller", "codex-taskboard-lifecycle"):
            self.assertFalse((ROOT / "skills" / retired / "SKILL.md").exists())

    def test_plugin_registers_codegraph_mcp_with_taskboard(self):
        manifest = json.loads((ROOT / ".codex-plugin/plugin.json").read_text(encoding="utf-8"))
        self.assertEqual("./.mcp.json", manifest["mcpServers"])
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        dotasks = config["mcpServers"]["dotasks"]
        self.assertEqual("./scripts/mcp-server", dotasks["command"])
        codegraph = config["mcpServers"]["codegraph"]
        self.assertEqual("codegraph", codegraph["command"])
        self.assertEqual(["serve", "--mcp"], codegraph["args"])


if __name__ == "__main__":
    unittest.main()
