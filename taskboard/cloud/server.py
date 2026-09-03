from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import sqlite3
import subprocess
import threading
import time
import zlib
from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from core.db import Database
from core.service import TaskboardService
from taskboard.config import CLOUD_MODE, ServerConfig
from taskboard.http_base import MAX_JSON_BODY_BYTES
from taskboard.http_security import HTTPRequestError
from taskboard.mcp_server import tool_handlers_for
from taskboard.project_guard import ProjectWorkspaceGuard
from taskboard.codex_projects import sanitize_codex_projects
from taskboard.server import TaskboardHandler
from taskboard.websocket_transport import (
    WebSocketConnection,
    read_frame,
    websocket_accept,
)

from .store import RelayStore


AGENT_BODY_LIMIT = MAX_JSON_BODY_BYTES * 8
SNAPSHOT_BODY_LIMIT = 90 * 1024 * 1024


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


class CloudProjectWorkspaceGuard(ProjectWorkspaceGuard):
    """Keep Mac project paths as identifiers without reading them on the cloud host."""

    @staticmethod
    def require_project_directory(project: str | Path | None) -> str:
        value = str(project or "").strip()
        if not value:
            raise ValueError("project is required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("project must be an absolute path")
        return str(path.resolve())

    def workspace_state(self, project: str | None) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "workspace_owned_by_local_agent",
            "project": self.normalize_project(project),
        }

    def workspace_diff(
        self, project: str | None, baseline_revision: str, paths: list[str],
        max_chars: int = 80000,
    ) -> dict[str, Any]:
        return {
            "available": False,
            "reason": "workspace_owned_by_local_agent",
            "project": self.normalize_project(project),
        }

    @staticmethod
    def git(project: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            ["git", "-C", project, *arguments], 1, "", "workspace is local"
        )


class CloudTaskboardService(TaskboardService):
    def __init__(self, home: str | Path, public_url: str):
        super().__init__(home, workspace_guard=CloudProjectWorkspaceGuard())
        self.taskboard_url = public_url.rstrip("/")
        self._import_lock = threading.Lock()

    def _target_snippet(
        self, project: str | None, target: dict[str, Any], line_count: int | None = None,
    ) -> dict[str, Any] | None:
        return None

    def parallel_development_enabled(self) -> bool:
        # Worktree creation belongs to the Local Agent. Until workspace RPC is
        # available, cloud scheduling must never run concurrent workers in the
        # same local checkout.
        return False

    def has_managed_state(self) -> bool:
        with self.db.connection() as connection:
            return bool(
                connection.execute("SELECT 1 FROM requirements LIMIT 1").fetchone()
                or connection.execute("SELECT 1 FROM tasks LIMIT 1").fetchone()
            )

    def import_initial_database(self, content: bytes, sha256: str) -> bool:
        with self._import_lock:
            if self.has_managed_state():
                return False
            if not content or len(content) > 64 * 1024 * 1024:
                raise ValueError("Taskboard snapshot must contain at most 64 MiB")
            if hashlib.sha256(content).hexdigest() != sha256:
                raise ValueError("Taskboard snapshot SHA-256 does not match")
            target = self.db.path
            temporary = target.with_name(f".{target.name}.import")
            migration_lock = temporary.with_suffix(temporary.suffix + ".migrate.lock")
            temporary.write_bytes(content)
            try:
                Database(temporary)
                with closing(sqlite3.connect(temporary)) as connection:
                    if connection.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise ValueError("Taskboard snapshot failed SQLite integrity check")
                    tables = {
                        row[0]
                        for row in connection.execute(
                            "SELECT name FROM sqlite_master WHERE type='table'"
                        )
                    }
                    if not {"requirements", "tasks", "scheduler_state"}.issubset(tables):
                        raise ValueError("Taskboard snapshot schema is incomplete")
                target.with_name(target.name + "-wal").unlink(missing_ok=True)
                target.with_name(target.name + "-shm").unlink(missing_ok=True)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)
                migration_lock.unlink(missing_ok=True)
            return True


class RelayHandler(TaskboardHandler):
    relay_config: RelayConfig
    store: RelayStore

    def _dispatcher_status(self) -> dict[str, Any]:
        agent = self.store.agent_status(self.relay_config.agent_id)
        return {
            "control_plane": "cloud",
            "enabled": self.service.dispatcher_enabled(),
            "execution_mode": "codex_cli_app_server",
            "agent_configured": bool(agent.get("last_seen_at")),
            "running": bool(agent.get("online")),
            "last_seen_at": str(agent.get("last_seen_at") or ""),
        }

    def _codex_projects(self) -> list[dict[str, str]]:
        agent = self.store.agent_status(self.relay_config.agent_id)
        metadata = agent.get("metadata") or {}
        return sanitize_codex_projects(metadata.get("codex_projects"))

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

    def _read_agent_json(self, limit: int = AGENT_BODY_LIMIT) -> dict[str, Any]:
        return self._read_json(limit)

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
            super().do_GET()
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
            if parsed.path == "/_agent/v1/bootstrap/status":
                agent_id = self._require_agent()
                payload = self._read_agent_json()
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                self._json(
                    HTTPStatus.OK,
                    {"accept_snapshot": not self.service.has_managed_state()},
                )
                return
            if parsed.path == "/_agent/v1/bootstrap":
                agent_id = self._require_agent()
                payload = self._read_agent_json(SNAPSHOT_BODY_LIMIT)
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                if payload.get("encoding") != "zlib+base64":
                    raise ValueError("Unsupported taskboard snapshot encoding")
                compressed = base64.b64decode(
                    str(payload.get("content") or ""), validate=True
                )
                decompressor = zlib.decompressobj()
                content = decompressor.decompress(compressed, 64 * 1024 * 1024 + 1)
                if len(content) > 64 * 1024 * 1024 or decompressor.unconsumed_tail:
                    raise ValueError("Taskboard snapshot expands beyond 64 MiB")
                content += decompressor.flush()
                if (
                    len(content) > 64 * 1024 * 1024
                    or not decompressor.eof
                    or decompressor.unused_data
                ):
                    raise ValueError("Taskboard snapshot compression is invalid")
                imported = self.service.import_initial_database(
                    content, str(payload.get("sha256") or "")
                )
                self._json(HTTPStatus.OK, {"imported": imported})
                if imported:
                    self.relay_server.notify_agent(agent_id, "state_changed")
                return
            if parsed.path == "/_agent/v1/tools/call":
                agent_id = self._require_agent()
                payload = self._read_agent_json()
                if payload.get("agent_id") != agent_id:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Agent ID mismatch")
                name = str(payload.get("name") or "")
                handlers = tool_handlers_for(self.service)
                handler = handlers.get(name)
                if handler is None:
                    raise ValueError(f"Unknown tool: {name}")
                result = handler(payload.get("arguments") or {})
                self._json(HTTPStatus.OK, {"result": result})
                self.relay_server.notify_agent(agent_id, "state_changed")
                return
            super().do_POST()
            self.relay_server.notify_agent(
                self.relay_config.agent_id, "state_changed"
            )
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def do_PATCH(self) -> None:  # noqa: N802
        try:
            super().do_PATCH()
            self.relay_server.notify_agent(
                self.relay_config.agent_id, "state_changed"
            )
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
    service = CloudTaskboardService(data_home, server_config.public_url)
    static_root = Path(__file__).resolve().parents[2] / "static"
    handler = type(
        "ConfiguredRelayHandler",
        (RelayHandler,),
        {
            "config": server_config,
            "relay_config": config,
            "store": store,
            "service": service,
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
