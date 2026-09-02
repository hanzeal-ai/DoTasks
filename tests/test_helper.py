from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskboard.project_guard import ProjectWorkspaceGuard


ROOT = Path(__file__).resolve().parents[1]


class ProjectWorkspaceGuardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.home = Path(self.temp.name).resolve()
        self.project = self.home / "project"
        self.project.mkdir()
        self.file = self.home / "file.txt"
        self.file.write_text("not a directory", encoding="utf-8")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_packaged_runtime_accepts_existing_absolute_directory_without_helper_state(self) -> None:
        environment = {
            "DOTASKS_HELPER_APP": str(self.home / "DoTasks Helper.app"),
            "DOTASKS_HOME": str(self.home),
        }
        with patch.dict(os.environ, environment, clear=False):
            self.assertEqual(
                str(self.project),
                ProjectWorkspaceGuard.require_project_directory(self.project),
            )

    def test_rejects_relative_missing_and_non_directory_paths(self) -> None:
        with self.assertRaisesRegex(ValueError, "absolute path"):
            ProjectWorkspaceGuard.require_project_directory("relative/project")
        with self.assertRaisesRegex(ValueError, "does not exist"):
            ProjectWorkspaceGuard.require_project_directory(self.home / "missing")
        with self.assertRaisesRegex(ValueError, "not a directory"):
            ProjectWorkspaceGuard.require_project_directory(self.file)


@unittest.skipUnless(sys.platform == "darwin", "macOS helper test")
class MacosHelperTest(unittest.TestCase):
    def test_builds_hidden_hardened_signed_helper_bundle(self) -> None:
        with tempfile.TemporaryDirectory(prefix="DoTasksHelperBuild") as temporary:
            environment = os.environ.copy()
            environment["DOTASKS_HELPER_OUTPUT_DIR"] = temporary
            environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
            subprocess.run(
                ["/bin/sh", str(ROOT / "scripts" / "build-helper")],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )
            app = Path(temporary) / "DoTasks Helper.app"
            self.assertTrue((app / "Contents" / "MacOS" / "DoTasksHelper").is_file())
            runtime = app / "Contents" / "Resources" / "runtime"
            self.assertTrue((runtime / "scripts" / "start").is_file())
            self.assertTrue((runtime / "scripts" / "start-agent").is_file())
            self.assertFalse((runtime / "taskboard" / "helper.py").exists())
            self.assertTrue((runtime / "taskboard" / "project_guard.py").is_file())
            self.assertTrue((runtime / "core" / "service" / "__init__.py").is_file())
            self.assertFalse((runtime / "vendor").exists())
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
                "dotasks-helper",
                subprocess.run(
                    ["/usr/bin/plutil", "-extract", "CFBundleURLTypes.0.CFBundleURLSchemes.0", "raw", str(info)],
                    check=True, capture_output=True, text=True,
                ).stdout.strip(),
            )
            self.assertEqual(
                "DoTasks Helper",
                subprocess.run(
                    ["/usr/bin/plutil", "-extract", "CFBundleDisplayName", "raw", str(info)],
                    check=True, capture_output=True, text=True,
                ).stdout.strip(),
            )
            info_dump = subprocess.run(
                ["/usr/bin/plutil", "-p", str(info)],
                check=True, capture_output=True, text=True,
            ).stdout
            self.assertNotIn("BootstrapProject", info_dump)
            self.assertNotIn("Authorization", info_dump)
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

    def test_mcp_server_does_not_enable_a_helper_project_guard(self) -> None:
        with tempfile.TemporaryDirectory(prefix="TaskboardMcpGuard") as temporary:
            home = Path(temporary)
            data_home = home / "Taskboard Data"
            helper = data_home / "DoTasks Helper.app" / "Contents" / "MacOS" / "DoTasksHelper"
            helper.parent.mkdir(parents=True)
            helper.write_text("#!/bin/sh\n", encoding="utf-8")
            helper.chmod(0o755)

            capture = home / "environment.json"
            python = home / "python3"
            python.write_text(
                "#!/bin/sh\n"
                "if [ \"${1:-}\" = \"-c\" ]; then exit 0; fi\n"
                "python3 - <<'PY'\n"
                "import json, os\n"
                "from pathlib import Path\n"
                "Path(os.environ['CAPTURE_PATH']).write_text(json.dumps({\n"
                "    'home': os.environ.get('DOTASKS_HOME'),\n"
                "    'dotasks_helper': os.environ.get('DOTASKS_HELPER_APP'),\n"
                "}))\n"
                "PY\n",
                encoding="utf-8",
            )
            python.chmod(0o755)
            environment = os.environ.copy()
            environment.update(
                {
                    "HOME": str(home),
                    "DOTASKS_HOME": str(data_home),
                    "DOTASKS_PYTHON_BIN": str(python),
                    "CAPTURE_PATH": str(capture),
                }
            )
            environment.pop("DOTASKS_HELPER_APP", None)

            subprocess.run(
                ["/bin/sh", str(ROOT / "scripts" / "mcp-server")],
                cwd=ROOT,
                env=environment,
                check=True,
                capture_output=True,
                text=True,
            )

            payload = json.loads(capture.read_text(encoding="utf-8"))
            self.assertEqual(str(data_home), payload["home"])
            self.assertIsNone(payload["dotasks_helper"])

    def test_helper_starts_server_without_project_authorization_code(self) -> None:
        source = (ROOT / "macos" / "DoTasksHelper.swift").read_text()
        self.assertIn("applicationDidFinishLaunching", source)
        self.assertIn("startServer()", source)
        self.assertIn("startAgent()", source)
        self.assertIn('"--wait-for-config"', source)
        for obsolete in (
            "NSOpenPanel", "authorized-projects.json", "BookmarkStore",
            "requestAuthorization", "restoreBookmarks", "persistAuthorization",
        ):
            self.assertNotIn(obsolete, source)

    def test_helper_supplies_stable_launch_agent_tool_paths(self) -> None:
        source = (ROOT / "macos" / "DoTasksHelper.swift").read_text()
        self.assertIn('"/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"', source)
        self.assertIn('environment["PATH"]', source)

    def test_install_uses_a_persistent_user_launch_agent(self) -> None:
        script = (ROOT / "scripts" / "build-helper").read_text()
        self.assertIn('HELPER_LABEL="local.sanmws.dotasks-helper"', script)
        self.assertLess(
            script.index('dotasks-helper://quit'),
            script.index('launchctl bootout "$SERVICE_DOMAIN/$HELPER_LABEL"'),
        )
        self.assertIn('-iTCP:8765 -sTCP:LISTEN', script)
        self.assertIn('*" -m taskboard.server"*', script)
        self.assertIn('Add :RunAtLoad bool true', script)
        self.assertIn('Add :KeepAlive bool true', script)
        self.assertIn('launchctl bootstrap "$SERVICE_DOMAIN" "$LAUNCH_AGENT"', script)


if __name__ == "__main__":
    unittest.main()
