from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest

from taskboard.server import MAX_JSON_BODY_BYTES, build_server


class TaskboardHTTPServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.server = build_server("127.0.0.1", 0, self.temporary.name)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_port
        self.origin = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temporary.cleanup()

    def request(self, method: str, path: str, body: bytes | None = None, **headers: str):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        content = response.read()
        connection.close()
        return response.status, response.headers, json.loads(content or b"{}")

    def test_workflow_metadata_comes_from_server(self) -> None:
        status, _, payload = self.request("GET", "/api/workflow")
        self.assertEqual(200, status)
        self.assertIn("done", payload["status_labels"])
        self.assertNotIn("completion", payload["status_labels"])

    def test_task_token_budget_settings_can_be_read_and_updated(self) -> None:
        status, _, payload = self.request("GET", "/api/settings")
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "task_token_budget": 60000,
                "max_batch_appended_tasks": 3,
                "parallel_development_enabled": False,
                "max_parallel_development": 2,
            }, payload
        )

        status, _, payload = self.request(
            "POST",
            "/api/settings",
            json.dumps(
                {
                    "task_token_budget": 120000,
                    "max_batch_appended_tasks": 4,
                    "parallel_development_enabled": True,
                    "max_parallel_development": 3,
                }
            ).encode("utf-8"),
            Origin=self.origin,
            **{"Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        self.assertEqual(
            {
                "task_token_budget": 120000,
                "max_batch_appended_tasks": 4,
                "parallel_development_enabled": True,
                "max_parallel_development": 3,
            }, payload
        )
        self.assertEqual(
            {
                "task_token_budget": 120000,
                "max_batch_appended_tasks": 4,
                "parallel_development_enabled": True,
                "max_parallel_development": 3,
            },
            self.server.RequestHandlerClass.service.task_settings(),
        )

    def test_only_latest_task_intake_route_is_available(self) -> None:
        service = self.server.RequestHandlerClass.service
        original_finalize = service.finalize_task_intake
        service.finalize_task_intake = lambda payload: {"status": "created", "title": payload["title"]}
        try:
            status, _, payload = self.request(
                "POST", "/api/task-intakes/finalize",
                json.dumps({"title": "latest"}).encode("utf-8"),
                Origin=self.origin, **{"Content-Type": "application/json"},
            )
            self.assertEqual(200, status)
            self.assertEqual({"status": "created", "title": "latest"}, payload)

            status, _, payload = self.request(
                "POST", "/api/tasks", b"{}",
                Origin=self.origin, **{"Content-Type": "application/json"},
            )
            self.assertEqual(404, status)
            self.assertEqual("API route not found", payload["error"])
        finally:
            service.finalize_task_intake = original_finalize

    def test_untrusted_origin_cannot_mutate_dispatcher(self) -> None:
        service = self.server.RequestHandlerClass.service
        service.set_dispatcher_enabled(True)
        status, headers, payload = self.request(
            "POST", "/api/dispatcher/pause", b"{}",
            Origin="https://untrusted.example", **{"Content-Type": "text/plain"},
        )
        self.assertEqual(403, status)
        self.assertIn("Untrusted request origin", payload["error"])
        self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
        self.assertTrue(service.dispatcher_enabled())

    def test_json_endpoint_rejects_non_json_body(self) -> None:
        status, _, payload = self.request(
            "POST", "/api/dispatcher/pause", b"{}",
            Origin=self.origin, **{"Content-Type": "text/plain"},
        )
        self.assertEqual(415, status)
        self.assertIn("application/json", payload["error"])

    def test_trusted_json_request_can_mutate_dispatcher(self) -> None:
        service = self.server.RequestHandlerClass.service
        service.set_dispatcher_enabled(True)
        status, headers, payload = self.request(
            "POST", "/api/dispatcher/pause", b"{}",
            Origin=self.origin, **{"Content-Type": "application/json"},
        )
        self.assertEqual(200, status)
        self.assertEqual(self.origin, headers.get("Access-Control-Allow-Origin"))
        self.assertFalse(payload["dispatcher_enabled"])
        self.assertFalse(service.dispatcher_enabled())

    def test_json_endpoint_rejects_oversized_body_before_reading(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("POST", "/api/dispatcher/pause")
        connection.putheader("Origin", self.origin)
        connection.putheader("Content-Type", "application/json")
        connection.putheader("Content-Length", str(MAX_JSON_BODY_BYTES + 1))
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(413, response.status)
        self.assertIn("too large", payload["error"])

    def test_api_rejects_untrusted_host(self) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        connection.putrequest("GET", "/api/health", skip_host=True)
        connection.putheader("Host", "attacker.example")
        connection.endheaders()
        response = connection.getresponse()
        payload = json.loads(response.read())
        connection.close()
        self.assertEqual(403, response.status)
        self.assertIn("Host", payload["error"])

    def test_server_refuses_non_loopback_binding(self) -> None:
        with self.assertRaisesRegex(ValueError, "loopback"):
            build_server("0.0.0.0", 0, self.temporary.name)

    def test_project_and_chat_endpoints_are_removed(self) -> None:
        headers = {"Origin": self.origin, "Content-Type": "application/json"}
        for method, path in (
            ("GET", "/api/codex/projects"),
            ("GET", "/api/codex/threads"),
            ("POST", "/api/codex/threads"),
            ("POST", "/api/chatkit"),
        ):
            body = b"{}" if method == "POST" else None
            status, _, payload = self.request(method, path, body, **(headers if body else {}))
            self.assertEqual(404, status, path)
            self.assertEqual("API route not found", payload["error"])

    def test_health_reports_native_controller_execution_mode(self) -> None:
        status, _, payload = self.request("GET", "/api/health")
        self.assertEqual(200, status)
        self.assertEqual("native_codex_controller", payload["dispatcher"]["execution_mode"])
        self.assertIsNone(payload["dispatcher"]["running"])


if __name__ == "__main__":
    unittest.main()
