from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
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
MAX_PERMISSION_RECOVERY_ATTEMPTS = 2
PERMISSION_DRIFT_STATUS = "worker_permission_drift"
PERMISSION_RECOVERY_FAILED = "worker_permission_recovery_failed"
PERMISSION_RECOVERY_PROMPT = (
    "DoTasks 已恢复此任务的执行权限。继续上一回合尚未完成的工作，"
    "保留已有修改和上下文，完成验证后按原任务要求提交生命周期回调。"
)


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
            run_id = str(dispatch.get("run_id") or "")
            with self._active_lock:
                if run_id and run_id in self._active_runs:
                    continue
            localized = self._localize_dispatch_prompt(dispatch)
            if self._launch(localized):
                launched += 1
        return launched

    def _localize_dispatch_prompt(self, dispatch: dict[str, Any]) -> dict[str, Any]:
        localized = dict(dispatch)
        prompt = TaskboardService._prepare_direct_dispatch_prompt(
            str(dispatch.get("dispatch_prompt") or ""),
            runtime_home=self.runtime_home,
        )
        if (
            str(localized.get("execution_environment") or "local") != "projectless"
            and str(localized.get("entity_id") or "").strip()
        ):
            entity_key = (
                "requirement"
                if str(localized.get("role") or "") == "requirement_decomposition"
                else "task"
            )
            localized["dispatch_title"] = TaskboardService._native_dispatch_title(
                {
                    "run": {"run_type": str(localized.get("role") or "")},
                    entity_key: {"id": str(localized["entity_id"])},
                }
            )
        local_paths: list[str] = []
        input_image_paths: list[str] = []
        for marker in (
            "RUN_CONTEXT_JSON=",
            "REQUIREMENT_VISUAL_REFERENCES_JSON=",
        ):
            prompt, paths, image_paths = self._localize_prompt_visuals(
                prompt, marker, str(dispatch.get("run_id") or "")
            )
            local_paths.extend(paths)
            input_image_paths.extend(image_paths)
        localized["dispatch_prompt"] = prompt
        localized["local_visual_paths"] = list(dict.fromkeys(local_paths))
        localized["input_image_paths"] = list(dict.fromkeys(input_image_paths))
        return localized

    def _localize_prompt_visuals(
        self, prompt: str, marker: str, run_id: str,
    ) -> tuple[str, list[str], list[str]]:
        offset = prompt.find(marker)
        if offset < 0:
            return prompt, [], []
        start = offset + len(marker)
        try:
            payload, length = json.JSONDecoder().raw_decode(prompt[start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"Dispatch prompt contains invalid {marker[:-1]}") from exc
        references = (
            payload.get("visual_references") or []
            if isinstance(payload, dict)
            else payload
        )
        if not isinstance(references, list) or not references:
            return prompt, [], []
        localized_references: list[dict[str, Any]] = []
        local_paths: list[str] = []
        input_image_paths: list[str] = []
        for item in references:
            if not isinstance(item, dict):
                raise ValueError("Dispatch visual reference must be an object")
            reference = dict(item)
            existing = Path(str(reference.get("path") or "")).expanduser().resolve()
            if existing.is_file():
                expected = str(reference.get("sha256") or "").strip()
                if not expected or hashlib.sha256(existing.read_bytes()).hexdigest() != expected:
                    raise ValueError(
                        f"Dispatch visual artifact checksum failed: {reference.get('artifact_id') or existing}"
                    )
                reference["path"] = str(existing)
                localized_references.append(reference)
                if not str(reference.get("artifact_id") or "").startswith("artifact://attachments/"):
                    input_image_paths.append(str(existing))
                continue
            artifact_id = str(reference.get("artifact_id") or "").strip()
            if not artifact_id or not hasattr(self.service, "read_visual_artifact"):
                raise ValueError(f"Dispatch visual artifact is unavailable: {artifact_id}")
            artifact = self.service.read_visual_artifact(artifact_id)
            content = base64.b64decode(
                str(artifact.get("content_base64") or ""), validate=True
            )
            digest = hashlib.sha256(content).hexdigest()
            expected_digests = {
                str(value).strip()
                for value in (artifact.get("sha256"), reference.get("sha256"))
                if str(value or "").strip()
            }
            if not expected_digests or expected_digests != {digest}:
                raise ValueError(f"Dispatch visual artifact checksum failed: {artifact_id}")
            suffix = {
                "image/png": ".png",
                "image/jpeg": ".jpg",
                "image/gif": ".gif",
                "image/webp": ".webp",
            }.get(str(artifact.get("content_type") or reference.get("content_type") or ""))
            if artifact_id.startswith("artifact://attachments/"):
                suffix = Path(artifact_id).suffix
                if not suffix or len(suffix) > 11 or not suffix[1:].isalnum():
                    suffix = ".bin"
            if not suffix:
                raise ValueError(f"Dispatch visual artifact type is unsupported: {artifact_id}")
            if not run_id or any(part in run_id for part in ("/", "\\", "..")):
                raise ValueError("Dispatch run id is invalid for visual materialization")
            destination = self.data_home / "artifacts" / "dispatch-visuals" / run_id
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{digest}{suffix}"
            if not target.exists():
                temporary = target.with_suffix(target.suffix + ".tmp")
                temporary.write_bytes(content)
                os.replace(temporary, target)
            reference.update({
                "path": str(target),
                "sha256": digest,
                "content_type": str(artifact.get("content_type") or ""),
                "size": len(content),
            })
            localized_references.append(reference)
            local_paths.append(str(target))
            if not artifact_id.startswith("artifact://attachments/"):
                input_image_paths.append(str(target))
        if isinstance(payload, dict):
            payload = dict(payload)
            payload["visual_references"] = localized_references
        else:
            payload = localized_references
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return (
            prompt[:start] + encoded + prompt[start + length:],
            local_paths,
            input_image_paths,
        )

    def _cleanup_dispatch_visuals(self, dispatch: dict[str, Any]) -> None:
        paths = [Path(path) for path in dispatch.get("local_visual_paths") or []]
        roots = {path.parent for path in paths}
        for path in paths:
            path.unlink(missing_ok=True)
        managed_root = (self.data_home / "artifacts" / "dispatch-visuals").resolve()
        for root in roots:
            resolved = root.resolve()
            if managed_root in resolved.parents:
                shutil.rmtree(resolved, ignore_errors=True)

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
            turn_id = client.start_turn(
                thread_id, str(dispatch["dispatch_prompt"]),
                image_paths=list(dispatch.get("input_image_paths") or []),
            )
            status, error = self._wait_for_turn(client, thread_id, turn_id, run_id)
            recovery_attempt = 0
            while status == PERMISSION_DRIFT_STATUS and not self._stop_event.is_set():
                self._log_worker_event(
                    "permission_drift_detected",
                    run_id=run_id,
                    thread_id=thread_id,
                    turn_id=turn_id,
                    attempt=recovery_attempt + 1,
                )
                try:
                    client.interrupt_turn(thread_id, turn_id)
                except AppServerError as exc:
                    status = "failed"
                    error = f"[{PERMISSION_RECOVERY_FAILED}] 无法中断受限回合：{exc}"
                    self._log_worker_event(
                        "permission_recovery_failed",
                        run_id=run_id,
                        thread_id=thread_id,
                        attempt=recovery_attempt,
                        error=error,
                    )
                    break
                current = self.service.get_native_dispatch(run_id)
                if current.get("status") not in {"claimed", "pending_thread", "bound"}:
                    status, error = "completed", ""
                    break
                if recovery_attempt >= MAX_PERMISSION_RECOVERY_ATTEMPTS:
                    status = "failed"
                    error = (
                        f"[{PERMISSION_RECOVERY_FAILED}] 执行权限连续漂移，"
                        f"已停止自动恢复（{MAX_PERMISSION_RECOVERY_ATTEMPTS} 次）"
                    )
                    self._log_worker_event(
                        "permission_recovery_failed",
                        run_id=run_id,
                        thread_id=thread_id,
                        attempt=recovery_attempt,
                        error=error,
                    )
                    break
                recovery_attempt += 1
                self._log_worker_event(
                    "permission_recovery_started",
                    run_id=run_id,
                    thread_id=thread_id,
                    attempt=recovery_attempt,
                )
                try:
                    turn_id = client.start_turn(
                        thread_id,
                        PERMISSION_RECOVERY_PROMPT,
                        image_paths=list(dispatch.get("input_image_paths") or []),
                    )
                except AppServerError as exc:
                    status = "failed"
                    error = f"[{PERMISSION_RECOVERY_FAILED}] 无法启动恢复回合：{exc}"
                    self._log_worker_event(
                        "permission_recovery_failed",
                        run_id=run_id,
                        thread_id=thread_id,
                        attempt=recovery_attempt,
                        error=error,
                    )
                    break
                self._log_worker_event(
                    "permission_recovery_succeeded",
                    run_id=run_id,
                    thread_id=thread_id,
                    attempt=recovery_attempt,
                )
                status, error = self._wait_for_turn(
                    client, thread_id, turn_id, run_id
                )
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
            self._cleanup_dispatch_visuals(dispatch)
            if client is not None:
                client.stop()
            with self._active_lock:
                if client is not None:
                    self._clients.discard(client)
                self._active_runs.discard(run_id)
                self._workers.discard(threading.current_thread())
            if not self._stop_event.is_set():
                self.wake()

    @staticmethod
    def _log_worker_event(event: str, **payload: Any) -> None:
        print(
            json.dumps(
                {"component": "dotasks-executor", "event": event, **payload},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )

    def _prepare_thread(
        self, client: CodexAppServerClient, dispatch: dict[str, Any]
    ) -> tuple[str, str]:
        existing_bound = str(dispatch.get("thread_id") or "")
        resume_id = existing_bound or str(dispatch.get("resume_thread_id") or "")
        if resume_id:
            try:
                return client.resume_thread(
                    resume_id, str(dispatch["dispatch_title"])
                ), ""
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
            if (
                notification is not None
                and CodexAppServerClient.has_worker_permission_drift(
                    notification, thread_id
                )
            ):
                return PERMISSION_DRIFT_STATUS, "worker sandbox policy changed"
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
