from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core.self_healing import (
    classify_recoverable_failure,
    infer_environment_repair_command,
)


class SelfHealingTest(unittest.TestCase):
    def test_failure_classifier_separates_project_environment_and_implementation(self):
        self.assertEqual("project", classify_recoverable_failure("缺少测试用例"))
        self.assertEqual("project", classify_recoverable_failure("npm ERR! Missing script: test"))
        self.assertEqual("environment", classify_recoverable_failure("ModuleNotFoundError: No module named 'httpx'"))
        self.assertEqual("implementation", classify_recoverable_failure("AssertionError: expected 2, got 3"))

    def test_node_dependency_repair_uses_the_existing_locked_package_manager(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            (project / "package.json").write_text(
                json.dumps({"packageManager": "pnpm@10.0.0"}), encoding="utf-8"
            )
            (project / "pnpm-lock.yaml").write_text("lockfileVersion: '9.0'\n", encoding="utf-8")

            command = infer_environment_repair_command(
                project, "Cannot find module 'vitest'"
            )

        self.assertEqual("pnpm install --frozen-lockfile", command)

    def test_unknown_tool_does_not_trigger_a_system_install(self):
        with tempfile.TemporaryDirectory() as temporary:
            command = infer_environment_repair_command(
                temporary, "some-global-tool: command not found"
            )

        self.assertEqual("", command)


if __name__ == "__main__":
    unittest.main()
