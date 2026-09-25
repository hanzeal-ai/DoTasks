from __future__ import annotations

import argparse
import base64
import hashlib
import http.client
import json
import os
import platform
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zlib
from contextlib import closing
from dataclasses import dataclass
from http import HTTPStatus
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .http_client import NoRedirect
from .runtime_paths import default_data_home, default_config_path
from .local_executor import LocalCodexExecutor
from .codex_projects import discover_codex_projects
from .remote_service import RemoteTaskboardService, RemoteToolClient
from .version import VERSION
from .websocket_transport import connect_websocket, encode_frame, read_frame


@dataclass(frozen=True)
class AgentConfig:
    cloud_url: str
    agent_id: str
    agent_token: str
    local_url: str = "http://127.0.0.1:8765"
    vault: str = ""
    data_home: str = ""

    def validate(self) -> "AgentConfig":
        for label, value in (("cloud_url", self.cloud_url), ("local_url", self.local_url)):
            parsed = urlparse(value)
            if parsed.scheme not in {"http", "https"} or not parsed.hostname:
                raise ValueError(f"{label} must be an absolute http(s) URL")
            if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
                raise ValueError(f"{label} must not contain a path, query, or fragment")
        if not self.agent_id or len(self.agent_id) > 100:
            raise ValueError("agent_id must contain 1 to 100 characters")
        if len(self.agent_token) < 24:
            raise ValueError("agent_token must contain at least 24 characters")
        return self

    @property
    def vault_path(self) -> Path:
        if self.vault:
            return Path(self.vault).expanduser().resolve()
        home = Path(self.data_home).expanduser().resolve()
        return home / "data" / "obsidian-vault"


def load_agent_config(path: Path | None = None) -> AgentConfig:
    target = path or default_config_path()
    stored: dict[str, Any] = {}
    if target.is_file():
        stored = json.loads(target.read_text(encoding="utf-8"))
    data_home = str(default_data_home())
    return AgentConfig(
        cloud_url=str(os.environ.get("DOTASKS_CLOUD_URL") or stored.get("cloud_url") or ""),
        agent_id=str(os.environ.get("DOTASKS_AGENT_ID") or stored.get("agent_id") or "default"),
        agent_token=str(os.environ.get("DOTASKS_AGENT_TOKEN") or stored.get("agent_token") or ""),
        local_url=str(os.environ.get("DOTASKS_LOCAL_URL") or stored.get("local_url") or "http://127.0.0.1:8765"),
        vault=str(os.environ.get("DOTASKS_OBSIDIAN_VAULT") or stored.get("vault") or ""),
        data_home=data_home,
    ).validate()


def save_agent_config(config: AgentConfig, path: Path | None = None) -> Path:
    config.validate()
    target = path or default_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "cloud_url": config.cloud_url,
                "agent_id": config.agent_id,
                "agent_token": config.agent_token,
                "local_url": config.local_url,
                "vault": config.vault,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    target.chmod(0o600)
    return target


class RelayAgent:
    def __init__(
        self,
        config: AgentConfig,
        executor: LocalCodexExecutor | None = None,
    ):
        self.config = config.validate()
        self.uploaded_vault_files: dict[str, str] = {}
        self.next_vault_sync_at = 0.0
        self.pending_result = self._load_pending_result()
        from .team_agent import TeamAgent
        self.team_agent = TeamAgent(self.config)
        self.executor = executor or LocalCodexExecutor(
            self.config.data_home,
            service=RemoteTaskboardService(
                RemoteToolClient(
                    self.config.cloud_url,
                    self.config.agent_id,
                    self.config.agent_token,
                )
            ),
        )

    @property
    def pending_result_path(self) -> Path:
        return Path(self.config.data_home) / "cloud-agent-pending.json"

    def _load_pending_result(self) -> dict[str, Any] | None:
        path = self.pending_result_path
        if not path.is_file():
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or not payload.get("command_id"):
            raise ValueError("Invalid cloud agent pending result")
        return payload

    def _save_pending_result(
        self, command_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        payload = {
            "agent_id": self.config.agent_id,
            "command_id": command_id,
            "status": int(result["status"]),
            "headers": result["headers"],
            "body": base64.b64encode(result["body"]).decode("ascii"),
        }
        path = self.pending_result_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8"
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        self.pending_result = payload
        return payload

    def _clear_pending_result(self) -> None:
        self.pending_result_path.unlink(missing_ok=True)
        self.pending_result = None

    def _cloud_request(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            self.config.cloud_url.rstrip("/") + path,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.agent_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with urllib.request.build_opener(NoRedirect()).open(request, timeout=35) as response:
            body = response.read()
        result = json.loads(body or b"{}")
        if not isinstance(result, dict):
            raise RuntimeError("Cloud relay returned a non-object response")
        return result

    def _local_request(
        self,
        method: str,
        path: str,
        headers: dict[str, str] | None = None,
        body: bytes = b"",
    ) -> dict[str, Any]:
        parsed = urlparse(self.config.local_url)
        connection_type = (
            http.client.HTTPSConnection if parsed.scheme == "https" else http.client.HTTPConnection
        )
        connection = connection_type(
            parsed.hostname,
            parsed.port,
            timeout=65,
        )
        request_headers = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() in {"content-type"}
        }
        connection.request(method, path, body=body or None, headers=request_headers)
        response = connection.getresponse()
        response_body = response.read()
        content_type = response.headers.get("Content-Type")
        connection.close()
        return {
            "status": response.status,
            "headers": {"Content-Type": content_type} if content_type else {},
            "body": response_body,
        }

    def board_hash(self) -> str:
        try:
            result = self._local_request("GET", "/api/board")
        except OSError:
            return ""
        if result["status"] != HTTPStatus.OK:
            return ""
        return hashlib.sha256(result["body"]).hexdigest()

    def metadata(self) -> dict[str, Any]:
        return {
            "version": VERSION,
            "hostname": socket.gethostname(),
            "platform": platform.system().lower(),
            "python": platform.python_version(),
            "codex_projects": discover_codex_projects(),
        }

    def bootstrap_cloud_state(self) -> bool:
        try:
            status = self._cloud_request(
                "/_agent/v1/bootstrap/status", {"agent_id": self.config.agent_id}
            )
        except urllib.error.HTTPError as exc:
            if exc.code == HTTPStatus.NOT_FOUND:
                return False
            raise
        if not status.get("accept_snapshot"):
            return False
        source = Path(self.config.data_home) / "data" / "taskboard.db"
        if not source.is_file():
            return False
        with tempfile.TemporaryDirectory() as temporary:
            snapshot = Path(temporary) / "taskboard.db"
            with closing(sqlite3.connect(source)) as source_connection:
                with closing(sqlite3.connect(snapshot)) as target_connection:
                    source_connection.backup(target_connection)
            content = snapshot.read_bytes()
        response = self._cloud_request(
            "/_agent/v1/bootstrap",
            {
                "agent_id": self.config.agent_id,
                "sha256": hashlib.sha256(content).hexdigest(),
                "content": base64.b64encode(zlib.compress(content, level=9)).decode("ascii"),
                "encoding": "zlib+base64",
            },
        )
        return bool(response.get("imported"))

    def execute_command(self, command: dict[str, Any]) -> None:
        command_id = str(command["id"])
        path = str(command.get("path") or "")
        method = str(command.get("method") or "").upper()
        if not path.startswith("/api/") or method not in {"GET", "POST", "PATCH"}:
            result = {
                "status": HTTPStatus.BAD_REQUEST,
                "headers": {"Content-Type": "application/json; charset=utf-8"},
                "body": json.dumps({"error": "Unsupported relay command"}).encode(),
            }
        elif path == "/api/agent/team-workspace" and method == "POST":
            from .team_local import workspace_request
            try:
                payload = json.loads(base64.b64decode(str(command.get("body") or ""), validate=True))
                response = workspace_request(self.config.data_home, payload)
                result = {"status": 200, "headers": {"Content-Type": "application/json"},
                          "body": json.dumps(response).encode()}
            except (ValueError, TypeError, KeyError, OSError, subprocess.SubprocessError):
                result = {"status": 400, "headers": {}, "body": b'{"error":"Workspace validation failed"}'}
        elif path == "/api/agent/verify" and method == "POST":
            from core.service.review import TaskReviewMixin
            from .project_guard import ProjectWorkspaceGuard

            try:
                payload = json.loads(base64.b64decode(str(command.get("body") or ""), validate=True))
                project = ProjectWorkspaceGuard.require_project_directory(payload.get("project"))
                verification = payload.get("command")
                timeout = int(payload.get("timeout_seconds", 60))
                if not isinstance(verification, str) or not verification.strip() or len(verification) > 100000 or not 1 <= timeout <= 60:
                    raise ValueError("Invalid local verification request")
                status, code, output, duration = TaskReviewMixin._run_project_command(verification, project, timeout)
                result = {"status": HTTPStatus.OK, "headers": {"Content-Type": "application/json"},
                          "body": json.dumps({"status": status, "exit_code": code, "output": output[:80000], "duration_ms": duration}).encode()}
            except (ValueError, TypeError, KeyError, AttributeError, OSError):
                result = {"status": HTTPStatus.BAD_REQUEST, "headers": {}, "body": b'{"error":"Invalid verification request"}'}
        else:
            result = self._local_request(
                method,
                path,
                command.get("headers") or {},
                base64.b64decode(str(command.get("body") or ""), validate=True),
            )
        completion = self._save_pending_result(command_id, result)
        self._cloud_request("/_agent/v1/complete", completion)
        self._clear_pending_result()

    def sync_vault(self) -> None:
        root = self.config.vault_path
        documents = root / "DoTasks"
        manifest: list[str] = []
        current: dict[str, str] = {}
        if documents.is_dir():
            for path in sorted(documents.rglob("*.md")):
                if path.is_symlink() or not path.is_file():
                    continue
                relative = path.relative_to(root).as_posix()
                content = path.read_bytes()
                digest = hashlib.sha256(content).hexdigest()
                manifest.append(relative)
                current[relative] = digest
                if self.uploaded_vault_files.get(relative) == digest:
                    continue
                self._cloud_request(
                    "/_agent/v1/vault/file",
                    {
                        "agent_id": self.config.agent_id,
                        "path": relative,
                        "sha256": digest,
                        "content": base64.b64encode(content).decode("ascii"),
                    },
                )
        self._cloud_request(
            "/_agent/v1/vault/manifest",
            {"agent_id": self.config.agent_id, "paths": manifest},
        )
        self.uploaded_vault_files = current

    def run_once(self) -> bool:
        if self.pending_result is not None:
            self._cloud_request("/_agent/v1/complete", self.pending_result)
            self._clear_pending_result()
        if time.monotonic() >= self.next_vault_sync_at:
            self.sync_vault()
            self.next_vault_sync_at = time.monotonic() + 60
        response = self._cloud_request(
            "/_agent/v1/claim",
            {
                "agent_id": self.config.agent_id,
                "board_hash": self.board_hash(),
                "metadata": self.metadata(),
                "wait_seconds": 0,
            },
        )
        command = response.get("command")
        if command:
            self.execute_command(command)
            return True
        return False

    def drain_commands(self) -> int:
        completed = 0
        while self.run_once():
            completed += 1
        return completed

    def run_event_stream_once(self) -> None:
        if self.pending_result is not None:
            self._cloud_request("/_agent/v1/complete", self.pending_result)
            self._clear_pending_result()
        self.bootstrap_cloud_state()
        if time.monotonic() >= self.next_vault_sync_at:
            self.sync_vault()
            self.next_vault_sync_at = time.monotonic() + 60
        connection, stream = connect_websocket(
            self.config.cloud_url, self.config.agent_token
        )
        try:
            while True:
                opcode, payload = read_frame(stream)
                if opcode == 0x8:
                    return
                if opcode == 0x9:
                    connection.sendall(encode_frame(payload, opcode=0xA, masked=True))
                    continue
                if opcode != 0x1:
                    continue
                event = json.loads(payload.decode("utf-8"))
                if event.get("type") in {
                    "connected",
                    "command_available",
                    "state_changed",
                }:
                    self.drain_commands()
                    self.executor.wake()
                    self.team_agent.wake()
        finally:
            stream.close()
            connection.close()

    def run_forever(self) -> None:
        failures = 0
        self.executor.start()
        self.team_agent.start()
        try:
            while True:
                try:
                    self.run_event_stream_once()
                    failures = 0
                except (
                    ConnectionError,
                    OSError,
                    ValueError,
                    RuntimeError,
                    urllib.error.URLError,
                ) as exc:
                    failures += 1
                    delay = min(30, 2 ** min(failures, 5))
                    print(f"[dotasks-agent] {exc}; retrying in {delay}s", file=sys.stderr)
                    time.sleep(delay)
        finally:
            self.executor.stop()
            self.team_agent.stop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local DoTasks cloud agent")
    parser.add_argument(
        "--wait-for-config",
        action="store_true",
        help="wait until cloud-agent.json has been configured",
    )
    subparsers = parser.add_subparsers(dest="command")
    configure = subparsers.add_parser("configure")
    configure.add_argument("--cloud-url", required=True)
    configure.add_argument("--agent-token", required=True)
    configure.add_argument("--agent-id", default="default")
    configure.add_argument("--local-url", default="http://127.0.0.1:8765")
    configure.add_argument("--vault", default="")
    args = parser.parse_args()

    def stop_agent(_signum: int, _frame: Any) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_agent)
    signal.signal(signal.SIGINT, stop_agent)

    if args.command == "configure":
        config = AgentConfig(
            cloud_url=args.cloud_url,
            agent_id=args.agent_id,
            agent_token=args.agent_token,
            local_url=args.local_url,
            vault=args.vault,
            data_home=str(default_data_home()),
        )
        target = save_agent_config(config)
        print(f"DoTasks cloud agent configured: {target}")
        return

    if args.wait_for_config:
        while True:
            try:
                config = load_agent_config()
                break
            except (OSError, ValueError, json.JSONDecodeError):
                time.sleep(5)
    else:
        config = load_agent_config()
    try:
        RelayAgent(config).run_forever()
    except KeyboardInterrupt:
        return


if __name__ == "__main__":
    main()
