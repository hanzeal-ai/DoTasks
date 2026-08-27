from __future__ import annotations

import os
import fcntl
import json
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

from taskboard.app_server import AppServerError, CodexAppServerClient, task_thread_start_params, task_turn_start_params
from .service import TaskboardService
from taskboard.workspace import thread_stage_name


class TaskDispatcher:
    def __init__(
        self,
        service: TaskboardService,
        client: CodexAppServerClient | None = None,
        interval_seconds: float | None = None,
    ):
        self.service = service
        self._injected_client = client
        self._active_clients: dict[str, CodexAppServerClient] = {}
        self._client_started_at: dict[str, float] = {}
        self._active_runs: dict[str, dict[str, Any]] = {}
        self._dispatch_lock = threading.RLock()
        self.interval_seconds = interval_seconds or float(os.environ.get("CODEX_TASKBOARD_DISPATCH_INTERVAL", "2"))
        self.turn_idle_timeout_seconds = max(
            300.0, float(os.environ.get("CODEX_TASKBOARD_TURN_IDLE_TIMEOUT", "1800")),
        )
        self.turn_close_grace_seconds = max(
            30.0, float(os.environ.get("CODEX_TASKBOARD_TURN_CLOSE_GRACE", "120")),
        )
        self.worker_id = "codex-taskboard-dispatcher"
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self.recovered_orphaned_runs = 0
        self._lock_handle: Any | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        lock_path = self.service.data_home / "dispatcher.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            self.last_error = "另一个 Codex Taskboard 调度器实例正在运行"
            return
        self._lock_handle = handle
        self._stop.clear()
        try:
            if hasattr(self.service, "recover_orphaned_dispatcher_runs"):
                self.recovered_orphaned_runs = self.service.recover_orphaned_dispatcher_runs(self.worker_id)
        except Exception as exc:
            self.last_error = f"调度器恢复失败：{exc}"
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            self._lock_handle = None
            return
        self._thread = threading.Thread(target=self._run, name="codex-taskboard-dispatcher", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        active_clients = list(self._active_clients.items())
        for thread_id, client in active_clients:
            metadata = self._active_runs.get(thread_id, {})
            run_id = str(metadata.get("run_id") or "")
            if run_id:
                try:
                    self.service.interrupt_unsubmitted_run(run_id, "Codex Taskboard 调度器已停止")
                except Exception as exc:
                    self.last_error = str(exc)
            client.stop()
        self._active_clients.clear()
        self._client_started_at.clear()
        self._active_runs.clear()
        if self._injected_client and all(
            self._injected_client is not client for _, client in active_clients
        ):
            self._injected_client.stop()
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
                self._lock_handle = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.service.dispatcher_enabled(),
            "running": bool(self._thread and self._thread.is_alive()),
            "app_server_connected": any(client.connected for client in self._active_clients.values()),
            "active_app_servers": len(self._active_clients),
            "recovered_orphaned_runs": self.recovered_orphaned_runs,
            "last_error": self.last_error,
        }

    def _new_client(
        self, tool_profile: str = "", project: str | Path | None = None,
    ) -> CodexAppServerClient:
        if self._injected_client is not None:
            return self._injected_client
        return CodexAppServerClient(
            codex_home=self.service.codex_home,
            taskboard_runtime_home=self.service.package_home,
            taskboard_data_home=self.service.data_home,
            tool_profile=tool_profile,
            project=project,
        )

    @staticmethod
    def _fully_automated_acceptance(task: dict[str, Any] | None) -> bool:
        task = task or {}
        criteria = [str(item) for item in task.get("acceptance_criteria") or []]
        plans = list(task.get("acceptance_plan") or [])
        plan_criteria = [
            str(item.get("criterion") or "") for item in plans if isinstance(item, dict)
        ]
        return bool(criteria) and len(plans) == len(criteria) and Counter(plan_criteria) == Counter(criteria) and all(
            isinstance(item, dict)
            and bool(item.get("required", True))
            and str(item.get("check_type") or ("automated" if item.get("command") else "static_review")) == "automated"
            and bool(str(item.get("command") or "").strip())
            for item in plans
        )

    @staticmethod
    def _model_assisted_acceptance(task: dict[str, Any] | None) -> bool:
        return any(
            isinstance(item, dict)
            and bool(item.get("required", True))
            and str(
                item.get("check_type")
                or ("automated" if item.get("command") else "static_review")
            )
            != "automated"
            for item in (task or {}).get("acceptance_plan") or []
        )

    def _claim_next_stage(self, project: str | None = None) -> dict[str, Any] | None:
        kwargs: dict[str, Any] = {"lease_seconds": 1800}
        if project:
            kwargs["project"] = project
        claim = (
            self.service.claim_next_acceptance_task(self.worker_id, **kwargs)
            if hasattr(self.service, "claim_next_acceptance_task")
            else None
        )
        if not claim:
            claim = (
                self.service.claim_next_code_review_task(self.worker_id, **kwargs)
                if hasattr(self.service, "claim_next_code_review_task")
                else None
            )
        if not claim:
            claim = self.service.claim_next_review_task(self.worker_id, **kwargs)
        if not claim:
            claim = self.service.claim_next_task(self.worker_id, **kwargs)
        return claim

    def _claimable_projects(self) -> list[str]:
        active = {
            str(metadata.get("project") or "")
            for metadata in self._active_runs.values()
            if str(metadata.get("project") or "")
        }
        if not active:
            return []
        if not hasattr(self.service, "list_tasks"):
            return []
        return sorted({
            str(task.get("project") or "")
            for task in self.service.list_tasks()
            if str(task.get("project") or "")
            and str(task.get("project") or "") not in active
            and str(task.get("status") or "")
            in {"ready", "rework", "review", "code_review", "acceptance"}
            and int(task.get("auto_dispatch", 1)) == 1
        })

    @classmethod
    def _tool_profile_for_run(
        cls, run_type: str, task: dict[str, Any] | None = None, resume_thread_id: str = "",
    ) -> str:
        if run_type in {"execution", "rework", "bugfix"}:
            return "execution"
        if run_type == "code_review":
            return "code_review" if cls._fully_automated_acceptance(task) else "verifier"
        if run_type == "acceptance":
            return "verifier" if resume_thread_id else "acceptance"
        return "legacy_review"

    @staticmethod
    def _terminal_turn_reason(turn: dict[str, Any], default_status: str = "") -> str:
        status = str(turn.get("status") or default_status or "").strip()
        if status not in {"completed", "interrupted", "failed"}:
            return ""
        if status == "completed":
            return "Codex 会话已结束，但没有提交交付或验收结果"
        if status == "interrupted":
            return "Codex 会话被中断，且没有提交交付或验收结果"
        error = turn.get("error")
        if isinstance(error, dict):
            detail = error.get("message") or error.get("additionalDetails") or error.get("codexErrorInfo")
        else:
            detail = error
        suffix = f"：{detail}" if detail else ""
        return f"Codex 会话执行失败{suffix}"

    def _record_token_notification(
        self, thread_id: str, metadata: dict[str, Any], message: dict[str, Any],
    ) -> None:
        if message.get("method") != "thread/tokenUsage/updated":
            return
        params = message.get("params") or {}
        event_thread_id = str(params.get("threadId") or "")
        event_turn_id = str(params.get("turnId") or "")
        if event_thread_id and event_thread_id != thread_id:
            return
        tracked_turn_id = str(metadata.get("turn_id") or "")
        if tracked_turn_id and event_turn_id and event_turn_id != tracked_turn_id:
            return
        usage = params.get("tokenUsage") or {}
        total = usage.get("total") or {}
        last = usage.get("last") or {}
        thread_total = int(total.get("totalTokens") or 0)
        last_total = int(last.get("totalTokens") or 0)
        previous_thread_total = metadata.get("thread_total_tokens")
        if previous_thread_total is None:
            increment = last_total
        else:
            increment = max(0, thread_total - int(previous_thread_total))
        metadata["thread_total_tokens"] = max(
            thread_total, int(previous_thread_total or 0),
        )
        metric_names = {
            "input_tokens": ("inputTokens", "input_tokens"),
            "cached_input_tokens": ("cachedInputTokens", "cached_input_tokens"),
            "output_tokens": ("outputTokens", "output_tokens"),
            "reasoning_output_tokens": ("reasoningOutputTokens", "reasoning_output_tokens"),
        }
        previous_usage = metadata.get("thread_total_usage")
        run_usage = metadata.setdefault("run_usage", {})
        next_thread_usage: dict[str, int] = {}
        for key, aliases in metric_names.items():
            total_value = next((int(total.get(alias) or 0) for alias in aliases if alias in total), 0)
            last_value = next((int(last.get(alias) or 0) for alias in aliases if alias in last), 0)
            prior_value = int((previous_usage or {}).get(key) or 0)
            metric_increment = last_value if previous_usage is None else max(0, total_value - prior_value)
            next_thread_usage[key] = max(total_value, prior_value)
            run_usage[key] = int(run_usage.get(key) or 0) + metric_increment
        metadata["thread_total_usage"] = next_thread_usage
        if increment <= 0:
            return
        metadata["run_token_used"] = int(metadata.get("run_token_used") or 0) + increment
        self.service.record_run_token_usage(
            str(metadata.get("run_id") or ""), int(metadata["run_token_used"]),
            {key: int(value) for key, value in run_usage.items()},
        )
        task_id = str(metadata.get("task_id") or "")
        if task_id and hasattr(self.service, "pause_run_for_budget"):
            task = self.service.get_task(task_id)
            budget = int(task.get("token_budget") or 0)
            used = int(task.get("effective_token_used") or 0)
            if budget > 0 and used >= budget:
                raw_used = int(task.get("token_used") or 0)
                metadata["budget_reason"] = (
                    f"有效 Token 预算已用尽：{used}/{budget}（原始 Token {raw_used}）"
                )

    def _reap_clients(self) -> None:
        for thread_id, client in list(self._active_clients.items()):
            metadata = self._active_runs.get(thread_id, {})
            run_id = str(metadata.get("run_id") or "")
            try:
                run = self.service.get_run(run_id)
                terminal_reason = ""
                turn_finished = False
                now = time.monotonic()
                if hasattr(client, "drain_notifications"):
                    messages = client.drain_notifications()
                    if messages:
                        metadata["last_activity_at"] = now
                    for message in messages:
                        self._record_token_notification(thread_id, metadata, message)
                        if message.get("method") in {"turn/completed", "turn/complete"}:
                            params = message.get("params") or {}
                            event_thread_id = params.get("threadId") or (params.get("thread") or {}).get("id")
                            if event_thread_id and str(event_thread_id) != thread_id:
                                continue
                            turn = params.get("turn") or {}
                            event_turn_id = str(turn.get("id") or params.get("turnId") or "")
                            tracked_turn_id = str(metadata.get("turn_id") or "")
                            if tracked_turn_id and event_turn_id and event_turn_id != tracked_turn_id:
                                continue
                            turn_finished = True
                            terminal_reason = self._terminal_turn_reason(turn, "completed")
                budget_reason = str(metadata.get("budget_reason") or "")
                if budget_reason and run["status"] in {"awaiting_thread", "running"}:
                    self.service.pause_run_for_budget(run_id, budget_reason)
                    client.stop()
                    self._active_clients.pop(thread_id, None)
                    self._client_started_at.pop(thread_id, None)
                    self._active_runs.pop(thread_id, None)
                    self.last_error = budget_reason
                    continue
                if run["status"] not in {"awaiting_thread", "running"}:
                    closed_at = float(metadata.setdefault("lifecycle_closed_at", now))
                    if turn_finished or not client.connected or now - closed_at >= self.turn_close_grace_seconds:
                        client.stop()
                        self._active_clients.pop(thread_id, None)
                        self._client_started_at.pop(thread_id, None)
                        self._active_runs.pop(thread_id, None)
                    continue
                if not terminal_reason and client.connected:
                    idle_seconds = now - float(metadata.get("last_activity_at", now))
                    if idle_seconds >= self.turn_idle_timeout_seconds:
                        terminal_reason = (
                            f"Codex 会话连续 {int(idle_seconds)} 秒没有生命周期事件，按卡死运行回收"
                        )
                if terminal_reason:
                    self.service.interrupt_unsubmitted_run(run_id, terminal_reason)
                    client.stop()
                    self._active_clients.pop(thread_id, None)
                    self._client_started_at.pop(thread_id, None)
                    self._active_runs.pop(thread_id, None)
                    continue
                if client.connected and now - float(metadata.get("last_renewed", 0)) >= 60:
                    self.service.renew_run_lease(run_id, str(metadata["lease_token"]), 1800)
                    metadata["last_renewed"] = now
                elif not client.connected and now - self._client_started_at.get(thread_id, now) >= 30:
                    self.service.interrupt_unsubmitted_run(run_id, "Codex App Server 连接中断")
                    self._active_clients.pop(thread_id, None)
                    self._client_started_at.pop(thread_id, None)
                    self._active_runs.pop(thread_id, None)
            except Exception as exc:
                self.last_error = str(exc)

    def dispatch_once(self, limit: int = 3) -> int:
        with self._dispatch_lock:
            return self._dispatch_once(limit)

    def _dispatch_once(self, limit: int = 3) -> int:
        self._reap_clients()
        if hasattr(self.service, "flush_integration_outbox"):
            self.service.flush_integration_outbox()
        if not self.service.dispatcher_enabled():
            return 0
        self._flush_batch_steers()
        dispatched = 0
        capacity = max(0, min(max(1, int(limit)), 3) - len(self._active_clients))
        for _ in range(capacity):
            # A lifecycle tool can close the DB run before the app-server turn
            # releases its thread writer. While that client is still alive,
            # only claim work from other projects.
            if self._active_runs:
                claim = None
                for candidate_project in self._claimable_projects():
                    claim = self._claim_next_stage(candidate_project)
                    if claim:
                        break
            else:
                claim = self._claim_next_stage()
            if not claim:
                break
            task = claim["task"]
            run = claim["run"]
            project = Path(str(task.get("project") or "")).expanduser()
            if not project.is_dir():
                reason = f"项目目录不存在：{project}"
                self.service.interrupt_unsubmitted_run(run["id"], reason)
                self.service.transition_task(task["id"], "blocked", reason)
                continue
            acceptance_preflight = None
            if run["run_type"] == "acceptance" and hasattr(self.service, "auto_accept_automated_task"):
                try:
                    acceptance_preflight = self.service.auto_accept_automated_task(task["id"], run["id"])
                except Exception as exc:
                    self.last_error = f"自动化验收短路失败，已回退模型验收：{exc}"
                if acceptance_preflight and acceptance_preflight.get("completed"):
                    dispatched += 1
                    continue
            if run["run_type"] in {"review", "code_review", "acceptance"}:
                role = run["run_type"]
                prompt = claim["dispatch_prompt"]
                if acceptance_preflight and acceptance_preflight.get("eligible"):
                    checks = acceptance_preflight.get("checks") or {}
                    compact_checks = [
                        {
                            "criterion": item.get("criterion"), "status": item.get("status"),
                            "exit_code": item.get("exit_code"),
                            "output": str(item.get("output") or "")[-2000:],
                        }
                        for item in checks.get("checks", [])
                    ]
                    prompt += (
                        "\n\n自动化验收预检已经执行且未全部通过，不要重复运行检查；"
                        "依据以下结果直接完成验收判定："
                        + json.dumps(compact_checks, ensure_ascii=False, separators=(",", ":"))
                    )
            else:
                role = (
                    "bugfix" if run["run_type"] == "bugfix" else
                    ("rework" if run["run_type"] == "rework" else "execution")
                )
                prompt = claim["dispatch_prompt"]
            title = thread_stage_name(role)
            client: CodexAppServerClient | None = None
            try:
                resume_thread_id = str(claim.get("resume_thread_id") or "")
                client = self._new_client(
                    self._tool_profile_for_run(run["run_type"], task, resume_thread_id),
                    project,
                )
                client.start()
                if resume_thread_id:
                    try:
                        client.request("thread/resume", {"threadId": resume_thread_id})
                        thread_id = resume_thread_id
                    except AppServerError as exc:
                        raise AppServerError(f"任务原会话仍有活动写入者，未创建新会话：{exc}") from exc
                if not resume_thread_id:
                    thread_result = client.request("thread/start", task_thread_start_params(project))
                    thread_id = str(thread_result["thread"]["id"])
                name_error = ""
                try:
                    # Naming before turn/start avoids reading a rollout while
                    # its session metadata is still being written.
                    client.request("thread/name/set", {"threadId": thread_id, "name": title})
                except AppServerError as exc:
                    # A cosmetic metadata failure must not discard a valid
                    # task thread or mark the execution itself as failed.
                    name_error = str(exc)
                high_risk = bool((task.get("review_contract") or {}).get("separate_acceptance_session", False))
                execution_profile = (run.get("context_snapshot") or {}).get("execution_profile") or {}
                low_risk = execution_profile.get("risk") == "low"
                turn_result = client.request(
                    "turn/start",
                    task_turn_start_params(
                        thread_id,
                        prompt,
                        run["run_type"],
                        high_risk,
                        low_risk,
                        self._model_assisted_acceptance(task),
                    ),
                )
                self.service.bind_conversation(task["id"], role, thread_id, run["id"], title)
                if hasattr(self.service, "bind_batch_turn"):
                    batch = (run.get("context_snapshot") or {}).get("batch") or {}
                    self.service.bind_batch_turn(
                        run["id"],
                        thread_id,
                        str((turn_result.get("turn") or {}).get("id") or ""),
                        int(batch.get("revision") or 0),
                    )
                self._active_clients[thread_id] = client
                self._client_started_at[thread_id] = time.monotonic()
                self._active_runs[thread_id] = {
                    "run_id": run["id"],
                    "task_id": task["id"],
                    "project": str(project.resolve()),
                    "lease_token": claim["lease_token"],
                    "turn_id": str((turn_result.get("turn") or {}).get("id") or ""),
                    "run_token_used": int(run.get("token_used") or 0),
                    "run_usage": {
                        "input_tokens": int(run.get("input_tokens") or 0),
                        "cached_input_tokens": int(run.get("cached_input_tokens") or 0),
                        "output_tokens": int(run.get("output_tokens") or 0),
                        "reasoning_output_tokens": int(run.get("reasoning_output_tokens") or 0),
                    },
                    "thread_total_tokens": None,
                    "thread_total_usage": None,
                    "last_renewed": time.monotonic(),
                    "last_activity_at": time.monotonic(),
                }
                dispatched += 1
                self.last_error = f"会话已创建，但命名失败：{name_error}" if name_error else ""
            except Exception as exc:
                if client is not None:
                    client.stop()
                self.last_error = str(exc)
                if run["status"] in {"awaiting_thread", "running"}:
                    self.service.interrupt_unsubmitted_run(run["id"], f"Codex 会话创建失败：{exc}")
        return dispatched

    def _flush_batch_steers(self) -> None:
        if not hasattr(self.service, "pending_batch_steers"):
            return
        for event in self.service.pending_batch_steers():
            thread_id = str(event.get("thread_id") or "")
            turn_id = str(event.get("active_turn_id") or "")
            client = self._active_clients.get(thread_id)
            if not client or not client.connected or not turn_id:
                continue
            prompt = (
                "$codex-taskboard-lifecycle\n\n"
                "执行批次在开发阶段新增了一个任务。将以下增量需求合并到当前实现；"
                "不要交付旧批次版本，最终 submit_task_delivery 必须携带新的 batch_revision。\n\n"
                "BATCH_APPEND_JSON="
                + json.dumps(event["input"], ensure_ascii=False, separators=(",", ":"))
            )
            try:
                client.request(
                    "turn/steer",
                    {
                        "threadId": thread_id,
                        "input": [{"type": "text", "text": prompt}],
                        "expectedTurnId": turn_id,
                    },
                )
                self.service.mark_batch_steer_sent(int(event["id"]))
            except AppServerError as exc:
                # A turn may finish between polling and steering. Keep the event
                # pending so a resumed development turn can include it.
                self.service.mark_batch_steer_pending(int(event["id"]), str(exc))

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.dispatch_once()
            except Exception as exc:
                self.last_error = str(exc)
            self._stop.wait(self.interval_seconds)
