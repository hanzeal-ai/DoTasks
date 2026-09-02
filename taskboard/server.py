from __future__ import annotations

import argparse
import ipaddress
import json
import mimetypes
import re
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.service import TaskboardService
from .version import VERSION
from core.workflow import workflow_metadata

MAX_JSON_BODY_BYTES = 1024 * 1024
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})


class HTTPRequestError(ValueError):
    def __init__(self, status: HTTPStatus, message: str):
        super().__init__(message)
        self.status = status


class TaskboardHandler(BaseHTTPRequestHandler):
    service: TaskboardService
    static_root: Path

    def _trusted_origins(self) -> set[str]:
        port = self.server.server_port
        return {
            "app://-",
            f"http://127.0.0.1:{port}",
            f"http://localhost:{port}",
            f"http://[::1]:{port}",
        }

    def _validate_api_request(self) -> None:
        host_header = self.headers.get("Host", "")
        try:
            parsed_host = urlparse(f"//{host_header}")
            hostname = (parsed_host.hostname or "").lower()
            port = parsed_host.port
        except ValueError as exc:
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Invalid Host header"
            ) from exc
        if hostname not in LOOPBACK_HOSTS or (
            port is not None and port != self.server.server_port
        ):
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted Host header")
        origin = self.headers.get("Origin")
        if origin and origin not in self._trusted_origins():
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted request origin")

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in self._trusted_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> dict[str, Any]:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Invalid Content-Length header"
            ) from exc
        if length < 0:
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Content-Length must be non-negative"
            )
        if length > MAX_JSON_BODY_BYTES:
            raise HTTPRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON request body is too large"
            )
        if length == 0:
            return {}
        content_type = self.headers.get_content_type()
        if content_type != "application/json":
            raise HTTPRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise HTTPRequestError(HTTPStatus.BAD_REQUEST, "Incomplete request body")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "JSON request body must be an object"
            )
        return payload

    def _serve_static(self, path: str) -> None:
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (self.static_root / relative).resolve()
        if self.static_root not in candidate.parents and candidate != self.static_root:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = self.static_root / "index.html"
        body = candidate.read_bytes()
        content_type = (
            mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        )
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8"
            if content_type.startswith("text/")
            else content_type,
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _serve_event_stream(self) -> None:
        try:
            cursor = int(self.headers.get("Last-Event-ID", "0") or "0")
        except ValueError:
            cursor = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(b"retry: 2000\n\n")
        self.wfile.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            latest = self.service.latest_event_id()
            if latest > cursor:
                cursor = latest
                payload = json.dumps({"event_id": latest}, ensure_ascii=False)
                self.wfile.write(
                    f"id: {latest}\nevent: board_changed\ndata: {payload}\n\n".encode(
                        "utf-8"
                    )
                )
                self.wfile.flush()
            time.sleep(0.5)
        self.wfile.write(b": reconnect\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            self._validate_api_request()
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, HTTPRequestError):
            self._json(exc.status, {"error": str(exc)})
        elif isinstance(exc, KeyError):
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        elif isinstance(exc, (ValueError, json.JSONDecodeError)):
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path.startswith("/api/"):
                self._validate_api_request()
            if parsed.path == "/api/health":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "version": VERSION,
                        "dispatcher": {
                            "enabled": self.service.dispatcher_enabled(),
                            "execution_mode": "native_codex_controller",
                            "running": None,
                        },
                    },
                )
            elif parsed.path == "/api/workflow":
                self._json(HTTPStatus.OK, workflow_metadata())
            elif parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, self.service.task_settings())
            elif parsed.path == "/api/events/stream":
                self._serve_event_stream()
            elif parsed.path == "/api/board":
                board = self.service.board()
                board["dispatcher"] = {
                    "enabled": self.service.dispatcher_enabled(),
                    "execution_mode": "native_codex_controller",
                    "running": None,
                }
                self._json(HTTPStatus.OK, board)
            elif parsed.path == "/api/integrations":
                query = parse_qs(parsed.query)
                project = query.get("project", [None])[0]
                self._json(HTTPStatus.OK, self.service.integration_status(project))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)", parsed.path):
                self._json(HTTPStatus.OK, self.service.get_task(match.group(1)))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/context", parsed.path):
                query = parse_qs(parsed.query)
                self._json(
                    HTTPStatus.OK,
                    self.service.build_context(
                        match.group(1), query.get("project_path", [None])[0]
                    ),
                )
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/details", parsed.path):
                self._json(HTTPStatus.OK, self.service.task_details(match.group(1)))
            elif parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
            else:
                self._serve_static(parsed.path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - exercised by integration behavior
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            self._validate_api_request()
            payload = self._read_json()
            if parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, self.service.update_task_settings(payload))
            elif parsed.path == "/api/task-intakes/finalize":
                self._json(HTTPStatus.OK, self.service.finalize_task_intake(payload))
            elif parsed.path == "/api/location-analyses":
                self._json(
                    HTTPStatus.CREATED,
                    self.service.prepare_location_analysis(
                        payload,
                        payload.get("stage", "creation"),
                        payload.get("task_id"),
                    ),
                )
            elif match := re.fullmatch(
                r"/api/task-changes/([^/]+)/resolve", parsed.path
            ):
                self._json(
                    HTTPStatus.OK,
                    self.service.resolve_task_change_confirmation(
                        match.group(1),
                        payload["decision"],
                    ),
                )
            elif parsed.path == "/api/integrations/location/report":
                self._json(
                    HTTPStatus.OK,
                    self.service.report_location_status(
                        payload["project"],
                        payload["available"],
                        payload["state"],
                        payload.get("summary", ""),
                        payload["evidence"],
                        payload.get("agent_id", ""),
                    ),
                )
            elif parsed.path == "/api/conversations/bind":
                self._json(
                    HTTPStatus.CREATED,
                    self.service.bind_conversation(
                        payload["task_id"],
                        payload["role"],
                        payload["thread_id"],
                        payload.get("run_id"),
                        payload.get("title", ""),
                    ),
                )
            elif parsed.path == "/api/dispatcher/pause":
                self._json(
                    HTTPStatus.OK,
                    self.service.pause_dispatcher(),
                )
            elif parsed.path == "/api/dispatcher/resume":
                result = (
                    self.service.resume_all_tasks()
                    if payload.get("resume_tasks")
                    else self.service.set_dispatcher_enabled(True)
                )
                self._json(HTTPStatus.OK, result)
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/resume", parsed.path):
                self._json(HTTPStatus.OK, self.service.resume_task(match.group(1)))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/transition", parsed.path):
                updates = payload.get("updates", {})
                self._json(
                    HTTPStatus.OK,
                    self.service.transition_task(
                        match.group(1),
                        payload["status"],
                        payload.get("reason", ""),
                        **updates,
                    ),
                )
            elif match := re.fullmatch(r"/api/runs/([^/]+)/delivery", parsed.path):
                self._json(
                    HTTPStatus.OK,
                    self.service.submit_delivery(
                        match.group(1),
                        payload["delivery_summary"],
                        payload["verification_result"],
                        payload["changed_locations"],
                        payload["acceptance_evidence"],
                        payload.get("token_used", 0),
                        workspace_path=payload.get("workspace_path"),
                    ),
                )
            elif match := re.fullmatch(
                r"/api/tasks/([^/]+)/conversation-summary", parsed.path
            ):
                self._json(
                    HTTPStatus.OK,
                    self.service.update_conversation_summary(
                        match.group(1),
                        payload["thread_id"],
                        payload["summary"],
                        payload.get("status", "completed"),
                    ),
                )
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/relations", parsed.path):
                self._json(
                    HTTPStatus.CREATED,
                    self.service.add_relation(
                        match.group(1),
                        payload["target_task_id"],
                        payload["relation_type"],
                        payload.get("description", ""),
                    ),
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def do_PATCH(self) -> None:  # noqa: N802
        try:
            self._validate_api_request()
            self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
        except Exception as exc:
            self._handle_error(exc)

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[taskboard] {self.address_string()} - {format % args}")


class TaskboardHTTPServer(ThreadingHTTPServer):
    pass


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    home: str | None = None,
) -> TaskboardHTTPServer:
    if host == "localhost":
        is_loopback = True
    else:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError as exc:
            raise ValueError(
                "DoTasks only supports localhost or a loopback IP address"
            ) from exc
    if not is_loopback:
        raise ValueError("DoTasks only supports loopback HTTP binding")
    service = TaskboardService(home)
    handler = type(
        "ConfiguredTaskboardHandler",
        (TaskboardHandler,),
        {"service": service, "static_root": service.package_home / "static"},
    )
    server = TaskboardHTTPServer((host, port), handler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DoTasks")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--home")
    args = parser.parse_args()
    server = build_server(args.host, args.port, args.home)
    print(f"DoTasks: http://{args.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
