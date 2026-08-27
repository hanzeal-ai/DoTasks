from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlparse

from taskboard.helper import TaskboardHelperClient, TaskboardHelperError
from taskboard.project_guard import ProjectWorkspaceGuard


ROOT = Path(__file__).resolve().parents[1]


class TaskboardHelperClientTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "project"
        self.project.mkdir()
        self.helper = self.home / "Taskboard Helper.app"
        executable = self.helper / "Contents" / "MacOS" / "TaskboardHelper"
        executable.parent.mkdir(parents=True)
        executable.touch()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def client_with_response(self, payload):
        def opener(arguments, **_kwargs):
            request_id = parse_qs(urlparse(arguments[-1]).query)["request"][0]
            response = self.home / "helper-requests" / f"{request_id}.json"
            response.write_text(json.dumps(payload), encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        return TaskboardHelperClient(
            self.home, helper_app=self.helper, opener=opener, sleep=lambda _value: None,
        )

    def test_authorize_project_returns_helper_selection(self) -> None:
        client = self.client_with_response({"project": str(self.project)})

        self.assertEqual(str(self.project), client.authorize_project())
        self.assertEqual([], list((self.home / "helper-requests").glob("*.json")))

    def test_authorize_project_returns_none_when_cancelled(self) -> None:
        client = self.client_with_response({"cancelled": True})

        self.assertIsNone(client.authorize_project())

    def test_authorize_project_surfaces_helper_error(self) -> None:
        client = self.client_with_response({"error": "authorization failed"})

        with self.assertRaisesRegex(TaskboardHelperError, "authorization failed"):
            client.authorize_project()

    def test_packaged_runtime_rejects_projects_without_a_bookmark(self) -> None:
        store = self.home / "authorized-projects.json"
        environment = {
            "CODEX_TASKBOARD_HELPER_APP": str(self.helper),
            "CODEX_TASKBOARD_HOME": str(self.home),
        }
        with patch.dict(os.environ, environment, clear=False):
            with self.assertRaisesRegex(ValueError, "not been authorized"):
                ProjectWorkspaceGuard.require_project_directory(self.project)
            store.write_text(
                json.dumps({"bookmarks": {str(self.project): "bookmark"}}),
                encoding="utf-8",
            )
            self.assertEqual(
                str(self.project),
                ProjectWorkspaceGuard.require_project_directory(self.project),
            )


@unittest.skipUnless(sys.platform == "darwin", "macOS helper test")
class MacosHelperTest(unittest.TestCase):
    def test_builds_hidden_hardened_signed_helper_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TaskboardHelperBuild") as temporary:
            environment = os.environ.copy()
            environment["CODEX_TASKBOARD_HELPER_OUTPUT_DIR"] = temporary
            environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            subprocess.run(
                ["/bin/sh", str(ROOT / "scripts" / "build-helper")],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            app = Path(temporary) / "Taskboard Helper.app"
            self.assertTrue((app / "Contents" / "MacOS" / "TaskboardHelper").is_file())
            runtime = app / "Contents" / "Resources" / "runtime"
            self.assertTrue((runtime / "scripts" / "start").is_file())
            self.assertTrue((runtime / "taskboard" / "helper.py").is_file())
            self.assertTrue((runtime / "taskboard" / "project_guard.py").is_file())
            self.assertTrue((runtime / "core" / "service" / "__init__.py").is_file())
            self.assertTrue((runtime / "vendor" / "tomli" / "__init__.py").is_file())
            self.assertTrue((runtime / "static" / "index.html").is_file())
            self.assertTrue(any((runtime / "static" / "assets").glob("*.js")))
            self.assertEqual([], list(runtime.rglob("__pycache__")))
            info = app / "Contents" / "Info.plist"
            self.assertEqual(
                "true",
                subprocess.run(
                    ["/usr/bin/plutil", "-extract", "LSUIElement", "raw", str(info)],
                    check=True, capture_output=True, text=True,
                ).stdout.strip(),
            )
            self.assertEqual(
                "codex-taskboard-helper",
                subprocess.run(
                    ["/usr/bin/plutil", "-extract", "CFBundleURLTypes.0.CFBundleURLSchemes.0", "raw", str(info)],
                    check=True, capture_output=True, text=True,
                ).stdout.strip(),
            )
            subprocess.run(
                ["/usr/bin/codesign", "--verify", "--deep", "--strict", str(app)],
                check=True, capture_output=True, text=True,
            )
            details = subprocess.run(
                ["/usr/bin/codesign", "-d", "--verbose=4", str(app)],
                check=False, capture_output=True, text=True,
            )
            self.assertIn("runtime", (details.stdout + details.stderr).casefold())

    def test_runtime_scripts_prefer_homebrew_python(self) -> None:
        for name, module in (("mcp-server", "taskboard.mcp_server"), ("start", "taskboard.server")):
            script = (ROOT / "scripts" / name).read_text()
            self.assertIn("CODEX_TASKBOARD_PYTHON_BIN", script)
            self.assertIn("/opt/homebrew/bin/python3", script)
            self.assertIn("Python 3.9 or newer", script)
            self.assertIn('VENDOR_DIR="$PROJECT_DIR/vendor"', script)
            self.assertIn(f'exec "$PYTHON_BIN" -B -m {module}', script)

    def test_helper_supplies_stable_launch_agent_tool_paths(self) -> None:
        source = (ROOT / "macos" / "TaskboardHelper.swift").read_text()
        self.assertIn('"/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"', source)
        self.assertIn('environment["PATH"]', source)

    def test_install_uses_a_persistent_user_launch_agent(self) -> None:
        script = (ROOT / "scripts" / "build-helper").read_text()
        self.assertIn('HELPER_LABEL="local.sanmws.codex-taskboard-helper"', script)
        self.assertIn('Add :RunAtLoad bool true', script)
        self.assertIn('Add :KeepAlive bool true', script)
        self.assertIn('launchctl bootstrap "$SERVICE_DOMAIN" "$LAUNCH_AGENT"', script)


if __name__ == "__main__":
    unittest.main()
