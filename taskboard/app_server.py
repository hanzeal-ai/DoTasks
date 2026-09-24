from __future__ import annotations

import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.14 is required in production.
    tomllib = None  # type: ignore[assignment]

from .version import VERSION


class AppServerError(RuntimeError):
    pass


@dataclass(frozen=True)
class WorkerExecutionPolicy:
    """Authoritative permissions contract for autonomous DoTasks workers."""

    sandbox_mode: str = "danger-full-access"
    sandbox_policy_type: str = "dangerFullAccess"
    approval_policy_name: str = "never"

    @staticmethod
    def approval_policy() -> dict[str, Any]:
        return {
            "granular": {
                "mcp_elicitations": True,
                "rules": False,
                "sandbox_approval": False,
            }
        }

    def thread_overrides(self) -> dict[str, Any]:
        return {
            "approvalPolicy": self.approval_policy(),
            "sandbox": self.sandbox_mode,
        }

    def turn_overrides(self) -> dict[str, Any]:
        return {
            "approvalPolicy": self.approval_policy(),
            "sandboxPolicy": {"type": self.sandbox_policy_type},
        }


WORKER_EXECUTION_POLICY = WorkerExecutionPolicy()


LIFECYCLE_TOOLS = {
    "get_requirement",
    "prepare_task_location",
    "report_location_status",
    "complete_location_analysis",
    "submit_requirement_decomposition",
    "report_requirement_decomposition_failed",
    "renew_dispatch_lease",
    "get_dispatch_status",
    "submit_task_delivery",
    "report_run_blocked",
    "review_code",
}

THREAD_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}")


def _default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def _latest_session_index_record(index: Path, thread_id: str) -> dict[str, Any] | None:
    if not index.is_file():
        return None
    latest: dict[str, Any] | None = None
    try:
        with index.open("r", encoding="utf-8") as source:
            for line in source:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(record, dict) and str(record.get("id") or "") == thread_id:
                    latest = record
    except OSError as exc:
        raise AppServerError(f"Cannot read Codex session index: {index}: {exc}") from exc
    return latest


def sync_worker_thread_to_shared_home(
    worker_codex_home: str | Path,
    shared_codex_home: str | Path,
    thread_id: str,
    thread_name: str = "",
) -> Path:
    """Copy one completed worker session into the desktop Codex session store."""
    normalized_id = str(thread_id or "").strip()
    if not THREAD_ID_PATTERN.fullmatch(normalized_id):
        raise AppServerError("Cannot sync an invalid Codex thread id")

    worker = Path(worker_codex_home).expanduser().resolve()
    shared = Path(shared_codex_home).expanduser().resolve()
    candidates = [
        candidate
        for root_name in ("sessions", "archived_sessions")
        for candidate in (worker / root_name).glob(
            f"**/rollout-*-{normalized_id}.jsonl"
        )
        if candidate.is_file()
    ]
    if not candidates:
        raise AppServerError(f"Worker session file not found for thread {normalized_id}")
    source = max(candidates, key=lambda candidate: candidate.stat().st_mtime_ns)
    relative = source.relative_to(worker)
    destination = shared / relative
    destination.parent.mkdir(parents=True, exist_ok=True)

    same_file = False
    if destination.exists():
        try:
            same_file = source.samefile(destination)
        except OSError:
            same_file = False
    temporary: Path | None = None
    if not same_file:
        temporary = destination.with_name(
            f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            shutil.copy2(source, temporary)
        except OSError as exc:
            raise AppServerError(
                f"Cannot sync Codex worker session {normalized_id}: {exc}"
            ) from exc

    worker_record = _latest_session_index_record(
        worker / "session_index.jsonl", normalized_id
    ) or {}
    resolved_name = str(thread_name or worker_record.get("thread_name") or "").strip()
    shared_index = shared / "session_index.jsonl"
    current_record = _latest_session_index_record(shared_index, normalized_id)
    current_name = str((current_record or {}).get("thread_name") or "").strip()
    if current_record is None or (resolved_name and current_name != resolved_name):
        shared.mkdir(parents=True, exist_ok=True)
        record = dict(worker_record)
        record.update(
            {
                "id": normalized_id,
                "thread_name": resolved_name or f"DoTasks {normalized_id}",
                "updated_at": str(
                    worker_record.get("updated_at")
                    or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
                ),
            }
        )
        encoded = (json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        try:
            descriptor = os.open(
                shared_index, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600
            )
            try:
                os.write(descriptor, encoded)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
        except OSError as exc:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass
            raise AppServerError(
                f"Cannot update shared Codex session index: {shared_index}: {exc}"
            ) from exc
    if temporary is not None:
        try:
            # Publish the visible rollout only after its title is indexed. The
            # desktop may cache a title-less task as soon as this path appears.
            os.replace(temporary, destination)
        except OSError as exc:
            raise AppServerError(
                f"Cannot sync Codex worker session {normalized_id}: {exc}"
            ) from exc
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass
    return destination


def prepare_worker_codex_home(
    data_home: str | Path,
    runtime_home: str | Path,
    python_executable: str | Path | None = None,
    shared_codex_home: str | Path | None = None,
    *, readonly: bool = False, readonly_executable: str | None = None,
) -> Path:
    """Prepare an isolated Codex store with only the DoTasks callback MCP."""
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

    skills = target / "skills"
    skills.mkdir(exist_ok=True)
    skill_target = skills / "dotasks-lifecycle"
    if skill_target.is_symlink():
        skill_target.unlink()

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
        f"approval_policy = {json.dumps(WORKER_EXECUTION_POLICY.approval_policy_name)}",
        f"sandbox_mode = {json.dumps(WORKER_EXECUTION_POLICY.sandbox_mode)}",
        "",
        "[mcp_servers.dotasks]",
        f"command = {json.dumps(python_bin)}",
        f"args = {json.dumps(['-B', '-m', 'taskboard.mcp_server'])}",
        f"cwd = {json.dumps(str(runtime_path))}",
        "startup_timeout_sec = 30",
        "enabled = true",
        "",
        "[mcp_servers.dotasks.env]",
        f"DOTASKS_HOME = {json.dumps(str(data_path))}",
        f"DOTASKS_AGENT_CONFIG = {json.dumps(str(data_path / 'cloud-agent.json'))}",
        'DOTASKS_REMOTE_SERVICE = "1"',
        f"PYTHONPATH = {json.dumps(str(runtime_path))}",
        'PYTHONDONTWRITEBYTECODE = "1"',
        "",
    ]
    if readonly:
        executable_paths=[]
        if readonly_executable:
            binary=Path(shutil.which(readonly_executable) or readonly_executable).expanduser().absolute()
            parents=sorted({binary.parent,binary.resolve().parent})
            if any(parent in {Path('/'),Path.home(),Path.home().parent} for parent in parents):
                raise ValueError('Codex must be installed in a dedicated executable directory for read-only analysis')
            executable_paths=[f'{json.dumps(str(parent))} = "read"' for parent in parents]
        config_lines = [*base_lines, 'approval_policy = "never"',
                        'default_permissions = "dotasks-analysis"', 'web_search = "disabled"',
                        '[permissions.dotasks-analysis.filesystem]', '":root" = "deny"',
                        '":minimal" = "read"',
                        *executable_paths,
                        '[permissions.dotasks-analysis.filesystem.":workspace_roots"]', '"." = "read"',
                        '[permissions.dotasks-analysis.network]', 'enabled = false',
                        '[features]', 'apps = false', 'plugins = false', 'multi_agent = false',
                        '[shell_environment_policy]', 'inherit = "none"', '']
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
        readonly: bool = False,
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
        self.shared_codex_home = _default_codex_home()
        self.worker_codex_home: Path | None = None
        self.timeout = timeout
        self.readonly = readonly
        self.process: subprocess.Popen[str] | None = None
        self.last_error = ""
        self._request_id = 0
        self._pending: dict[int, queue.Queue[dict[str, Any]]] = {}
        self._pending_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._notifications: queue.Queue[dict[str, Any]] = queue.Queue()
        self._threads_to_sync: dict[str, str] = {}

    @property
    def connected(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> None:
        if self.connected:
            return
        if not Path(self.executable).is_file() and not shutil.which(self.executable):
            raise AppServerError(f"Codex executable not found: {self.executable}")
        worker_home = prepare_worker_codex_home(
            self.data_home,
            self.runtime_home,
            sys.executable,
            shared_codex_home=self.shared_codex_home,
            readonly=self.readonly,
            readonly_executable=self.executable if self.readonly else None,
        )
        self.worker_codex_home = worker_home
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
                process.wait(timeout=3)
        for stream in (process.stdout,process.stderr):
            if stream:
                stream.close()
        self._fail_pending("Codex App Server stopped")
        self._sync_completed_threads()

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
                **WORKER_EXECUTION_POLICY.thread_overrides(),
                "baseInstructions": (
                    "You are an autonomous DoTasks worker. Treat the persisted task prompt as "
                    "the authoritative stage contract, preserve unrelated changes, and verify "
                    "the result. Submit the prompt's required DoTasks MCP callback exactly once, "
                    "then finish this turn. Never claim or create another task from this worker."
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
        self._threads_to_sync[thread_id] = title
        return thread_id

    def resume_thread(self, thread_id: str, title: str = "") -> str:
        result = self.request(
            "thread/resume",
            {
                "threadId": thread_id,
                **WORKER_EXECUTION_POLICY.thread_overrides(),
            },
        )
        resumed_id = str((result.get("thread") or {}).get("id") or "")
        if not resumed_id:
            raise AppServerError("thread/resume did not return a thread id")
        if title:
            self.request("thread/name/set", {"threadId": resumed_id, "name": title})
        self._threads_to_sync[resumed_id] = title
        return resumed_id

    def _sync_completed_threads(self) -> None:
        worker_home = self.worker_codex_home
        if worker_home is None or worker_home == self.shared_codex_home:
            return
        for thread_id, thread_name in self._threads_to_sync.items():
            try:
                sync_worker_thread_to_shared_home(
                    worker_home,
                    self.shared_codex_home,
                    thread_id,
                    thread_name,
                )
            except AppServerError as exc:
                print(f"[dotasks-agent] session visibility sync failed: {exc}", file=sys.stderr)

    def start_turn(
        self,
        thread_id: str,
        prompt: str,
        image_paths: list[str] | None = None,
    ) -> str:
        self.ensure_worker_settings(thread_id)
        turn_input: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
        for value in image_paths or []:
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise AppServerError(f"Turn image input does not exist: {path}")
            turn_input.append({"type": "localImage", "path": str(path)})
        result = self.request(
            "turn/start",
            {
                "threadId": thread_id,
                "input": turn_input,
                **WORKER_EXECUTION_POLICY.turn_overrides(),
                "effort": "medium",
                "turnTrigger": "dotasks_dispatch",
            },
        )
        turn_id = str((result.get("turn") or {}).get("id") or "")
        if not turn_id:
            raise AppServerError("turn/start did not return a turn id")
        return turn_id

    def ensure_worker_settings(self, thread_id: str) -> None:
        self.request(
            "thread/settings/update",
            {
                "threadId": thread_id,
                **WORKER_EXECUTION_POLICY.turn_overrides(),
            },
        )

    def interrupt_turn(self, thread_id: str, turn_id: str) -> None:
        self.request(
            "turn/interrupt",
            {"threadId": thread_id, "turnId": turn_id},
        )

    @staticmethod
    def has_worker_permission_drift(
        notification: dict[str, Any], thread_id: str
    ) -> bool:
        method = str(notification.get("method") or "")
        if method not in {"thread/settings/updated", "thread_settings_applied"}:
            return False
        params = notification.get("params") or notification.get("payload") or {}
        target_thread = str(params.get("threadId") or params.get("thread_id") or "")
        if target_thread and target_thread != thread_id:
            return False
        settings = params.get("threadSettings") or params.get("thread_settings") or {}
        sandbox = settings.get("sandboxPolicy") or settings.get("sandbox_policy") or {}
        sandbox_type = str(sandbox.get("type") or "")
        return bool(
            sandbox_type
            and sandbox_type != WORKER_EXECUTION_POLICY.sandbox_policy_type
        )

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
