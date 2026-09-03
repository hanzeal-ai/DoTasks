from __future__ import annotations

import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from core.service import TaskboardService

from .app_server import AppServerError, CodexAppServerClient


WORKER_ID = "dotasks-local-agent"
LEASE_SECONDS = 3600
LEASE_RENEW_SECONDS = 1200


class LocalCodexExecutor:
    """Consume durable schedule wakeups and run Codex without a polling timer."""

    def __init__(
        self,
        data_home: str | Path,
        runtime_home: str | Path | None = None,
        client_factory: Callable[..., CodexAppServerClient] = CodexAppServerClient,
        service: Any | None = None,
    ):
        self.data_home = Path(data_home).expanduser().resolve()
        self.runtime_home = (
            Path(runtime_home).expanduser().resolve()
            if runtime_home
            else Path(__file__).resolve().parents[1]
        )
        self.service = service or TaskboardService(self.data_home)
        self.client_factory = client_factory
        self._wake_event = threading.Event()
        self._stop_event = threading.Event()
        self._scheduler_thread: threading.Thread | None = None
        self._active_lock = threading.Lock()
        self._active_runs: set[str] = set()
        self._workers: set[threading.Thread] = set()
        self._clients: set[CodexAppServerClient] = set()

    @property
    def active_runs(self) -> set[str]:
        with self._active_lock:
            return set(self._active_runs)

    def start(self) -> None:
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._stop_event.clear()
        self._scheduler_thread = threading.Thread(
            target=self._event_loop,
            name="dotasks-local-scheduler",
            daemon=True,
        )
        self._scheduler_thread.start()
        self.wake()

    def stop(self) -> None:
        self._stop_event.set()
        self._wake_event.set()
        with self._active_lock:
            clients = list(self._clients)
        for client in clients:
            client.stop()
        if self._scheduler_thread and self._scheduler_thread is not threading.current_thread():
            self._scheduler_thread.join(timeout=3)

    def wake(self) -> None:
        self._wake_event.set()

    def _event_loop(self) -> None:
        while not self._stop_event.is_set():
            self._wake_event.wait()
            self._wake_event.clear()
            if self._stop_event.is_set():
                return
            try:
                self.dispatch_once()
            except Exception as exc:  # The next durable WSS/state event retries the cycle.
                print(f"[dotasks-executor] schedule failed: {exc}", file=sys.stderr)

    def dispatch_once(self) -> int:
        cycle = self.service.claim_schedule_cycle(
            WORKER_ID, lease_seconds=LEASE_SECONDS, force=False
        )
        if cycle.get("status") != "claimed":
            return 0
        try:
            dispatches = [
                *(cycle.get("code_review", {}).get("dispatches") or []),
                *(cycle.get("development", {}).get("dispatches") or []),
            ]
        finally:
            self.service.complete_schedule_cycle(
                WORKER_ID, int(cycle.get("cycle_generation") or 0)
            )
        launched = 0
        for dispatch in dispatches:
            if self._launch(self._localize_dispatch_prompt(dispatch)):
                launched += 1
        return launched

    def _localize_dispatch_prompt(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        localized = dict(dispatch)
        localized["dispatch_prompt"] = TaskboardService._attach_lifecycle_skill(
            str(dispatch.get("dispatch_prompt") or ""),
            runtime_home=self.runtime_home,
        )
        return localized

    def wait_for_workers(self) -> None:
        while True:
            with self._active_lock:
                workers = list(self._workers)
            if not workers:
                return
            for worker in workers:
                worker.join()

    def _launch(self, dispatch: dict[str, Any]) -> bool:
        run_id = str(dispatch.get("run_id") or "")
        if not run_id:
            raise ValueError("Native dispatch is missing run_id")
        with self._active_lock:
            if run_id in self._active_runs:
                return False
            self._active_runs.add(run_id)
        worker = threading.Thread(
            target=self._run_worker,
            args=(dispatch,),
            name=f"dotasks-worker-{run_id}",
            daemon=True,
        )
        with self._active_lock:
            self._workers.add(worker)
        worker.start()
        return True

    def _run_worker(self, dispatch: dict[str, Any]) -> None:
        run_id = str(dispatch["run_id"])
        client: CodexAppServerClient | None = None
        try:
            client = self.client_factory(
                data_home=self.data_home,
                runtime_home=self.runtime_home,
            )
            with self._active_lock:
                self._clients.add(client)
            client.start()
            thread_id, fallback_reason = self._prepare_thread(client, dispatch)
            self.service.bind_native_dispatch(
                run_id,
                thread_id,
                host_id=socket.gethostname(),
                codex_project_id="codex-cli-app-server",
                resume_fallback_reason=fallback_reason,
                dispatch_attempt_id=str(dispatch.get("dispatch_attempt_id") or ""),
            )
            turn_id = client.start_turn(thread_id, str(dispatch["dispatch_prompt"]))
            status, error = self._wait_for_turn(client, thread_id, turn_id, run_id)
            if self._stop_event.is_set():
                return
            current = self.service.get_native_dispatch(run_id)
            if current.get("status") in {"claimed", "pending_thread", "bound"}:
                if status == "failed":
                    reason = f"Codex CLI 任务执行失败: {error or 'turn failed'}"
                elif status == "interrupted":
                    reason = "Codex CLI 任务被中断且未提交生命周期回调"
                else:
                    reason = self.service._MISSING_LIFECYCLE_CALLBACK_REASON
                self.service.fail_native_dispatch(run_id, reason)
        except Exception as exc:
            if self._stop_event.is_set():
                return
            try:
                current = self.service.get_native_dispatch(run_id)
                if current.get("status") in {"claimed", "pending_thread", "bound"}:
                    self.service.fail_native_dispatch(run_id, f"Codex CLI 派发失败: {exc}")
            except Exception as report_error:
                print(
                    f"[dotasks-executor] failed to report {run_id}: {report_error}",
                    file=sys.stderr,
                )
        finally:
            if client is not None:
                client.stop()
            with self._active_lock:
                if client is not None:
                    self._clients.discard(client)
                self._active_runs.discard(run_id)
                self._workers.discard(threading.current_thread())
            if not self._stop_event.is_set():
                self.wake()

    def _prepare_thread(
        self, client: CodexAppServerClient, dispatch: dict[str, Any]
    ) -> tuple[str, str]:
        existing_bound = str(dispatch.get("thread_id") or "")
        resume_id = existing_bound or str(dispatch.get("resume_thread_id") or "")
        if resume_id:
            try:
                return client.resume_thread(resume_id), ""
            except AppServerError as exc:
                if existing_bound:
                    raise
                replacement = client.start_thread(
                    self._execution_path(dispatch), str(dispatch["dispatch_title"])
                )
                return replacement, f"无法恢复 {resume_id}: {exc}"
        return (
            client.start_thread(
                self._execution_path(dispatch), str(dispatch["dispatch_title"])
            ),
            "",
        )

    def _execution_path(self, dispatch: dict[str, Any]) -> str:
        execution_environment = str(
            dispatch.get("execution_environment") or "local"
        )
        if execution_environment == "projectless":
            entity_id = str(dispatch.get("entity_id") or "task").strip()
            if not entity_id or any(part in entity_id for part in ("/", "\\", "..")):
                raise AppServerError("Projectless dispatch has an invalid entity id")
            workspace = self.data_home / "projectless-workspaces" / entity_id
            workspace.mkdir(parents=True, exist_ok=True)
            return str(workspace)
        project = Path(str(dispatch["project_path"])).expanduser().resolve()
        if execution_environment != "worktree":
            return str(project)
        base_ref = str(dispatch.get("base_ref") or "").strip()
        base_revision = str(dispatch.get("base_revision") or "").strip()
        run_id = str(dispatch.get("run_id") or "").strip()
        if not base_ref or not base_revision or not run_id:
            raise AppServerError("Worktree dispatch is missing base_ref, base_revision, or run_id")
        checkout = self.data_home / "execution-worktrees" / run_id
        if checkout.is_dir():
            probe = subprocess.run(
                ["git", "-C", str(checkout), "rev-parse", "--is-inside-work-tree"],
                capture_output=True,
                text=True,
                check=False,
            )
            if probe.returncode != 0 or probe.stdout.strip() != "true":
                raise AppServerError(f"Existing execution path is not a Git worktree: {checkout}")
            return str(checkout)
        checkout.parent.mkdir(parents=True, exist_ok=True)
        created = subprocess.run(
            ["git", "-C", str(project), "worktree", "add", "--detach", str(checkout), base_ref],
            capture_output=True,
            text=True,
            check=False,
        )
        if created.returncode != 0:
            detail = (created.stderr or created.stdout).strip()
            raise AppServerError(f"Cannot create execution worktree: {detail}")
        actual = subprocess.run(
            ["git", "-C", str(checkout), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if actual.returncode != 0 or actual.stdout.strip() != base_revision:
            raise AppServerError("Execution worktree does not match the pinned base revision")
        return str(checkout)

    def _wait_for_turn(
        self,
        client: CodexAppServerClient,
        thread_id: str,
        turn_id: str,
        run_id: str,
    ) -> tuple[str, str]:
        renewed_at = time.monotonic()
        while not self._stop_event.is_set():
            notification = client.wait_notification(timeout=30)
            if notification is not None and notification.get("method") == "turn/completed":
                params = notification.get("params") or {}
                turn = params.get("turn") or {}
                if str(params.get("threadId") or "") != thread_id:
                    continue
                if str(turn.get("id") or "") != turn_id:
                    continue
                error = turn.get("error") or {}
                error_message = str(error.get("message") or error or "")
                return str(turn.get("status") or "completed"), error_message
            if not client.connected:
                raise AppServerError(client.last_error or "Codex App Server exited")
            if time.monotonic() - renewed_at >= LEASE_RENEW_SECONDS:
                self.service.renew_native_dispatch(run_id, LEASE_SECONDS)
                renewed_at = time.monotonic()
        return "interrupted", "Local Agent stopped"
