from __future__ import annotations

import json
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class SkillActivationTest(unittest.TestCase):
    def test_taskboard_skills_require_explicit_invocation(self):
        manual_skill = (ROOT / "skills/codex-taskboard/SKILL.md").read_text(encoding="utf-8")
        lifecycle_skill = (ROOT / "skills/codex-taskboard-lifecycle/SKILL.md").read_text(encoding="utf-8")
        manual_agent = (ROOT / "skills/codex-taskboard/agents/openai.yaml").read_text(encoding="utf-8")
        lifecycle_agent = (
            ROOT / "skills/codex-taskboard-lifecycle/agents/openai.yaml"
        ).read_text(encoding="utf-8")

        self.assertIn("allow_implicit_invocation: false", manual_agent)
        self.assertIn("allow_implicit_invocation: false", lifecycle_agent)
        self.assertIn("$codex-taskboard-lifecycle", lifecycle_skill)
        self.assertIn("TASK-*", lifecycle_skill)
        self.assertIn("RUN-*", lifecycle_skill)
        self.assertIn("Do not use", manual_skill.split("---", 2)[1])

    def test_new_task_skill_keeps_management_details_progressive(self):
        manual_skill = (ROOT / "skills/codex-taskboard/SKILL.md").read_text(encoding="utf-8")
        reference = ROOT / "skills/codex-taskboard/references/management-and-review.md"
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

    def test_plugin_registers_codegraph_mcp_with_taskboard(self):
        config = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        codegraph = config["mcpServers"]["codegraph"]
        self.assertEqual("codegraph", codegraph["command"])
        self.assertEqual(["serve", "--mcp"], codegraph["args"])


if __name__ == "__main__":
    unittest.main()
