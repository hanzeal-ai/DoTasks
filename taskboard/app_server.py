from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from core.runtime import project_runtime_environment

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9 and 3.10
    import tomli as tomllib

from .version import VERSION


class AppServerError(RuntimeError):
    pass


SHARED_CODEX_CONFIG_ENTRIES = (
    "auth.json",
    "AGENTS.md",
)

LEGACY_SHARED_CODEX_CONFIG_ENTRIES = (
    "hooks.json",
    "plugins",
    "prompts",
    "rules",
    "skills-disabled",
)
TASKBOARD_SKILLS = ("codex-taskboard-lifecycle",)
LEGACY_TASKBOARD_SKILLS = ("codex-taskboard",)
TASKBOARD_CONFIG_KEYS = ("model", "service_tier")
TASKBOARD_CONFIG_MARKER = "# --- Codex Taskboard managed configuration ---"
TASKBOARD_MCP_TOOLS = {
    "get_task_context",
    "transition_task",
    "report_run_blocked",
    "submit_task_delivery",
    "update_conversation_summary",
    "review_code",
    "run_acceptance_checks",
    "accept_task",
    "prepare_review_location",
    "prepare_task_review",
    "report_location_status",
    "complete_location_analysis",
    "review_task",
}

TASK_WORKER_BASE_INSTRUCTIONS = (
    "You are a focused Codex Taskboard worker. Use the supplied task context as the source of truth. "
    "Stay inside the confirmed scope, preserve unrelated work, make the smallest complete change, "
    "and run proportionate verification. Use only the lifecycle tools exposed for this stage."
)

TASK_WORKER_DEVELOPER_INSTRUCTIONS = (
    "Follow repository AGENTS.md instructions. Do not commit, push, publish, or perform destructive "
    "operations unless the task explicitly authorizes them. Complete the stage through its lifecycle tool."
)

APP_SERVER_INITIALIZE_ATTEMPTS = 2
APP_SERVER_INITIALIZE_RETRY_DELAY_SECONDS = 0.2


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() if configured else Path.home() / ".codex"


def prepare_taskboard_codex_home(
    codex_home: str | Path,
    shared_home: str | Path | None = None,
    taskboard_runtime_home: str | Path | None = None,
    taskboard_data_home: str | Path | None = None,
    python_executable: str | Path | None = None,
) -> Path:
    """Create an isolated session store while reusing non-session Codex configuration."""
    target_home = Path(codex_home).expanduser().resolve()
    target_home.mkdir(parents=True, exist_ok=True)
    source_home = (
        Path(shared_home).expanduser().resolve()
        if shared_home
        else default_codex_home().resolve()
    )
    if source_home == target_home:
        raise AppServerError(
            "Taskboard CODEX_HOME must be separate from the primary Codex home"
        )
    share_config = os.environ.get("CODEX_TASKBOARD_SHARE_CODEX_CONFIG", "1") != "0"
    for name in (*LEGACY_SHARED_CODEX_CONFIG_ENTRIES, *SHARED_CODEX_CONFIG_ENTRIES):
        source = source_home / name
        target = target_home / name
        if (
            target.is_symlink()
            and target.resolve() == source.resolve()
            and (name in LEGACY_SHARED_CODEX_CONFIG_ENTRIES or not share_config)
        ):
            target.unlink()
    if share_config:
        for name in SHARED_CODEX_CONFIG_ENTRIES:
            source = source_home / name
            target = target_home / name
            if not source.exists() or target.exists() or target.is_symlink():
                continue
            try:
                target.symlink_to(source, target_is_directory=source.is_dir())
            except (
                FileExistsError
            ):  # Another app-server may prepare the same home concurrently.
                pass

    runtime_home = (
        Path(taskboard_runtime_home).expanduser().resolve()
        if taskboard_runtime_home
        else Path(__file__).resolve().parents[1]
    )
    data_home = (
        Path(taskboard_data_home).expanduser().resolve()
        if taskboard_data_home
        else target_home.parent
    )
    python_bin = str(Path(python_executable or sys.executable).expanduser().resolve())
    _prepare_taskboard_skills(target_home, source_home, runtime_home)
    _prepare_taskboard_config(
        target_home,
        source_home if share_config else None,
        runtime_home,
        data_home,
        python_bin,
    )
    return target_home


def _prepare_taskboard_skills(
    target_home: Path, shared_home: Path | None, runtime_home: Path
) -> None:
    target = target_home / "skills"
    shared = shared_home / "skills" if shared_home else None
    if target.is_symlink():
        target.unlink()
    target.mkdir(parents=True, exist_ok=True)
    # Remove only symlinks created by older Taskboard builds. Real user-owned
    # files in the isolated home are left untouched.
    for destination in target.iterdir():
        if not destination.is_symlink() or destination.name in TASKBOARD_SKILLS:
            continue
        shared_source = shared / destination.name if shared else None
        legacy_source = runtime_home / "skills" / destination.name
        if (shared_source and destination.resolve() == shared_source.resolve()) or (
            destination.name in LEGACY_TASKBOARD_SKILLS
            and legacy_source.exists()
            and destination.resolve() == legacy_source.resolve()
        ):
            destination.unlink()
    for name in TASKBOARD_SKILLS:
        source = runtime_home / "skills" / name
        if not (source / "SKILL.md").is_file():
            raise AppServerError(f"Taskboard skill not found: {source}")
        destination = target / name
        if destination.is_symlink():
            if destination.resolve() == source:
                continue
            destination.unlink()
        elif destination.exists():
            shutil.rmtree(destination) if destination.is_dir() else destination.unlink()
        destination.symlink_to(source, target_is_directory=True)


def _prepare_taskboard_config(
    target_home: Path,
    shared_home: Path | None,
    runtime_home: Path,
    data_home: Path,
    python_executable: str,
) -> None:
    source = shared_home / "config.toml" if shared_home else None
    base_lines: list[str] = []
    if source and source.is_file():
        try:
            source_config = tomllib.loads(source.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            source_config = {}
        for key in TASKBOARD_CONFIG_KEYS:
            value = source_config.get(key)
            if isinstance(value, str):
                base_lines.append(f"{key} = {json.dumps(value)}")
    block = "\n".join(
        (
            TASKBOARD_CONFIG_MARKER,
            "[features]",
            "apps = false",
            "goals = false",
            "memories = false",
            "multi_agent = false",
            "plugins = false",
            "remote_plugin = false",
            "tool_suggest = false",
            "",
            "[mcp_servers.codex-taskboard]",
            'type = "stdio"',
            f"command = {json.dumps(python_executable)}",
            f"args = {json.dumps(['-B', '-m', 'taskboard.mcp_server'])}",
            f"cwd = {json.dumps(str(runtime_home))}",
            "startup_timeout_sec = 30",
            "enabled = true",
            "",
            "[mcp_servers.codex-taskboard.env]",
            f"CODEX_TASKBOARD_HOME = {json.dumps(str(data_home))}",
            f"PYTHONPATH = {json.dumps(str(runtime_home))}",
            'PYTHONDONTWRITEBYTECODE = "1"',
        )
    )
    target = target_home / "config.toml"
    if target.is_symlink():
        target.unlink()
    base = "\n".join(base_lines)
    content = (base + "\n\n" if base else "") + block + "\n"
    temporary = target.with_name(
        f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    )
    try:
        temporary.write_text(content, encoding="utf-8")
        temporary.chmod(0o600)
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def app_server_initialize_params() -> dict[str, Any]:
    return {
        "clientInfo": {
            "name": "codex-taskboard",
            "title": "Codex Taskboard",
            "version": VERSION,
        },
        "capabilities": {"experimentalApi": True},
    }


def task_thread_start_params(project: str | Path) -> dict[str, Any]:
    """Build the common parameters for a persisted, user-visible project thread."""
    project_path = str(Path(project).expanduser().resolve())
    return {
        "cwd": project_path,
        "runtimeWorkspaceRoots": [project_path],
        # Taskboard MCP calls require an elicitation response. Shell and
        # sandbox approvals stay disabled for this autonomous worker.
        "approvalPolicy": task_approval_policy(),
        "sandbox": "danger-full-access",
        "serviceName": "codex-taskboard",
        "threadSource": "user",
        "baseInstructions": TASK_WORKER_BASE_INSTRUCTIONS,
        "developerInstructions": TASK_WORKER_DEVELOPER_INSTRUCTIONS,
    }


def task_approval_policy() -> dict[str, Any]:
    return {
        "granular": {
            "mcp_elicitations": True,
            "rules": False,
            "sandbox_approval": False,
        }
    }


def task_turn_start_params(
    thread_id: str,
    prompt: str,
    run_type: str = "execution",
    high_risk: bool = False,
    low_risk: bool = False,
) -> dict[str, Any]:
    effort = (
        "high"
        if high_risk
        else ("low" if run_type == "acceptance" or low_risk else "medium")
    )
    return {
        "threadId": thread_id,
        "input": [{"type": "text", "text": prompt}],
        # turn/start overrides sticky settings on resumed legacy threads too.
        # Continuation and bugfix runs can inherit a source thread originally
        # created by a read-only client, so permissions must be explicit here.
        "approvalPolicy": task_approval_policy(),
        "sandboxPolicy": {"type": "dangerFullAccess"},
        "effort": effort,
    }


class CodexAppServerClient:
    """Small synchronous JSON-RPC client for a local Codex app-server process."""

    def __init__(
        self,
        executable: str | None = None,
        timeout: float = 30.0,
        codex_home: str | Path | None = None,
        shared_codex_home: str | Path | None = None,
        taskboard_runtime_home: str | Path | None = None,
        taskboard_data_home: str | Path | None = None,
        tool_profile: str = "",
        project: str | Path | None = None,
    ):
        default = "/Applications/ChatGPT.app/Contents/Resources/codex"
        self.executable = (
            executable
            or os.environ.get("CODEX_TASKBOARD_CODEX_BIN")
            or shutil.which("codex")
            or default
        )
        self.timeout = timeout
        configured_home = codex_home or os.environ.get("CODEX_TASKBOARD_CODEX_HOME")
        if configured_home is None:
            taskboard_home = os.environ.get("CODEX_TASKBOARD_HOME")
            base_home = (
                Path(taskboard_home).expanduser()
                if taskboard_home
                else Path.home() / "Library" / "Application Support" / "Codex Taskboard"
            )
            configured_home = base_home / "codex-home"
        self.codex_home = Path(configured_home).expanduser().resolve()
        self.shared_codex_home = (
            Path(shared_codex_home).expanduser().resolve()
            if shared_codex_home
            else default_codex_home().resolve()
        )
        self.taskboard_runtime_home = (
            Path(taskboard_runtime_home).expanduser().resolve()
            if taskboard_runtime_home
            else Path(__file__).resolve().parents[1]
        )
        self.taskboard_data_home = (
            Path(taskboard_data_home).expanduser().resolve()
            if taskboard_data_home
            else self.codex_home.parent
        )
        self.tool_profile = str(tool_profile or "").strip()
        self.project = Path(project).expanduser().resolve() if project else None
        self.process: subprocess.Popen[str] | None = None
        self._reader: threading.Thread | None = None
        self._stderr_reader: threading.Thread | None = None
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._notifications: queue.SimpleQueue[dict[str, Any]] = queue.SimpleQueue()
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._request_id = 0
        self.last_error = ""

    @property
    def connected(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.connected:
            return
        if not Path(self.executable).is_file() and not shutil.which(self.executable):
            raise AppServerError(f"Codex executable not found: {self.executable}")
        isolated_home = prepare_taskboard_codex_home(
            self.codex_home,
            self.shared_codex_home,
            self.taskboard_runtime_home,
            self.taskboard_data_home,
            sys.executable,
        )
        failures: list[str] = []
        for attempt in range(APP_SERVER_INITIALIZE_ATTEMPTS):
            self.last_error = ""
            try:
                self._start_connection(isolated_home)
                return
            except AppServerError as exc:
                failure = str(exc)
                failures.append(failure)
                self.stop()
                retryable = self._is_retryable_initialize_failure(failure)
                if not retryable or attempt + 1 >= APP_SERVER_INITIALIZE_ATTEMPTS:
                    if len(failures) == 1:
                        raise
                    raise AppServerError(
                        "Codex app-server initialize failed after "
                        f"{len(failures)} attempts: " + " | ".join(failures)
                    ) from exc
                time.sleep(APP_SERVER_INITIALIZE_RETRY_DELAY_SECONDS)

    def _start_connection(self, isolated_home: Path) -> None:
        environment = project_runtime_environment(self.project)
        environment["CODEX_HOME"] = str(isolated_home)
        if self.tool_profile:
            environment["CODEX_TASKBOARD_TOOL_PROFILE"] = self.tool_profile
        else:
            environment.pop("CODEX_TASKBOARD_TOOL_PROFILE", None)
        self.process = subprocess.Popen(
            [self.executable, "app-server", "--stdio"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
            env=environment,
        )
        process = self.process
        assert process is not None
        self._reader = threading.Thread(
            target=self._read_stdout,
            args=(process,),
            name="codex-app-server-reader",
            daemon=True,
        )
        self._stderr_reader = threading.Thread(
            target=self._read_stderr,
            args=(process,),
            name="codex-app-server-stderr",
            daemon=True,
        )
        self._reader.start()
        self._stderr_reader.start()
        self.request("initialize", app_server_initialize_params())
        self.notify("initialized", {})

    @staticmethod
    def _is_retryable_initialize_failure(message: str) -> bool:
        value = str(message or "")
        return (
            "initialize: Codex app-server connection closed" in value
            or "Codex app-server failed to start" in value
        )

    def stop(self) -> None:
        process = self.process
        self.process = None
        if process is None:
            return
        if process.stdin:
            process.stdin.close()
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
        self._fail_pending("Codex app-server stopped")

    def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        if not self.connected:
            if method == "initialize":
                raise AppServerError(
                    self._process_failure_message(
                        self.process,
                        "Codex app-server failed to start",
                    )
                )
            self.start()
        assert self.process is not None and self.process.stdin is not None
        response_queue: queue.Queue[dict[str, Any]] = queue.Queue(maxsize=1)
        with self._pending_lock:
            self._request_id += 1
            request_id = self._request_id
            self._pending[request_id] = response_queue
        message = json.dumps(
            {"id": request_id, "method": method, "params": params}, ensure_ascii=False
        )
        try:
            with self._write_lock:
                self.process.stdin.write(message + "\n")
                self.process.stdin.flush()
            response = response_queue.get(timeout=self.timeout)
        except Exception:
            with self._pending_lock:
                self._pending.pop(request_id, None)
            raise
        if "error" in response:
            error = response["error"]
            raise AppServerError(f"{method}: {error.get('message', error)}")
        return response.get("result") or {}

    def notify(self, method: str, params: dict[str, Any]) -> None:
        if not self.connected:
            raise AppServerError("Codex app-server is not connected")
        assert self.process is not None and self.process.stdin is not None
        message = json.dumps({"method": method, "params": params}, ensure_ascii=False)
        with self._write_lock:
            self.process.stdin.write(message + "\n")
            self.process.stdin.flush()

    def drain_notifications(self) -> list[dict[str, Any]]:
        messages: list[dict[str, Any]] = []
        while True:
            try:
                messages.append(self._notifications.get_nowait())
            except queue.Empty:
                return messages

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
                    response = self._server_request_response(message)
                    if response is not None:
                        self._write_message(response)
                    else:
                        self._notifications.put(message)
                    continue
                with self._pending_lock:
                    target = self._pending.pop(request_id, None)
                if target is not None:
                    target.put(message)
        finally:
            # A stopped reader from an earlier process must never fail requests
            # belonging to a freshly restarted connection.
            if self.process is process:
                self._fail_pending(
                    self._process_failure_message(
                        process,
                        "Codex app-server connection closed",
                    )
                )

    @staticmethod
    def _server_request_response(message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("method") != "mcpServer/elicitation/request":
            return None
        params = message.get("params") or {}
        metadata = params.get("_meta") or {}
        if (
            params.get("serverName") != "codex-taskboard"
            or metadata.get("codex_approval_kind") != "mcp_tool_call"
        ):
            return {"id": message.get("id"), "result": {"action": "decline"}}
        match = re.search(r'run tool "([^"]+)"', str(params.get("message") or ""))
        tool_name = match.group(1) if match else ""
        action = "accept" if tool_name in TASKBOARD_MCP_TOOLS else "decline"
        result: dict[str, Any] = {"action": action}
        if action == "accept":
            result["content"] = {}
        return {"id": message.get("id"), "result": result}

    def _write_message(self, payload: dict[str, Any]) -> None:
        process = self.process
        if process is None or process.stdin is None:
            return
        with self._write_lock:
            process.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
            process.stdin.flush()

    def _read_stderr(self, process: subprocess.Popen[str]) -> None:
        assert process.stderr is not None
        for line in process.stderr:
            value = line.strip()
            if value and self.process is process:
                self.last_error = value[-2000:]

    def _process_failure_message(
        self,
        process: subprocess.Popen[str] | None,
        message: str,
    ) -> str:
        if process is None:
            return message
        returncode = process.poll()
        if returncode is None:
            try:
                returncode = process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                returncode = None
        stderr_reader = self._stderr_reader
        if (
            stderr_reader is not None
            and stderr_reader is not threading.current_thread()
            and stderr_reader.is_alive()
        ):
            stderr_reader.join(timeout=0.05)
        details: list[str] = []
        if returncode is not None:
            details.append(f"exit_code={returncode}")
        if self.last_error:
            details.append(f"stderr={self.last_error}")
        return f"{message} ({'; '.join(details)})" if details else message

    def _fail_pending(self, message: str) -> None:
        with self._pending_lock:
            pending = list(self._pending.values())
            self._pending.clear()
        for target in pending:
            target.put({"error": {"message": message}})
