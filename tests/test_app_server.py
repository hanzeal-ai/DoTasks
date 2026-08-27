from __future__ import annotations

import io
import os
import queue
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from core.service import TaskboardService
from taskboard.app_server import (
    AppServerError,
    CodexAppServerClient,
    prepare_taskboard_codex_home,
    task_thread_start_params,
    task_turn_start_params,
    tomllib,
)


class TaskboardCodexHomeTest(unittest.TestCase):
    def test_service_accepts_an_injected_workspace_guard(self):
        class WorkspaceGuard:
            def workspace_state(self, project):
                return {"available": True, "project": project, "fingerprint": "test"}

        with tempfile.TemporaryDirectory() as temporary:
            service = TaskboardService(temporary, workspace_guard=WorkspaceGuard())

            self.assertEqual("test", service._workspace_state("project")["fingerprint"])

    def test_service_uses_a_codex_home_inside_its_data_home(self):
        with tempfile.TemporaryDirectory() as temporary:
            service = TaskboardService(temporary)

            self.assertEqual(
                Path(temporary).resolve() / "codex-home", service.codex_home
            )
            self.assertNotEqual(Path.home() / ".codex", service.codex_home)

    def test_isolated_home_reuses_configuration_but_not_session_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared"
            isolated = root / "isolated"
            shared.mkdir()
            (shared / "auth.json").write_text("{}", encoding="utf-8")
            (shared / "config.toml").write_text("model = 'test'", encoding="utf-8")
            (shared / "plugins").mkdir()
            (shared / "sessions").mkdir()
            (shared / "state_5.sqlite").touch()

            runtime = root / "runtime"
            for name in ("codex-taskboard", "codex-taskboard-lifecycle"):
                skill = runtime / "skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

            result = prepare_taskboard_codex_home(
                isolated, shared, runtime, root / "data", "/usr/bin/python3"
            )

            self.assertEqual(isolated.resolve(), result)
            self.assertEqual(
                (shared / "auth.json").resolve(), (isolated / "auth.json").resolve()
            )
            config = (isolated / "config.toml").read_text(encoding="utf-8")
            self.assertEqual("test", tomllib.loads(config)["model"])
            self.assertEqual(
                {
                    "apps": False,
                    "goals": False,
                    "memories": False,
                    "multi_agent": False,
                    "plugins": False,
                    "remote_plugin": False,
                    "tool_suggest": False,
                },
                tomllib.loads(config)["features"],
            )
            mcp_config = tomllib.loads(config)["mcp_servers"]["codex-taskboard"]
            self.assertEqual(["-B", "-m", "taskboard.mcp_server"], mcp_config["args"])
            self.assertEqual("1", mcp_config["env"]["PYTHONDONTWRITEBYTECODE"])
            self.assertIn("[mcp_servers.codex-taskboard]", config)
            self.assertIn(str(root / "data"), config)
            self.assertNotIn("CODEX_TASKBOARD_TOOL_PROFILE", config)
            self.assertEqual([], list(isolated.glob(".config.toml.*.tmp")))
            self.assertEqual(
                (runtime / "skills" / "codex-taskboard-lifecycle").resolve(),
                (isolated / "skills" / "codex-taskboard-lifecycle").resolve(),
            )
            self.assertFalse((isolated / "skills" / "codex-taskboard").exists())
            self.assertFalse((isolated / "plugins").exists())
            self.assertFalse((isolated / "sessions").exists())
            self.assertFalse((isolated / "state_5.sqlite").exists())

    def test_shared_configuration_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shared = root / "shared"
            isolated = root / "isolated"
            shared.mkdir()
            (shared / "auth.json").write_text("{}", encoding="utf-8")
            runtime = root / "runtime"
            for name in ("codex-taskboard", "codex-taskboard-lifecycle"):
                skill = runtime / "skills" / name
                skill.mkdir(parents=True)
                (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")

            prepare_taskboard_codex_home(
                isolated, shared, runtime, root / "data", "/usr/bin/python3"
            )
            self.assertTrue((isolated / "auth.json").is_symlink())
            with patch.dict(os.environ, {"CODEX_TASKBOARD_SHARE_CODEX_CONFIG": "0"}):
                prepare_taskboard_codex_home(
                    isolated, shared, runtime, root / "data", "/usr/bin/python3"
                )

            self.assertFalse((isolated / "auth.json").exists())
            self.assertIn(
                "[mcp_servers.codex-taskboard]",
                (isolated / "config.toml").read_text(encoding="utf-8"),
            )

    def test_primary_codex_home_cannot_be_used_as_taskboard_storage(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(AppServerError, "must be separate"):
                prepare_taskboard_codex_home(temporary, temporary)

    def test_task_threads_request_only_whitelisted_mcp_approvals(self):
        expected = {
            "granular": {
                "mcp_elicitations": True,
                "rules": False,
                "sandbox_approval": False,
            }
        }
        self.assertEqual(expected, task_thread_start_params(".")["approvalPolicy"])
        turn_params = task_turn_start_params("thread", "prompt")
        self.assertEqual(expected, turn_params["approvalPolicy"])
        self.assertEqual({"type": "dangerFullAccess"}, turn_params["sandboxPolicy"])
        self.assertEqual("medium", turn_params["effort"])
        self.assertEqual(
            "low", task_turn_start_params("thread", "prompt", "acceptance")["effort"]
        )
        self.assertEqual(
            "high",
            task_turn_start_params("thread", "prompt", "acceptance", True)["effort"],
        )
        self.assertEqual(
            "low",
            task_turn_start_params("thread", "prompt", "execution", False, True)[
                "effort"
            ],
        )
        self.assertIn("baseInstructions", task_thread_start_params("."))
        approved = CodexAppServerClient._server_request_response(
            {
                "id": 7,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "serverName": "codex-taskboard",
                    "message": 'Allow the codex-taskboard MCP server to run tool "get_task_context"?',
                    "_meta": {"codex_approval_kind": "mcp_tool_call"},
                },
            }
        )
        self.assertEqual(
            {"id": 7, "result": {"action": "accept", "content": {}}}, approved
        )
        declined = CodexAppServerClient._server_request_response(
            {
                "id": 8,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "serverName": "untrusted",
                    "message": 'Allow an MCP server to run tool "get_task_context"?',
                    "_meta": {"codex_approval_kind": "mcp_tool_call"},
                },
            }
        )
        self.assertEqual("decline", declined["result"]["action"])

    def test_app_server_initialize_retries_one_transient_disconnect(self):
        client = CodexAppServerClient(executable=__file__)
        with (
            patch(
                "taskboard.app_server.prepare_taskboard_codex_home",
                return_value=Path("/tmp/test-home"),
            ),
            patch.object(
                client,
                "_start_connection",
                side_effect=[
                    AppServerError("initialize: Codex app-server connection closed"),
                    None,
                ],
            ) as start_connection,
            patch.object(client, "stop") as stop,
            patch("taskboard.app_server.time.sleep") as sleep,
        ):
            client.start()

        self.assertEqual(2, start_connection.call_count)
        stop.assert_called_once_with()
        sleep.assert_called_once()

    def test_app_server_worker_inherits_project_virtual_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            project = Path(temporary)
            python_home = project / ".venv" / "bin"
            python_home.mkdir(parents=True)
            (python_home / "python").touch(mode=0o755)
            client = CodexAppServerClient(executable=__file__, project=project)
            process = SimpleNamespace(
                poll=lambda: None,
                stdin=io.StringIO(),
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )
            with (
                patch("taskboard.app_server.subprocess.Popen", return_value=process) as popen,
                patch("taskboard.app_server.threading.Thread"),
                patch.object(client, "request"),
                patch.object(client, "notify"),
            ):
                client._start_connection(project / "codex-home")

            environment = popen.call_args.kwargs["env"]
            self.assertEqual(str(python_home.resolve()), environment["PATH"].split(os.pathsep)[0])
            self.assertIn("/usr/local/go/bin", environment["PATH"].split(os.pathsep))

    def test_app_server_initialize_does_not_retry_protocol_errors(self):
        client = CodexAppServerClient(executable=__file__)
        with (
            patch(
                "taskboard.app_server.prepare_taskboard_codex_home",
                return_value=Path("/tmp/test-home"),
            ),
            patch.object(
                client,
                "_start_connection",
                side_effect=AppServerError("initialize: invalid params"),
            ) as start_connection,
            patch.object(client, "stop") as stop,
            patch("taskboard.app_server.time.sleep") as sleep,
        ):
            with self.assertRaisesRegex(AppServerError, "invalid params"):
                client.start()

        start_connection.assert_called_once()
        stop.assert_called_once_with()
        sleep.assert_not_called()

    def test_app_server_initialize_reports_both_failed_attempts(self):
        client = CodexAppServerClient(executable=__file__)
        with (
            patch(
                "taskboard.app_server.prepare_taskboard_codex_home",
                return_value=Path("/tmp/test-home"),
            ),
            patch.object(
                client,
                "_start_connection",
                side_effect=[
                    AppServerError(
                        "initialize: Codex app-server connection closed (exit_code=1)"
                    ),
                    AppServerError(
                        "initialize: Codex app-server connection closed (exit_code=2)"
                    ),
                ],
            ),
            patch.object(client, "stop"),
            patch("taskboard.app_server.time.sleep"),
        ):
            with self.assertRaisesRegex(AppServerError, "after 2 attempts") as raised:
                client.start()

        self.assertIn("exit_code=1", str(raised.exception))
        self.assertIn("exit_code=2", str(raised.exception))

    def test_app_server_connection_error_keeps_exit_code_and_stderr(self):
        process = SimpleNamespace(poll=lambda: 17)
        client = CodexAppServerClient(executable=__file__)
        client.last_error = "fatal startup detail"

        message = client._process_failure_message(
            process, "Codex app-server connection closed"
        )

        self.assertIn("exit_code=17", message)
        self.assertIn("stderr=fatal startup detail", message)

    def test_stale_app_server_reader_cannot_fail_new_connection(self):
        old_process = SimpleNamespace(stdout=io.StringIO(""))
        new_process = SimpleNamespace()
        client = CodexAppServerClient(executable=__file__)
        client.process = new_process
        pending = queue.Queue(maxsize=1)
        client._pending[1] = pending

        client._read_stdout(old_process)

        self.assertIn(1, client._pending)
        self.assertTrue(pending.empty())


if __name__ == "__main__":
    unittest.main()
