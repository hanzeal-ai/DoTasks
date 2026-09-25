from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class RuntimeScriptsTest(unittest.TestCase):
    def test_runtime_scripts_prefer_homebrew_python(self) -> None:
        for name, module in (
            ("mcp-server", "taskboard.mcp_server"),
            ("start", "taskboard.server"),
            ("start-agent", "taskboard.agent"),
            ("start-cloud", "taskboard.cloud.server"),
        ):
            script = (ROOT / "scripts" / name).read_text()
            self.assertIn("DOTASKS_PYTHON_BIN", script)
            self.assertIn("/opt/homebrew/bin/python3", script)
            self.assertIn("Python 3.14 or newer", script)
            self.assertNotIn('VENDOR_DIR="$PROJECT_DIR/vendor"', script)
            self.assertIn(f'exec "$PYTHON_BIN" -B -m {module}', script)

    def test_mcp_server_supports_one_shot_lifecycle_tool_fallback(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DoTasksCliFallback") as temporary:
            environment = os.environ.copy()
            environment.update({
                "DOTASKS_HOME": temporary,
                "DOTASKS_PYTHON_BIN": sys.executable,
            })
            result = subprocess.run(
                [
                    "/bin/sh",
                    str(ROOT / "scripts" / "mcp-server"),
                    "--call-tool",
                    "set_dispatcher_enabled",
                ],
                cwd=ROOT,
                env=environment,
                input='{"enabled":true}',
                check=True,
                capture_output=True,
                text=True,
            )

            self.assertTrue(json.loads(result.stdout)["enabled"])

