from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.14 is required in production.
    tomllib = None  # type: ignore[assignment]

from .version import VERSION


class AppServerError(RuntimeError):
    pass


LIFECYCLE_TOOLS = {
    "get_requirement",
    "submit_requirement_decomposition",
    "report_requirement_decomposition_failed",
    "renew_dispatch_lease",
    "get_dispatch_status",
    "submit_task_delivery",
    "report_run_blocked",
    "review_code",
}


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def prepare_worker_codex_home(
    data_home: str | Path,
    runtime_home: str | Path,
    python_executable: str | Path | None = None,
    shared_codex_home: str | Path | None = None,
) -> Path:
    """Prepare an isolated Codex store with only the DoTasks lifecycle MCP."""
    data_path = Path(data_home).expanduser().resolve()
    runtime_path = Path(runtime_home).expanduser().resolve()
    target = data_path / "codex-worker-home"
    target.mkdir(parents=True, exist_ok=True)
    shared = (
        Path(shared_codex_home).expanduser().resolve()
        if shared_codex_home
        else _default_codex_home()
    )

    source_auth = shared / "auth.json"
    target_auth = target / "auth.json"
    if source_auth.is_file() and not target_auth.exists():
        try:
            target_auth.symlink_to(source_auth)
        except FileExistsError:
            pass

    skill_source = runtime_path / "skills" / "dotasks-lifecycle"
    if not (skill_source / "SKILL.md").is_file():
        raise AppServerError(f"DoTasks lifecycle skill not found: {skill_source}")
    skills = target / "skills"
    skills.mkdir(exist_ok=True)
    skill_target = skills / "dotasks-lifecycle"
    if skill_target.is_symlink() and skill_target.resolve() != skill_source:
        skill_target.unlink()
    if not skill_target.exists():
        skill_target.symlink_to(skill_source, target_is_directory=True)

    base_lines: list[str] = []
    source_config = shared / "config.toml"
    if tomllib is not None and source_config.is_file():
        try:
            values = tomllib.loads(source_config.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            values = {}
        for key in ("model", "service_tier"):
            value = values.get(key)
            if isinstance(value, str) and value:
                base_lines.append(f"{key} = {json.dumps(value)}")

    python_bin = str(Path(python_executable or sys.executable).expanduser().resolve())
    config_lines = [
        *base_lines,
        "",
        "[mcp_servers.dotasks]",
        'type = "stdio"',
        f"command = {json.dumps(python_bin)}",
        f"args = {json.dumps(['-B', '-m', 'taskboard.mcp_server'])}",
        f"cwd = {json.dumps(str(runtime_path))}",
        "startup_timeout_sec = 30",
        "enabled = true",
        "",
        "[mcp_servers.dotasks.env]",
        f"DOTASKS_HOME = {json.dumps(str(data_path))}",
        'DOTASKS_REMOTE_SERVICE = "1"',
        f"PYTHONPATH = {json.dumps(str(runtime_path))}",
        'PYTHONDONTWRITEBYTECODE = "1"',
        "",
    ]
    config = target / "config.toml"
    temporary = config.with_name(f".{config.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    temporary.write_text("\n".join(config_lines), encoding="utf-8")
    temporary.chmod(0o600)
    os.replace(temporary, config)
    return target


class CodexAppServerClient:
    """Synchronous JSON-RPC client for one local Codex App Server worker."""

    def __init__(
        self,
        data_home: str | Path,
        runtime_home: str | Path,
        executable: str | None = None,
        timeout: float = 30.0,
    ):
        bundled = "/Applications/ChatGPT.app/Contents/Resources/codex"
        self.executable = (
            executable
            or os.environ.get("DOTASKS_CODEX_BIN")
            or shutil.which("codex")
            or bundled
        )
        self.data_home = Path(data_home).expanduser().resolve()
        self.runtime_home = Path(runtime_home).expanduser().resolve()
        self.timeout = timeout
        self.process: subprocess.Popen[str] | None = None
        self.last_error = ""
        self._request_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()

    @property
    def connected(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.connected:
            return
        if not Path(self.executable).is_file() and not shutil.which(self.executable):
            raise AppServerError(f"Codex executable not found: {self.executable}")
        worker_home = prepare_worker_codex_home(
            self.data_home, self.runtime_home, sys.executable
        )
        environment = os.environ.copy()
        environment["CODEX_HOME"] = str(worker_home)
        environment["DOTASKS_HOME"] = str(self.data_home)
        environment["PYTHONPATH"] = str(self.runtime_home)
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--listen", "stdio://"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )
        process = self.process
        threading.Thread(
            target=self._read_stdout,
            args=(process,),
            name="dotasks-codex-jsonrpc",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name="dotasks-codex-stderr",
            daemon=True,
        ).start()
        self.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "dotasks-agent",
                    "title": "DoTasks Local Agent",
                    "version": VERSION,
                },
                "capabilities": {"experimentalApi": True},
            },
        )
        self.notify("initialized", {})

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin:
            try:
                process.stdin.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._fail_pending("Codex App Server stopped")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.connected:
            if method != "initialize":
                raise AppServerError("Codex App Server is not connected")
            if self.process is None or self.process.poll() is not None:
                raise AppServerError("Codex App Server failed to start")
        assert self.process is not None and self.process.stdin is not None
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._request_id += 1
            request_id = self._request_id
            self._pending[request_id] = response_queue
        try:
            self._write({"id": request_id, "method": method, "params": params})
            response = response_queue.get(timeout=self.timeout)
        except queue.Empty as exc:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise AppServerError(f"{method}: timed out") from exc
        if "error" in response:
            error = response["error"]
            raise AppServerError(f"{method}: {error.get('message', error)}")
        result = response.get("result") or {}
        if not isinstance(result, dict):
            raise AppServerError(f"{method}: invalid response")
        return result

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.connected:
            raise AppServerError("Codex App Server is not connected")
        self._write({"method": method, "params": params})

    def wait_notification(self, timeout: float) -> dict[str, Any] | None:
        try:
            return self._notifications.get(timeout=timeout)
        except queue.Empty:
            return None

    def start_thread(self, project_path: str, title: str) -> str:
        result = self.request(
            "thread/start",
            {
                "cwd": str(Path(project_path).expanduser().resolve()),
                "ephemeral": False,
                "serviceName": "dotasks-agent",
                "threadSource": "dotasks",
                "approvalPolicy": {
                    "granular": {
                        "mcp_elicitations": True,
                        "rules": False,
                        "sandbox_approval": False,
                    }
                },
                "sandbox": "danger-full-access",
                "baseInstructions": (
                    "You are an autonomous DoTasks worker. Follow the persisted task prompt, "
                    "preserve unrelated changes, verify the result, and finish through the "
                    "DoTasks lifecycle callback."
                ),
                "developerInstructions": (
                    "Follow repository AGENTS.md instructions. Do not broaden scope, publish, "
                    "or perform destructive operations unless the task explicitly authorizes it."
                ),
            },
        )
        thread_id = str((result.get("thread") or {}).get("id") or "")
        if not thread_id:
            raise AppServerError("thread/start did not return a thread id")
        self.request("thread/name/set", {"threadId": thread_id, "name": title})
        return thread_id

    def resume_thread(self, thread_id: str) -> str:
        result = self.request("thread/resume", {"threadId": thread_id})
        resumed_id = str((result.get("thread") or {}).get("id") or "")
        if not resumed_id:
            raise AppServerError("thread/resume did not return a thread id")
        return resumed_id

    def start_turn(self, thread_id: str, prompt: str) -> str:
        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": [{"type": "text", "text": prompt}],
                "approvalPolicy": {
                    "granular": {
                        "mcp_elicitations": True,
                        "rules": False,
                        "sandbox_approval": False,
                    }
                },
                "sandboxPolicy": {"type": "dangerFullAccess"},
                "effort": "medium",
                "turnTrigger": "dotasks_dispatch",
            },
        )
        turn_id = str((result.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AppServerError("turn/start did not return a turn id")
        return turn_id

    def _write(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            raise AppServerError("Codex App Server is not connected")
        with self._write_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _read_stdout(self, process: subprocess.Popen[str]) -> None:
        assert process.stdout is not None
        try:
            for line in process.stdout:
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                request_id = message.get("id")
                if request_id is None:
                    self._notifications.put(message)
                    continue
                if message.get("method"):
                    self._respond_to_server_request(message)
                    continue
                with self._pending_lock:
                    target = self._pending.pop(request_id, None)
                if target is not None:
                    target.put(message)
        finally:
            if self.process is process:
                detail = f": {self.last_error}" if self.last_error else ""
                self._fail_pending(f"Codex App Server connection closed{detail}")

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            value = line.strip()
            if value and self.process is process:
                self.last_error = value[-2000:]

    def _respond_to_server_request(self, message: dict[str, Any]) -> None:
        method = str(message.get("method") or "")
        if method != "mcpServer/elicitation/request":
            self._write({"id": message.get("id"), "result": {"decision": "decline"}})
            return
        params = message.get("params") or {}
        metadata = params.get("_meta") or {}
        server_name = str(params.get("serverName") or "")
        match = re.search(r'run tool "([^"]+)"', str(params.get("message") or ""))
        tool_name = match.group(1) if match else ""
        accepted = (
            server_name == "dotasks"
            and metadata.get("codex_approval_kind") == "mcp_tool_call"
            and tool_name in LIFECYCLE_TOOLS
        )
        result: dict[str, Any] = {"action": "accept" if accepted else "decline"}
        if accepted:
            result["content"] = {}
        self._write({"id": message.get("id"), "result": result})

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for target in pending:
            target.put({"error": {"message": message}})
