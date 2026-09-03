from __future__ import annotations

import argparse
import base64
import hmac
import json
import os
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.workflow import workflow_metadata
from taskboard.config import CLOUD_MODE, ServerConfig
from taskboard.http_base import BaseDoTasksHandler, MAX_JSON_BODY_BYTES
from taskboard.http_security import HTTPRequestError
from taskboard.version import VERSION
from taskboard.websocket_transport import (
    WebSocketConnection,
    read_frame,
    websocket_accept,
)

from .store import RelayStore


AGENT_BODY_LIMIT = MAX_JSON_BODY_BYTES * 8


@dataclass(frozen=True)
class RelayConfig:
    server: ServerConfig
    agent_id: str
    agent_token: str
    command_timeout_seconds: int = 90
    agent_poll_seconds: int = 20

    @classmethod
    def from_environment(
        cls,
        *,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        public_url: str | None = None,
    ) -> "RelayConfig":
        server = ServerConfig.from_environment(
            mode=CLOUD_MODE,
            host=host,
            port=port,
            home=home,
            public_url=public_url,
        )
        agent_token = str(os.environ.get("DOTASKS_AGENT_TOKEN") or "")
        if len(agent_token) < 24:
            raise ValueError("DOTASKS_AGENT_TOKEN must contain at least 24 characters")
        agent_id = str(os.environ.get("DOTASKS_AGENT_ID") or "default").strip()
        if not agent_id or len(agent_id) > 100:
            raise ValueError("DOTASKS_AGENT_ID must contain 1 to 100 characters")
        return cls(
            server=server,
            agent_id=agent_id,
            agent_token=agent_token,
            command_timeout_seconds=max(
                5, min(int(os.environ.get("DOTASKS_COMMAND_TIMEOUT") or 90), 120)
            ),
            agent_poll_seconds=max(
                1, min(int(os.environ.get("DOTASKS_AGENT_POLL_SECONDS") or 20), 25)
            ),
        )


class RelayHandler(BaseDoTasksHandler):
    relay_config: RelayConfig
    store: RelayStore

    def _require_agent(self) -> str:
        host = self.headers.get("Host", "").lower()
        if host not in self.config.trusted_hosts(self.server.server_port):
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted Host header")
        authorization = self.headers.get("Authorization", "")
        supplied = authorization[7:] if authorization.startswith("Bearer ") else ""
        if not supplied or not hmac.compare_digest(
            supplied, self.relay_config.agent_token
        ):
            raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, "Invalid agent credentials")
        return self.relay_config.agent_id

    def _read_agent_json(self) -> dict[str, Any]:
        return self._read_json(AGENT_BODY_LIMIT)

    @property
    def relay_server(self) -> "RelayHTTPServer":
        return self.server  # type: ignore[return-value]

    def _serve_agent_events(self) -> None:
        agent_id = self._require_agent()
        if self.headers.get("Upgrade", "").lower() != "websocket":
            raise HTTPRequestError(
                HTTPStatus.UPGRADE_REQUIRED, "Agent event stream requires WebSocket"
            )
        if "upgrade" not in self.headers.get("Connection", "").lower():
            raise HTTPRequestError(HTTPStatus.BAD_REQUEST, "Invalid WebSocket connection")
        key = self.headers.get("Sec-WebSocket-Key", "")
        if not key or self.headers.get("Sec-WebSocket-Version") != "13":
            raise HTTPRequestError(HTTPStatus.BAD_REQUEST, "Invalid WebSocket handshake")
        self.send_response(HTTPStatus.SWITCHING_PROTOCOLS)
        self.send_header("Upgrade", "websocket")
        self.send_header("Connection", "Upgrade")
        self.send_header("Sec-WebSocket-Accept", websocket_accept(key))
        self.end_headers()
        connection = WebSocketConnection(self.connection)
        registration = self.relay_server.register_agent_socket(agent_id, connection)
        self.store.touch_agent(agent_id, "", {"transport": "websocket"})
        try:
            self.relay_server.notify_agent(agent_id, "connected")
            while True:
                opcode, payload = read_frame(self.rfile)
                if opcode == 0x8:
                    break
                if opcode == 0x9:
                    connection.send(payload, opcode=0xA)
                elif opcode == 0x1:
                    message = json.loads(payload.decode("utf-8"))
                    if message.get("type") == "heartbeat":
                        self.store.touch_agent(
                            agent_id,
                            str(message.get("board_hash") or ""),
                            message.get("metadata") or {},
                        )
        except (ConnectionError, BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.relay_server.unregister_agent_socket(agent_id, registration)
            self.close_connection = True

    def _serve_relay_events(self) -> None:
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
            latest = self.store.latest_event_id()
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

    def _proxy(self, method: str) -> None:
        if not self.path.startswith("/api/") or self.path == "/api/events/stream":
            self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
            return
        body = self._read_body() if method in {"POST", "PATCH"} else b""
        headers = {}
        content_type = self.headers.get("Content-Type")
        if content_type:
            headers["Content-Type"] = content_type
        command_id = self.store.enqueue(
            self.relay_config.agent_id, method, self.path, headers, body
        )
        self.relay_server.notify_agent(self.relay_config.agent_id, "command_available")
        result = self.store.wait_result(
            command_id, self.relay_config.command_timeout_seconds
        )
        if result is None:
            cancelled = self.store.cancel_queued(command_id)
            self._json(
                HTTPStatus.GATEWAY_TIMEOUT,
                {
                    "error": (
                        "Local DoTasks agent was unavailable; the request was cancelled"
                        if cancelled
                        else "Local DoTasks agent did not complete the request in time"
                    ),
                    "command_id": command_id,
                    "command_status": "cancelled" if cancelled else "in_progress",
                },
            )
            return
        response_body = result["body"]
        response_headers = result["headers"]
        self.send_response(result["status"])
        self.send_header(
            "Content-Type",
            response_headers.get("Content-Type", "application/json; charset=utf-8"),
        )
        self.send_header("Content-Length", str(len(response_body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(response_body)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/_agent/v1/events":
                self._serve_agent_events()
                return
            if parsed.path.startswith("/_agent/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "Agent route not found"})
                return
            if parsed.path.startswith("/api/"):
                self._validate_api_request()
            if parsed.path == "/api/health":
                agent = self.store.agent_status(self.relay_config.agent_id)
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": bool(agent["online"]),
                        "version": VERSION,
                        "mode": "cloud_relay",
                        "agent": agent,
                        "dispatcher": {
                            "enabled": None,
                            "execution_mode": "codex_cli_app_server",
                            "running": None,
                        },
                    },
                )
            elif parsed.path == "/api/workflow":
                self._json(HTTPStatus.OK, workflow_metadata())
            elif parsed.path == "/api/events/stream":
                self._serve_relay_events()
            elif parsed.path.startswith("/api/"):
                self._proxy("GET")
            else:
                self._serve_static(parsed.path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if parsed.path == "/_agent/v1/claim":
                agent_id = self._require_agent()
                payload = self._read_agent_json()
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                event_id = self.store.touch_agent(
                    agent_id,
                    str(payload.get("board_hash") or ""),
                    payload.get("metadata") or {},
                )
                deadline = time.monotonic() + self.relay_config.agent_poll_seconds
                if payload.get("wait_seconds") is not None:
                    deadline = time.monotonic() + max(
                        0, min(float(payload["wait_seconds"]), 25)
                    )
                command = self.store.claim(agent_id)
                while command is None and time.monotonic() < deadline:
                    time.sleep(0.25)
                    command = self.store.claim(agent_id)
                encoded = None
                if command:
                    encoded = {
                        **{key: command[key] for key in ("id", "method", "path", "headers")},
                        "body": base64.b64encode(command["body"]).decode("ascii"),
                    }
                self._json(HTTPStatus.OK, {"command": encoded, "event_id": event_id})
                return
            if parsed.path == "/_agent/v1/complete":
                agent_id = self._require_agent()
                payload = self._read_agent_json()
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                self.store.complete(
                    agent_id,
                    str(payload["command_id"]),
                    int(payload["status"]),
                    payload.get("headers") or {},
                    base64.b64decode(str(payload.get("body") or ""), validate=True),
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if parsed.path == "/_agent/v1/vault/file":
                agent_id = self._require_agent()
                payload = self._read_agent_json()
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                self.store.write_vault_file(
                    agent_id,
                    str(payload["path"]),
                    str(payload["sha256"]),
                    base64.b64decode(str(payload.get("content") or ""), validate=True),
                )
                self._json(HTTPStatus.OK, {"ok": True})
                return
            if parsed.path == "/_agent/v1/vault/manifest":
                agent_id = self._require_agent()
                payload = self._read_agent_json()
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                self.store.apply_vault_manifest(agent_id, payload.get("paths") or [])
                self._json(HTTPStatus.OK, {"ok": True})
                return
            self._validate_api_request()
            self._proxy("POST")
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def do_PATCH(self) -> None:  # noqa: N802
        try:
            self._validate_api_request()
            self._proxy("PATCH")
        except Exception as exc:
            self._handle_error(exc)


class RelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self._agent_sockets: dict[str, dict[int, WebSocketConnection]] = {}
        self._agent_sockets_lock = threading.Lock()
        self._next_agent_socket_id = 0

    def register_agent_socket(
        self, agent_id: str, connection: WebSocketConnection
    ) -> int:
        with self._agent_sockets_lock:
            self._next_agent_socket_id += 1
            registration = self._next_agent_socket_id
            self._agent_sockets.setdefault(agent_id, {})[registration] = connection
            return registration

    def unregister_agent_socket(self, agent_id: str, registration: int) -> None:
        with self._agent_sockets_lock:
            connections = self._agent_sockets.get(agent_id)
            if not connections:
                return
            connections.pop(registration, None)
            if not connections:
                self._agent_sockets.pop(agent_id, None)

    def notify_agent(self, agent_id: str, event: str) -> int:
        payload = json.dumps(
            {
                "type": event,
                "event_id": self.RequestHandlerClass.store.latest_event_id(),
            },
            ensure_ascii=False,
        ).encode("utf-8")
        with self._agent_sockets_lock:
            connections = list(self._agent_sockets.get(agent_id, {}).items())
        delivered = 0
        for registration, connection in connections:
            try:
                connection.send(payload)
                delivered += 1
            except OSError:
                self.unregister_agent_socket(agent_id, registration)
        return delivered


def build_relay_server(config: RelayConfig) -> RelayHTTPServer:
    server_config = config.server.validate()
    data_home = server_config.data_home or (
        Path.home() / ".local" / "share" / "DoTasks"
    )
    store = RelayStore(data_home)
    static_root = Path(__file__).resolve().parents[2] / "static"
    handler = type(
        "ConfiguredRelayHandler",
        (RelayHandler,),
        {
            "config": server_config,
            "relay_config": config,
            "store": store,
            "static_root": static_root,
        },
    )
    return RelayHTTPServer((server_config.host, server_config.port), handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the DoTasks cloud relay")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--home")
    parser.add_argument("--public-url")
    args = parser.parse_args()
    config = RelayConfig.from_environment(
        host=args.host,
        port=args.port,
        home=args.home,
        public_url=args.public_url,
    )
    server = build_relay_server(config)
    print(f"DoTasks cloud relay: {config.server.public_url}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
