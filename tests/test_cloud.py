from __future__ import annotations

import base64
import hashlib
import http.client
import json
import os
import socket
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

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
        self.assertIn("Basic", headers.get("WWW-Authenticate", ""))
        self.assertIn("Authentication", json.loads(body)["error"])

    def test_browser_request_round_trips_through_local_agent(self) -> None:
        received: dict[str, object] = {}

        def browser_request() -> None:
            received["response"] = self.request(
                "GET", "/api/board?view=all", headers=self.browser_headers
            )

        browser = threading.Thread(target=browser_request)
        browser.start()
        status, _, body = self.agent_post(
            "/_agent/v1/claim",
            {"agent_id": "mac", "board_hash": "abc", "metadata": {"version": "1"}},
        )
        self.assertEqual(200, status)
        command = json.loads(body)["command"]
        self.assertEqual("GET", command["method"])
        self.assertEqual("/api/board?view=all", command["path"])

        status, _, _ = self.agent_post(
            "/_agent/v1/complete",
            {
                "agent_id": "mac",
                "command_id": command["id"],
                "status": 200,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": base64.b64encode(b'{"tasks":[]}').decode("ascii"),
            },
        )
        self.assertEqual(200, status)
        browser.join(timeout=4)
        self.assertFalse(browser.is_alive())
        response_status, _, response_body = received["response"]
        self.assertEqual(200, response_status)
        self.assertEqual({"tasks": []}, json.loads(response_body))

    def test_websocket_notifies_agent_before_https_claim(self) -> None:
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

        received: dict[str, object] = {}

        def browser_request() -> None:
            received["response"] = self.request(
                "GET", "/api/board?view=all", headers=self.browser_headers
            )

        browser = threading.Thread(target=browser_request)
        browser.start()
        opcode, payload = read_frame(stream)
        self.assertEqual(0x1, opcode)
        self.assertEqual("command_available", json.loads(payload)["type"])

        status, _, body = self.agent_post(
            "/_agent/v1/claim",
            {
                "agent_id": "mac",
                "board_hash": "abc",
                "metadata": {"version": "1"},
                "wait_seconds": 0,
            },
        )
        self.assertEqual(200, status)
        command = json.loads(body)["command"]
        self.agent_post(
            "/_agent/v1/complete",
            {
                "agent_id": "mac",
                "command_id": command["id"],
                "status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": base64.b64encode(b'{"tasks":[]}').decode("ascii"),
            },
        )
        browser.join(timeout=4)
        self.assertFalse(browser.is_alive())
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

    def test_offline_agent_timeout_cancels_unclaimed_command(self) -> None:
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

        self.assertEqual(504, status)
        self.assertEqual("cancelled", json.loads(body)["command_status"])
        self.assertIsNone(self.server.RequestHandlerClass.store.claim("mac"))


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
