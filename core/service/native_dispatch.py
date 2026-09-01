from __future__ import annotations

import fcntl
import hashlib
from contextlib import contextmanager
from typing import Any, Iterator


class NativeDispatchMixin:
    """Persist hand-offs from DoTasks' scheduler to native Codex tasks."""

    _ACTIVE_NATIVE_DISPATCH_STATUSES = ("claimed", "pending_thread", "bound")
    _NATIVE_DISPATCH_STAGES = ("development", "code_review")

    @contextmanager
    def _native_dispatch_worker_lock(self, worker_id: str) -> Iterator[None]:
        """Serialize one controller worker across all local MCP processes."""
        lock_directory = self.data_home / "native-dispatch-locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(worker_id.encode("utf-8")).hexdigest()
        with (lock_directory / f"{digest}.lock").open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _native_dispatch_title(claim: dict[str, Any]) -> str:
        run = claim.get("run") or {}
        entity = claim.get("task") or claim.get("requirement") or {}
        role = str(run.get("run_type") or "task")
        role_label = {
            "execution": "开发",
            "rework": "返工",
            "bugfix": "Bug 修复",
            "code_review": "Code Review",
            "requirement_decomposition": "需求拆解",
        }.get(role, "任务")
        entity_id = str(entity.get("id") or "UNKNOWN")
        return f"[DoTaks] {entity_id} {role_label}"

    @staticmethod
    def _native_dispatch_prompt(claim: dict[str, Any]) -> str:
        run = claim.get("run") or {}
        if claim.get("kind") == "requirement_decomposition":
            requirement = claim.get("requirement") or {}
            return (
                "$dotasks-lifecycle\n\n"
                "这是 DoTasks 已领取的需求拆解运行，不是新任务 intake。不要编辑代码，也不要创建新的需求。\n"
                f"需求 ID：{requirement.get('id')}\n"
                f"拆解运行 ID：{run.get('id')}\n"
                "调用 get_requirement 获取完整内容，将需求拆成可独立执行、具备明确依赖且状态为 ready 的子任务。"
                "完成后调用 submit_requirement_decomposition；失败时调用 report_requirement_decomposition_failed。"
            )
        prompt = str(claim.get("dispatch_prompt") or "").strip()
        if "$dotasks-lifecycle" not in prompt:
            prompt = "$dotasks-lifecycle\n\n" + prompt
        return prompt

    def _decode_native_dispatch(self, row: Any) -> dict[str, Any]:
        item = dict(row)
        item["resume_required"] = bool(item.get("resume_thread_id"))
        item["can_create_fallback"] = item["status"] in {"claimed", "pending_thread"}
        return item

    def _refresh_native_dispatch(self, row: Any) -> dict[str, Any]:
        item = self._decode_native_dispatch(row)
        if item["status"] not in self._ACTIVE_NATIVE_DISPATCH_STATUSES:
            return item
        terminal_status = ""
        if item["entity_type"] == "task":
            with self.db.connection() as connection:
                run = connection.execute(
                    "SELECT status FROM task_runs WHERE id=?", (item["run_id"],),
                ).fetchone()
            if run and run["status"] not in {"awaiting_thread", "running"}:
                terminal_status = "completed" if run["status"] in {"completed", "waiting_review"} else "failed"
        else:
            with self.db.connection() as connection:
                run = connection.execute(
                    "SELECT status FROM requirement_decomposition_runs WHERE id=?",
                    (item["run_id"],),
                ).fetchone()
            if run and run["status"] != "running":
                terminal_status = "completed" if run["status"] == "completed" else "failed"
        if terminal_status:
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE native_dispatches SET status=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (terminal_status, item["run_id"]),
                )
            item["status"] = terminal_status
        return item

    def get_native_dispatch(self, run_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM native_dispatches WHERE run_id=?", (run_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"Native dispatch not found: {run_id}")
        return self._refresh_native_dispatch(row)

    def claim_next_native_dispatch(
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800,
        stage: str = "",
    ) -> dict[str, Any] | None:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        stage = str(stage or "").strip()
        if stage not in self._NATIVE_DISPATCH_STAGES:
            raise ValueError(
                "stage must be development or code_review"
            )
        lane_worker_id = f"{worker_id}:{stage}"
        with self._native_dispatch_worker_lock(lane_worker_id):
            return self._claim_next_native_dispatch_locked(
                lane_worker_id, project, lease_seconds, stage,
            )

    def claim_native_dispatch_batch(
        self, worker_id: str, project: str | None = None,
        lease_seconds: int = 1800, stage: str = "",
    ) -> dict[str, Any]:
        """Recover or fill every configured native dispatch slot for one stage."""
        stage = str(stage or "").strip()
        if stage not in self._NATIVE_DISPATCH_STAGES:
            raise ValueError("stage must be development or code_review")
        capacity = (
            self.max_parallel_development()
            if stage == "development" and self.parallel_development_enabled()
            else 1
        )
        dispatches: list[dict[str, Any]] = []
        for slot in range(1, capacity + 1):
            dispatch = self.claim_next_native_dispatch(
                f"{worker_id}:slot-{slot}", project, lease_seconds, stage,
            )
            if dispatch:
                dispatches.append(dispatch)
        return {
            "stage": stage,
            "capacity": capacity,
            "dispatches": dispatches,
        }

    def _claim_next_native_dispatch_locked(
        self, worker_id: str, project: str | None, lease_seconds: int,
        stage: str = "",
    ) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            active = connection.execute(
                """SELECT * FROM native_dispatches
                   WHERE worker_id=? AND status IN ('claimed','pending_thread','bound')
                   ORDER BY created_at LIMIT 1""",
                (worker_id,),
            ).fetchone()
        if active:
            refreshed = self._refresh_native_dispatch(active)
            if refreshed["status"] in self._ACTIVE_NATIVE_DISPATCH_STATUSES:
                return refreshed

        while True:
            if stage == "development":
                claim = self.claim_next_task(worker_id, project, lease_seconds)
            elif stage == "code_review":
                claim = self.claim_next_code_review_task(
                    worker_id, project, lease_seconds
                )
            if not claim:
                return None
            run = claim.get("run") or {}
            entity_type = "requirement" if claim.get("kind") == "requirement_decomposition" else "task"
            entity = claim.get(entity_type) or {}
            title = self._native_dispatch_title(claim)
            prompt = self._native_dispatch_prompt(claim)
            with self.db.transaction() as connection:
                connection.execute(
                    """INSERT INTO native_dispatches(
                           run_id, entity_type, entity_id, role, worker_id, project_path,
                           dispatch_title, dispatch_prompt, resume_thread_id,
                           execution_environment, parallel_fallback_reason,
                           base_revision, base_ref
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        run["id"], entity_type, entity["id"], run.get("run_type") or "",
                        worker_id, str(entity.get("project") or ""), title, prompt,
                        str(claim.get("resume_thread_id") or ""),
                        str(run.get("execution_environment") or "local"),
                        str((run.get("context_snapshot") or {}).get("parallel_fallback_reason") or ""),
                        str(run.get("base_revision") or ""),
                        str(run.get("base_ref") or ""),
                    ),
                )
            return self.get_native_dispatch(run["id"])

    def mark_native_dispatch_pending(
        self, run_id: str, client_thread_id: str, host_id: str = "",
        codex_project_id: str = "",
    ) -> dict[str, Any]:
        client_thread_id = str(client_thread_id or "").strip()
        if not client_thread_id:
            raise ValueError("client_thread_id is required")
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE native_dispatches SET status='pending_thread', client_thread_id=?,
                   host_id=?, codex_project_id=?, updated_at=CURRENT_TIMESTAMP
                   WHERE run_id=? AND status IN ('claimed','pending_thread')""",
                (client_thread_id, str(host_id or ""), str(codex_project_id or ""), run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Native dispatch cannot be marked pending")
        return self.get_native_dispatch(run_id)

    def bind_native_dispatch(
        self, run_id: str, thread_id: str, host_id: str = "",
        codex_project_id: str = "", resume_fallback_reason: str = "",
    ) -> dict[str, Any]:
        dispatch = self.get_native_dispatch(run_id)
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            raise ValueError("thread_id is required")
        if dispatch["status"] not in {"claimed", "pending_thread", "bound"}:
            raise ValueError("Native dispatch is no longer active")
        if dispatch["thread_id"] and dispatch["thread_id"] != thread_id:
            raise ValueError("Native dispatch is already bound to a different thread")
        fallback_reason = str(resume_fallback_reason or "").strip()
        if dispatch["entity_type"] == "task":
            self.bind_conversation(
                dispatch["entity_id"], dispatch["role"], thread_id, run_id,
                dispatch["dispatch_title"],
            )
            if fallback_reason and dispatch["role"] in {"execution", "rework", "bugfix"}:
                with self.db.transaction() as connection:
                    connection.execute(
                        "UPDATE tasks SET codex_thread_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (thread_id, dispatch["entity_id"]),
                    )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE native_dispatches SET status='bound', thread_id=?, host_id=?,
                   codex_project_id=?, resume_fallback_reason=?, updated_at=CURRENT_TIMESTAMP
                   WHERE run_id=?""",
                (thread_id, str(host_id or ""), str(codex_project_id or ""), fallback_reason, run_id),
            )
            self._event(
                connection, dispatch["entity_type"], dispatch["entity_id"],
                "native_thread_bound",
                {"run_id": run_id, "thread_id": thread_id, "role": dispatch["role"], "resume_fallback_reason": fallback_reason},
            )
        return self.get_native_dispatch(run_id)

    def renew_native_dispatch(self, run_id: str, lease_seconds: int = 1800) -> dict[str, Any]:
        dispatch = self.get_native_dispatch(run_id)
        if dispatch["entity_type"] == "task":
            with self.db.connection() as connection:
                run = connection.execute(
                    "SELECT lease_token FROM task_runs WHERE id=?", (run_id,),
                ).fetchone()
            if not run:
                raise KeyError(f"Run not found: {run_id}")
            self.renew_run_lease(run_id, run["lease_token"], lease_seconds)
        else:
            self.claim_next_task(
                "", action="renew_decomposition", requirement_id=dispatch["entity_id"],
                decomposition_run_id=run_id, lease_seconds=lease_seconds,
            )
        return self.get_native_dispatch(run_id)

    def fail_native_dispatch(self, run_id: str, reason: str) -> dict[str, Any]:
        dispatch = self.get_native_dispatch(run_id)
        reason = str(reason or "原生 Codex 任务派发失败").strip()
        if dispatch["status"] in {"completed", "failed"}:
            return dispatch
        if dispatch["entity_type"] == "task":
            self.interrupt_unsubmitted_run(run_id, reason)
        else:
            self.claim_next_task(
                "native-controller", action="fail_decomposition",
                requirement_id=dispatch["entity_id"], decomposition_run_id=run_id,
                error=reason,
            )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE native_dispatches SET status='failed', error=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (reason, run_id),
            )
        return self.get_native_dispatch(run_id)
