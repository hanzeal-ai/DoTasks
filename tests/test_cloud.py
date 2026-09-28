from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import sqlite3
import socket
import tempfile
import threading
import unittest
import zlib
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.service import TaskboardService
from taskboard.agent import AgentConfig, RelayAgent, load_agent_config, save_agent_config
from taskboard.cloud.server import RelayConfig, build_relay_server
from taskboard.cloud.store import RelayStore
from taskboard.config import CLOUD_MODE, ServerConfig
from taskboard.websocket_transport import encode_frame, read_frame, websocket_accept


class RelayStoreTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.store = RelayStore(self.temporary.name)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_command_round_trip_is_durable(self) -> None:
        command_id = self.store.enqueue(
            "mac", "POST", "/api/tasks", {"Content-Type": "application/json"}, b"{}"
        )
        command = self.store.claim("mac")
        self.assertEqual(command_id, command["id"])
        self.assertEqual(b"{}", command["body"])

        self.store.complete(
            "mac",
            command_id,
            201,
            {"Content-Type": "application/json"},
            b'{"ok":true}',
        )
        result = self.store.wait_result(command_id, 0.2)
        self.assertEqual(201, result["status"])
        self.assertEqual(b'{"ok":true}', result["body"])

    def test_queued_command_can_be_cancelled_before_agent_claim(self) -> None:
        command_id = self.store.enqueue("mac", "POST", "/api/settings", {}, b"{}")

        self.assertTrue(self.store.cancel_queued(command_id))
        self.assertFalse(self.store.cancel_queued(command_id))
        self.assertIsNone(self.store.claim("mac"))

    def test_agent_presence_and_board_events(self) -> None:
        self.assertFalse(self.store.agent_status("mac")["online"])
        first = self.store.touch_agent("mac", "hash-a", {"version": "1"})
        unchanged = self.store.touch_agent("mac", "hash-a", {"version": "1"})
        changed = self.store.touch_agent("mac", "hash-b", {"version": "1"})
        self.assertEqual(first, unchanged)
        self.assertEqual(first + 1, changed)
        self.assertTrue(self.store.agent_status("mac")["online"])

    def test_vault_mirror_rejects_escape_and_removes_deleted_files(self) -> None:
        content = b"# Task\n"
        digest = hashlib.sha256(content).hexdigest()
        self.store.write_vault_file("mac", "DoTasks/task.md", digest, content)
        target = self.store.vault_root / "mac" / "DoTasks" / "task.md"
        self.assertEqual(content, target.read_bytes())
        with self.assertRaisesRegex(ValueError, "project-relative"):
            self.store.write_vault_file("mac", "../outside.md", digest, content)
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            self.store.write_vault_file(
                "mac", "DoTasks/corrupt.md", "0" * 64, content
            )
        self.store.apply_vault_manifest("mac", [])
        self.assertFalse(target.exists())


class RelayHTTPServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        server_config = ServerConfig(
            mode=CLOUD_MODE,
            host="127.0.0.1",
            port=0,
            home=self.temporary.name,
            public_url="http://dotasks.test",
            http_user="operator",
            http_password="correct horse battery staple",
        ).validate()
        self.config = RelayConfig(
            server=server_config,
            agent_id="mac",
            agent_token="agent-token-with-at-least-24-characters",
            command_timeout_seconds=3,
            agent_poll_seconds=1,
        )
        self.server = build_relay_server(self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    @property
    def browser_headers(self) -> dict[str, str]:
        credentials = base64.b64encode(
            b"operator:correct horse battery staple"
        ).decode("ascii")
        return {
            "Host": "dotasks.test",
            "Origin": "http://dotasks.test",
            "Authorization": f"Basic {credentials}",
        }

    @property
    def agent_headers(self) -> dict[str, str]:
        return {
            "Host": "dotasks.test",
            "Authorization": f"Bearer {self.config.agent_token}",
            "Content-Type": "application/json",
        }

    def request(
        self,
        method: str,
        path: str,
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 4,
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        connection.request(method, path, body=body, headers=headers or {})
        response = connection.getresponse()
        result = response.status, dict(response.headers.items()), response.read()
        connection.close()
        return result

    def agent_post(self, path: str, payload: dict[str, object]):
        return self.request(
            "POST",
            path,
            json.dumps(payload).encode(),
            self.agent_headers,
        )

    def test_cloud_health_requires_browser_authentication(self) -> None:
        status, headers, body = self.request(
            "GET",
            "/api/health",
            headers={"Host": "dotasks.test", "Origin": "http://dotasks.test"},
        )
        self.assertEqual(401, status)
        self.assertNotIn("WWW-Authenticate", headers)
        self.assertIn("Authentication", json.loads(body)["error"])

    def login(self, password="correct horse battery staple", extra_headers=None):
        return self.request("POST", "/api/auth/login", json.dumps({
            "username": "operator", "password": password,
        }).encode(), {"Host": "dotasks.test", "Origin": "http://dotasks.test",
                      "Content-Type": "application/json", **(extra_headers or {})})

    def test_browser_session_login_logout_and_replay(self):
        public = {"Host": "dotasks.test", "Origin": "http://dotasks.test"}
        status, _, body = self.request("GET", "/api/auth/status", headers=public)
        self.assertEqual(200, status)
        self.assertEqual({"enabled": True, "authenticated": False}, json.loads(body))
        status, headers, _ = self.login()
        self.assertEqual(200, status)
        cookie = headers["Set-Cookie"].split(";", 1)[0]
        self.assertIn("HttpOnly", headers["Set-Cookie"])
        self.assertIn("SameSite=Strict", headers["Set-Cookie"])
        authenticated = {**public, "Cookie": cookie}
        self.assertEqual(200, self.request("GET", "/api/board", headers=authenticated)[0])
        profile = json.loads(self.request("GET", "/api/auth/status", headers=authenticated)[2])
        self.assertTrue(profile["authenticated"])
        self.assertEqual("operator", profile["username"])
        status, headers, _ = self.request("POST", "/api/auth/logout", b"{}", {
            **authenticated, "Content-Type": "application/json"})
        self.assertEqual(200, status)
        self.assertIn("dotasks_session=signed-out", headers["Set-Cookie"])
        for invalid_cookie in (headers["Set-Cookie"].split(";", 1)[0], cookie,
                               "dotasks_session=forged", "dotasks_session="):
            self.assertEqual(401, self.request("GET", "/api/board", headers={
                "Host": "dotasks.test", "Authorization": self.browser_headers["Authorization"],
                "Cookie": invalid_cookie})[0])
            profile = json.loads(self.request("GET", "/api/auth/status", headers={
                **public, "Cookie": invalid_cookie})[2])
            self.assertFalse(profile["authenticated"])
            self.assertNotIn("username", profile)
        self.assertEqual(401, self.request("GET", "/api/board", headers=authenticated)[0])
        self.assertEqual(401, self.request("GET", "/api/board", headers={
            **self.browser_headers, "Sec-Fetch-Mode": "cors"})[0])
        self.assertEqual(200, self.request("GET", "/api/board", headers=self.browser_headers)[0])

    def test_login_validation_and_rate_limit(self):
        for password in ("wrong", "错误密码", None, {"value": "wrong"}):
            status, headers, _ = self.login(password)
            self.assertEqual(401, status)
            self.assertNotIn("Set-Cookie", headers)
            self.assertNotIn("WWW-Authenticate", headers)
        for _ in range(16):
            self.assertEqual(401, self.login("wrong")[0])
        status, headers, _ = self.login()
        self.assertEqual(429, status)
        self.assertEqual("60", headers["Retry-After"])
        self.assertEqual(413, self.login("x" * 5000)[0])

    def test_auth_rejects_untrusted_host_origin_and_missing_origin(self):
        self.assertEqual(403, self.login(extra_headers={"Origin": "https://evil.test"})[0])
        self.assertEqual(403, self.login(extra_headers={"Host": "evil.test"})[0])
        self.assertEqual(403, self.login(extra_headers={"Origin": ""})[0])
        self.assertEqual(403, self.request("GET", "/api/auth/status", headers={"Host": "evil.test"})[0])
        self.assertEqual(403, self.request("POST", "/api/auth/logout", b"{}", {
            "Host": "dotasks.test", "Origin": "https://evil.test", "Content-Type": "application/json"})[0])

    def test_session_rotation_expiry_and_agent_isolation(self):
        cookie = self.login()[1]["Set-Cookie"].split(";", 1)[0]
        next_cookie = self.login(extra_headers={"Cookie": cookie})[1]["Set-Cookie"].split(";", 1)[0]
        self.assertNotEqual(cookie, next_cookie)
        public = {"Host": "dotasks.test"}
        self.assertEqual(401, self.request("GET", "/api/board", headers={**public, "Cookie": cookie})[0])
        self.assertEqual(401, self.request("GET", "/_agent/v1/events", headers={**public, "Cookie": next_cookie})[0])
        self.assertEqual(401, self.request("GET", "/api/board", headers=self.agent_headers)[0])
        with patch("taskboard.web_auth.time.monotonic", return_value=10**12):
            self.assertEqual(401, self.request("GET", "/api/board", headers={**public, "Cookie": next_cookie})[0])
        self.assertEqual(401, self.request("GET", "/api/board", headers={**public, "Cookie": "dotasks_session=forged"})[0])

    def test_public_login_shell_keeps_business_routes_protected(self):
        public = {"Host": "dotasks.test"}
        for path in ("/", "/login", "/dotasks-mark.svg"):
            self.assertEqual(200, self.request("GET", path, headers=public)[0])
        for method, path in (("GET", "/api/board"), ("GET", "/api/events/stream"),
                             ("POST", "/api/settings"), ("PATCH", "/api/tasks/example")):
            self.assertEqual(401, self.request(method, path, headers=public)[0])

    def test_browser_manages_cloud_board_while_agent_is_offline(self) -> None:
        payload = {
            "intake_kind": "requirement",
            "title": "Phone requirement",
            "goal": "Persist this on the cloud",
            "project": "/Users/example/project",
            "priority": "P2",
            "auto_dispatch": True,
        }
        status, _, body = self.request(
            "POST",
            "/api/task-intakes/finalize",
            json.dumps(payload).encode(),
            {**self.browser_headers, "Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        requirement_id = json.loads(body)["requirement_id"]
        status, _, body = self.request(
            "GET", "/api/board?view=all", headers=self.browser_headers
        )
        self.assertEqual(200, status)
        board = json.loads(body)
        self.assertEqual([requirement_id], [item["id"] for item in board["requirements"]])
        self.assertFalse(board["dispatcher"]["running"])

    def test_image_preview_requires_browser_authentication(self):
        content = b"\x89PNG\r\n\x1a\ncloud-image"
        status, _, body = self.request("POST", "/api/visual-artifacts", json.dumps({
            "filename": "image.png", "content_base64": base64.b64encode(content).decode(),
        }).encode(), {**self.browser_headers, "Content-Type": "application/json"})
        self.assertEqual(201, status)
        from urllib.parse import quote
        path = "/api/visual-artifacts/content?artifact_id=" + quote(json.loads(body)["artifact_id"], safe="")
        self.assertEqual(401, self.request("GET", path, headers={"Host": "dotasks.test"})[0])
        status, headers, body = self.request("GET", path, headers=self.browser_headers)
        self.assertEqual(200, status)
        self.assertEqual("image/png", headers["Content-Type"])
        self.assertEqual(content, body)

    def test_browser_uploads_visual_to_cloud_owned_storage(self) -> None:
        content = b"\x89PNG\r\n\x1a\ncloud-visual"
        status, _, body = self.request(
            "POST",
            "/api/visual-artifacts",
            json.dumps({
                "filename": "phone.png",
                "content_base64": base64.b64encode(content).decode("ascii"),
                "purpose": "phone requirement",
            }).encode(),
            {**self.browser_headers, "Content-Type": "application/json"},
        )

        self.assertEqual(201, status)
        reference = json.loads(body)
        self.assertTrue(reference["artifact_id"].startswith("artifact://visuals/"))
        self.assertNotIn("path", reference)
        status, _, body = self.agent_post(
            "/_agent/v1/tools/call",
            {
                "agent_id": "mac",
                "name": "read_visual_artifact",
                "arguments": {"artifact_id": reference["artifact_id"]},
            },
        )
        self.assertEqual(200, status)
        downloaded = json.loads(body)["result"]
        self.assertEqual(content, base64.b64decode(downloaded["content_base64"]))

    def test_websocket_notifies_agent_after_cloud_state_change(self) -> None:
        connection = socket.create_connection(("127.0.0.1", self.port), timeout=2)
        key = base64.b64encode(b"0123456789abcdef").decode("ascii")
        connection.sendall(
            (
                "GET /_agent/v1/events HTTP/1.1\r\n"
                "Host: dotasks.test\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Key: {key}\r\n"
                "Sec-WebSocket-Version: 13\r\n"
                f"Authorization: Bearer {self.config.agent_token}\r\n"
                "\r\n"
            ).encode("ascii")
        )
        stream = connection.makefile("rb")
        self.assertIn(b"101 Switching Protocols", stream.readline())
        response_headers: dict[str, str] = {}
        while True:
            line = stream.readline()
            if line in {b"\r\n", b"\n", b""}:
                break
            name, value = line.decode("iso-8859-1").split(":", 1)
            response_headers[name.lower()] = value.strip()
        self.assertEqual(websocket_accept(key), response_headers["sec-websocket-accept"])
        opcode, payload = read_frame(stream)
        self.assertEqual(0x1, opcode)
        self.assertEqual("connected", json.loads(payload)["type"])

        status, _, _ = self.request(
            "POST",
            "/api/dispatcher/resume",
            b"{}",
            {**self.browser_headers, "Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        opcode, payload = read_frame(stream)
        self.assertEqual(0x1, opcode)
        self.assertEqual("state_changed", json.loads(payload)["type"])
        connection.sendall(encode_frame(b"", opcode=0x8, masked=True))
        stream.close()
        connection.close()

    def test_agent_token_cannot_be_replaced_by_browser_credentials(self) -> None:
        status, _, body = self.request(
            "POST",
            "/_agent/v1/claim",
            b'{"agent_id":"mac"}',
            {**self.browser_headers, "Content-Type": "application/json"},
        )
        self.assertEqual(401, status)
        self.assertIn("agent credentials", json.loads(body)["error"])

    def test_offline_agent_does_not_block_cloud_reads(self) -> None:
        self.config = RelayConfig(
            server=self.config.server,
            agent_id=self.config.agent_id,
            agent_token=self.config.agent_token,
            command_timeout_seconds=0.1,
            agent_poll_seconds=self.config.agent_poll_seconds,
        )
        self.server.RequestHandlerClass.relay_config = self.config

        status, _, body = self.request(
            "GET", "/api/board", headers=self.browser_headers
        )

        self.assertEqual(200, status)
        self.assertEqual([], json.loads(body)["tasks"])
        self.assertIsNone(self.server.RequestHandlerClass.store.claim("mac"))

    def test_cloud_codex_projects_come_from_local_agent_metadata(self) -> None:
        projects = [
            {
                "id": "project-1",
                "name": "Example",
                "path": "/Users/example/project",
            }
        ]
        status, _, _ = self.agent_post(
            "/_agent/v1/claim",
            {
                "agent_id": "mac",
                "board_hash": "",
                "metadata": {"codex_projects": projects},
                "wait_seconds": 0,
            },
        )
        self.assertEqual(200, status)

        status, _, body = self.request(
            "GET", "/api/codex/projects", headers=self.browser_headers
        )
        self.assertEqual(200, status)
        self.assertEqual(projects, json.loads(body)["projects"])

    def test_agent_tool_call_operates_on_cloud_database(self) -> None:
        status, _, body = self.agent_post(
            "/_agent/v1/tools/call",
            {
                "agent_id": "mac",
                "name": "set_dispatcher_enabled",
                "arguments": {"enabled": True},
            },
        )
        self.assertEqual(200, status)
        self.assertEqual({"enabled": True}, json.loads(body)["result"])
        status, _, body = self.request("GET", "/api/board", headers=self.browser_headers)
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["dispatcher"]["enabled"])

    def test_empty_cloud_imports_one_compressed_local_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as local_home:
            project = Path(local_home) / "project"
            project.mkdir()
            local = TaskboardService(local_home)
            local.finalize_task_intake({
                "intake_kind": "requirement",
                "title": "Existing local requirement",
                "goal": "Move the source of truth to cloud",
                "project": str(project),
                "priority": "P2",
                "auto_dispatch": False,
            })
            snapshot = Path(local_home) / "snapshot.db"
            with closing(
                sqlite3.connect(Path(local_home) / "data" / "taskboard.db")
            ) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
            content = snapshot.read_bytes()

        status, _, body = self.agent_post(
            "/_agent/v1/bootstrap/status", {"agent_id": "mac"}
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["accept_snapshot"])

        status, _, body = self.agent_post(
            "/_agent/v1/bootstrap",
            {
                "agent_id": "mac",
                "encoding": "zlib+base64",
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": base64.b64encode(zlib.compress(content)).decode("ascii"),
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(json.loads(body)["imported"])

        status, _, body = self.request("GET", "/api/board", headers=self.browser_headers)
        self.assertEqual(200, status)
        self.assertEqual(
            ["Existing local requirement"],
            [item["title"] for item in json.loads(body)["requirements"]],
        )
        status, _, body = self.agent_post(
            "/_agent/v1/bootstrap/status", {"agent_id": "mac"}
        )
        self.assertEqual(200, status)
        self.assertFalse(json.loads(body)["accept_snapshot"])


class WebSocketTransportTest(unittest.TestCase):
    def test_masked_frame_round_trip(self) -> None:
        left, right = socket.socketpair()
        stream = right.makefile("rb")
        try:
            left.sendall(encode_frame(b'{"type":"wake"}', masked=True))
            opcode, payload = read_frame(stream)
        finally:
            stream.close()
            left.close()
            right.close()
        self.assertEqual(0x1, opcode)
        self.assertEqual(b'{"type":"wake"}', payload)


class RelayAgentTest(unittest.TestCase):
    def test_websocket_command_event_wakes_local_codex_executor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = AgentConfig(
                cloud_url="https://dotasks.example.com",
                agent_id="mac",
                agent_token="agent-token-with-at-least-24-characters",
                data_home=temporary,
            )
            executor = MagicMock()
            agent = RelayAgent(config, executor=executor)
            agent.bootstrap_cloud_state = lambda: False
            agent.sync_vault = lambda: None
            agent.drain_commands = MagicMock(return_value=0)
            connection = MagicMock()
            stream = MagicMock()
            with (
                patch(
                    "taskboard.agent.connect_websocket",
                    return_value=(connection, stream),
                ),
                patch(
                    "taskboard.agent.read_frame",
                    side_effect=[
                        (0x1, b'{"type":"command_available"}'),
                        (0x8, b""),
                    ],
                ),
            ):
                agent.run_event_stream_once()

            agent.drain_commands.assert_called_once_with()
            executor.wake.assert_called_once_with()

    def test_configuration_round_trip_uses_private_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config_path = Path(temporary) / "cloud-agent.json"
            config = AgentConfig(
                cloud_url="https://dotasks.example.com",
                agent_id="mac",
                agent_token="agent-token-with-at-least-24-characters",
                data_home=temporary,
            )
            save_agent_config(config, config_path)
            with patch.dict(
                os.environ,
                {"DOTASKS_AGENT_CONFIG": str(config_path), "DOTASKS_HOME": temporary},
                clear=False,
            ):
                loaded = load_agent_config()
            self.assertEqual(config.cloud_url, loaded.cloud_url)
            self.assertEqual(config.agent_token, loaded.agent_token)
            self.assertEqual(0o600, config_path.stat().st_mode & 0o777)

    def test_execute_command_forwards_api_response(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = AgentConfig(
                cloud_url="https://dotasks.example.com",
                agent_id="mac",
                agent_token="agent-token-with-at-least-24-characters",
                data_home=temporary,
            )
            agent = RelayAgent(config)
            completed: dict[str, object] = {}

            def local_request(method, path, headers=None, body=b""):
                self.assertEqual(("POST", "/api/tasks"), (method, path))
                self.assertEqual(b'{"title":"Task"}', body)
                return {
                    "status": 201,
                    "headers": {"Content-Type": "application/json"},
                    "body": b'{"id":"task-1"}',
                }

            def cloud_request(path, payload):
                completed.update({"path": path, "payload": payload})
                return {"ok": True}

            agent._local_request = local_request
            agent._cloud_request = cloud_request
            agent.execute_command(
                {
                    "id": "command-1",
                    "method": "POST",
                    "path": "/api/tasks",
                    "headers": {"Content-Type": "application/json"},
                    "body": base64.b64encode(b'{"title":"Task"}').decode("ascii"),
                }
            )
            self.assertEqual("/_agent/v1/complete", completed["path"])
            self.assertEqual(201, completed["payload"]["status"])
            self.assertEqual(
                b'{"id":"task-1"}', base64.b64decode(completed["payload"]["body"])
            )
            self.assertFalse(agent.pending_result_path.exists())

    def test_run_retransmits_pending_result_before_claiming_new_work(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            config = AgentConfig(
                cloud_url="https://dotasks.example.com",
                agent_id="mac",
                agent_token="agent-token-with-at-least-24-characters",
                data_home=temporary,
            )
            agent = RelayAgent(config)
            agent._save_pending_result(
                "command-1", {"status": 204, "headers": {}, "body": b""}
            )
            calls: list[str] = []

            def cloud_request(path, payload):
                calls.append(path)
                if path == "/_agent/v1/claim":
                    return {"command": None}
                return {"ok": True}

            agent._cloud_request = cloud_request
            agent.sync_vault = lambda: None
            self.assertFalse(agent.run_once())
            self.assertEqual(
                [
                    "/_agent/v1/complete",
                    "/_agent/v1/claim",
                ],
                calls,
            )


if __name__ == "__main__":
    unittest.main()
