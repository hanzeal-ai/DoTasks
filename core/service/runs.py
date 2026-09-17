from __future__ import annotations

import math
import os
from typing import Any

from .domain import (
    ACTIVE_RUN_STATUSES,
    CONVERSATION_ROLES,
    EXECUTION_RECOVERY_LIMIT,
    REVIEW_INTERRUPT_LIMIT,
    REVIEW_RETRY_DELAYS_SECONDS,
    REVIEW_STAGE_BY_RUN_TYPE,
    RUN_JSON_FIELDS,
    decode_row,
)


def effective_token_total(
    token_used: int, usage: dict[str, int] | None = None, cached_weight: float | None = None,
) -> int:
    """Approximate budget usage while preserving raw tokens for analytics."""
    raw = max(0, int(token_used))
    normalized = {
        key: max(0, int((usage or {}).get(key) or 0))
        for key in ("input_tokens", "cached_input_tokens", "output_tokens")
    }
    if not any(normalized.values()):
        return raw
    if cached_weight is None:
        try:
            cached_weight = float(os.environ.get("DOTASKS_CACHED_TOKEN_WEIGHT", "0.1"))
        except ValueError:
            cached_weight = 0.1
    weight = max(0.0, min(float(cached_weight), 1.0))
    cached = min(normalized["cached_input_tokens"], normalized["input_tokens"])
    return (
        normalized["input_tokens"] - cached
        + math.ceil(cached * weight)
        + normalized["output_tokens"]
    )


class TaskRunMixin:
    """Run leases, recovery, Token accounting, and conversation bindings."""

    def _recover_execution_task(
        self,
        connection: Any,
        *,
        task_id: str,
        run_type: str,
        recovery_count: int,
        reason: str,
    ) -> str:
        """Resume the canonical development thread twice, then require attention."""
        attempt = int(recovery_count or 0) + 1
        if attempt <= EXECUTION_RECOVERY_LIMIT:
            target = "rework" if run_type == "rework" else "ready"
            connection.execute(
                """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                   retry_required=1, retry_run_type=?, auto_dispatch=1,
                   execution_recovery_count=?, last_recovery_reason=?,
                   last_failure_reason=?, last_failure_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (target, run_type, attempt, reason, reason, task_id),
            )
            self._event(
                connection,
                "task",
                task_id,
                "execution_auto_requeued",
                {
                    "reason": reason,
                    "run_type": run_type,
                    "attempt": attempt,
                    "attempt_limit": EXECUTION_RECOVERY_LIMIT,
                    "next_status": target,
                },
            )
            return target
        connection.execute(
            """UPDATE tasks SET status='failed', active_run_id=NULL, assigned_to=NULL,
               retry_required=1, retry_run_type=?, auto_dispatch=0,
               execution_recovery_count=?, last_recovery_reason=?,
               last_failure_reason=?, last_failure_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (run_type, attempt, reason, reason, task_id),
        )
        self._event(
            connection,
            "task",
            task_id,
            "execution_recovery_exhausted",
            {
                "reason": reason,
                "run_type": run_type,
                "attempt": attempt,
                "attempt_limit": EXECUTION_RECOVERY_LIMIT,
            },
        )
        return "failed"

    def recover_expired_runs(self) -> int:
        with self.db.transaction() as connection:
            rows = connection.execute(
                """SELECT r.id, r.task_id, r.run_type, t.status, t.review_interrupt_count,
                          t.execution_recovery_count
                   FROM task_runs r JOIN tasks t ON t.id=r.task_id
                   WHERE r.status IN ('awaiting_thread','running') AND r.lease_expires_at < CURRENT_TIMESTAMP"""
            ).fetchall()
            for row in rows:
                connection.execute("UPDATE task_runs SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row["id"],))
                connection.execute(
                    "UPDATE task_conversations SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (row["id"],),
                )
                connection.execute(
                    "UPDATE task_run_conversations SET status='expired', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (row["id"],),
                )
                review_stage = REVIEW_STAGE_BY_RUN_TYPE.get(row["run_type"])
                if review_stage:
                    interrupt_count = int(row["review_interrupt_count"] or 0) + 1
                    delay = REVIEW_RETRY_DELAYS_SECONDS[min(interrupt_count - 1, len(REVIEW_RETRY_DELAYS_SECONDS) - 1)]
                    connection.execute(
                        """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                           review_interrupt_count=?, review_retry_after=datetime('now', ?),
                           auto_dispatch=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND active_run_id=?""",
                        (review_stage, interrupt_count, f"+{delay} seconds", int(interrupt_count < REVIEW_INTERRUPT_LIMIT), row["task_id"], row["id"]),
                    )
                elif row["status"] in {"claimed", "investigating", "implementing", "rework"}:
                    self._recover_execution_task(
                        connection,
                        task_id=row["task_id"],
                        run_type=row["run_type"],
                        recovery_count=row["execution_recovery_count"],
                        reason="执行会话租约已过期",
                    )
                elif row["status"] in {"blocked", "waiting_confirmation"}:
                    connection.execute(
                        """UPDATE tasks SET active_run_id=NULL, assigned_to=NULL,
                           retry_required=1, retry_run_type=?, updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND active_run_id=?""",
                        (row["run_type"], row["task_id"], row["id"]),
                    )
                if review_stage:
                    self._event(connection, "run", row["id"], "review_interrupted", {"task_id": row["task_id"], "stage": review_stage, "reason": "评审会话租约已过期"})
                else:
                    self._event(connection, "run", row["id"], "lease_expired", {"task_id": row["task_id"]})
                self._queue_obsidian_sync(connection, "task", row["task_id"])
            recovered = len(rows)
        self.flush_integration_outbox()
        return recovered

    def interrupt_unsubmitted_run(
        self,
        run_id: str,
        reason: str,
        review_retry_immediately: bool = False,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        if run["status"] not in ACTIVE_RUN_STATUSES:
            return {"run": run, "task": self.get_task(run["task_id"]), "changed": False}
        task = self.get_task(run["task_id"])
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('awaiting_thread','running')""",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_conversations SET status='interrupted', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_run_conversations SET status='interrupted', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            review_stage = REVIEW_STAGE_BY_RUN_TYPE.get(run["run_type"])
            if review_stage:
                interrupt_count = int(task.get("review_interrupt_count") or 0) + 1
                delay = REVIEW_RETRY_DELAYS_SECONDS[min(interrupt_count - 1, len(REVIEW_RETRY_DELAYS_SECONDS) - 1)]
                connection.execute(
                    """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                       review_interrupt_count=?,
                       review_retry_after=CASE WHEN ? THEN NULL ELSE datetime('now', ?) END,
                       auto_dispatch=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND active_run_id=?""",
                    (
                        review_stage,
                        interrupt_count,
                        int(review_retry_immediately),
                        f"+{delay} seconds",
                        int(interrupt_count < REVIEW_INTERRUPT_LIMIT),
                        task["id"],
                        run_id,
                    ),
                )
                self._event(connection, "run", run_id, "review_interrupted", {"reason": reason, "stage": review_stage})
            elif task["status"] in {"blocked", "waiting_confirmation"}:
                connection.execute(
                    """UPDATE tasks SET active_run_id=NULL, assigned_to=NULL,
                       retry_required=1, retry_run_type=?, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (run["run_type"], task["id"]),
                )
                self._event(connection, "run", run_id, "execution_stopped", {"reason": reason, "task_status": task["status"]})
            else:
                next_status = self._recover_execution_task(
                    connection,
                    task_id=task["id"],
                    run_type=run["run_type"],
                    recovery_count=task.get("execution_recovery_count", 0),
                    reason=reason,
                )
                self._event(
                    connection,
                    "run",
                    run_id,
                    "execution_requeued" if next_status != "failed" else "execution_failed",
                    {"reason": reason, "next_status": next_status},
                )
            self._queue_obsidian_sync(connection, "task", task["id"])
        self.flush_integration_outbox()
        return {"run": self.get_run(run_id), "task": self.get_task(task["id"]), "changed": True}

    def pause_run_for_budget(self, run_id: str, reason: str) -> dict[str, Any]:
        """Stop an active stage at its hard task budget without scheduling a retry loop."""
        run = self.get_run(run_id)
        if run["status"] not in ACTIVE_RUN_STATUSES:
            return {"run": run, "task": self.get_task(run["task_id"]), "changed": False}
        task = self.get_task(run["task_id"])
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('awaiting_thread','running')""",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_conversations SET status='interrupted', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_run_conversations SET status='interrupted', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                """UPDATE tasks SET status='waiting_confirmation', active_run_id=NULL, assigned_to=NULL,
                   auto_dispatch=0, retry_required=1, retry_run_type=?, last_failure_reason=?,
                   last_failure_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND active_run_id=?""",
                (run["run_type"], reason, task["id"], run_id),
            )
            self._event(connection, "run", run_id, "token_budget_exceeded", {
                "task_id": task["id"], "token_used": task.get("token_used", 0),
                "effective_token_used": task.get("effective_token_used", 0),
                "token_budget": task.get("token_budget", 0), "reason": reason,
            })
            self._queue_obsidian_sync(connection, "task", task["id"])
        self.flush_integration_outbox()
        return {"run": self.get_run(run_id), "task": self.get_task(task["id"]), "changed": True}

    def renew_run_lease(self, run_id: str, lease_token: str, lease_seconds: int = 1800) -> dict[str, Any]:
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE task_runs SET lease_expires_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND lease_token=? AND status IN ('awaiting_thread','running')""",
                (f"+{lease_seconds} seconds", run_id, lease_token),
            )
            if cursor.rowcount != 1:
                raise ValueError("Run lease is invalid or no longer active")
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                """SELECT r.*, c.thread_id AS conversation_thread_id,
                          c.role AS conversation_role, c.title AS conversation_title
                   FROM task_runs r LEFT JOIN task_run_conversations c ON c.run_id=r.id
                   WHERE r.id=?""",
                (run_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"Run not found: {run_id}")
        return decode_row(row, RUN_JSON_FIELDS)

    def list_runs(self, task_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT r.*, c.thread_id AS conversation_thread_id,
                          c.role AS conversation_role, c.title AS conversation_title
                   FROM task_runs r LEFT JOIN task_run_conversations c ON c.run_id=r.id
                   WHERE r.task_id=?
                   ORDER BY r.created_at, r.id""",
                (task_id,),
            ).fetchall()
        return [decode_row(row, RUN_JSON_FIELDS) for row in rows]

    def record_run_token_usage(
        self, run_id: str, token_used: int, usage: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        """Persist monotonic run totals, including cached-input and output detail."""
        with self.db.transaction() as connection:
            self._record_run_token_usage(connection, run_id, token_used, usage)
        return self.get_run(run_id)

    def _record_run_token_usage(self, connection: Any, run_id: str,
                               token_used: int, usage: dict[str, int] | None = None) -> None:
        token_used = int(token_used)
        if token_used < 0:
            raise ValueError("token_used must be non-negative")
        normalized_usage = {
            key: max(0, int((usage or {}).get(key) or 0))
            for key in (
                "input_tokens", "cached_input_tokens", "output_tokens",
                "reasoning_output_tokens",
            )
        }
        effective_used = effective_token_total(token_used, normalized_usage)
        row = connection.execute(
            """SELECT task_id, token_used, effective_token_used, input_tokens, cached_input_tokens,
                      output_tokens, reasoning_output_tokens
               FROM task_runs WHERE id=?""", (run_id,),
        ).fetchone()
        if not row:
            raise KeyError(f"Run not found: {run_id}")
        previous = int(row["token_used"] or 0)
        previous_effective = int(row["effective_token_used"] or 0)
        if token_used > previous:
            deltas = {
                key: max(0, normalized_usage[key] - int(row[key] or 0))
                for key in normalized_usage
            }
            connection.execute(
                """INSERT OR IGNORE INTO token_usage_events(
                       task_id, run_id, source_total, token_delta, effective_token_delta, input_delta,
                       cached_input_delta, output_delta, reasoning_output_delta
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    row["task_id"], run_id, token_used, token_used - previous,
                    max(0, effective_used - previous_effective),
                    deltas["input_tokens"], deltas["cached_input_tokens"],
                    deltas["output_tokens"], deltas["reasoning_output_tokens"],
                ),
            )
        if token_used > previous or any(normalized_usage[key] > int(row[key] or 0) for key in normalized_usage):
            connection.execute(
                """UPDATE task_runs SET token_used=MAX(token_used, ?),
                       effective_token_used=MAX(effective_token_used, ?),
                       input_tokens=MAX(input_tokens, ?),
                       cached_input_tokens=MAX(cached_input_tokens, ?),
                       output_tokens=MAX(output_tokens, ?),
                       reasoning_output_tokens=MAX(reasoning_output_tokens, ?),
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    token_used, effective_used, normalized_usage["input_tokens"],
                    normalized_usage["cached_input_tokens"], normalized_usage["output_tokens"],
                    normalized_usage["reasoning_output_tokens"], run_id,
                ),
            )
            connection.execute(
                """UPDATE tasks SET token_used=(
                       SELECT COALESCE(SUM(r.token_used), 0) FROM task_runs r WHERE r.task_id=tasks.id
                   ), effective_token_used=(
                       SELECT COALESCE(SUM(r.effective_token_used), 0) FROM task_runs r WHERE r.task_id=tasks.id
                   ), updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (row["task_id"],),
            )
            self._event(connection, "run", run_id, "token_usage_updated", {
                "task_id": row["task_id"], "token_used": token_used,
                "effective_token_used": effective_used, **normalized_usage,
            })

    def _bind_conversation_in_connection(
        self, connection: Any, task_id: str, role: str, thread_id: str,
        run_id: str | None = None, title: str = "",
    ) -> None:
        if role not in CONVERSATION_ROLES:
            raise ValueError(f"Invalid conversation role: {role}")
        thread_id = str(thread_id or "").strip()
        if not thread_id:
            raise ValueError("thread_id is required")
        if role in {"execution", "rework", "bugfix", "code_review"} and not run_id:
            raise ValueError(f"run_id is required for role {role}")
        if role == "source" and run_id:
            raise ValueError(f"role {role} cannot be bound to a task run")
        task_row = connection.execute(
            "SELECT * FROM tasks WHERE id=?", (task_id,),
        ).fetchone()
        if not task_row:
            raise KeyError(f"Task not found: {task_id}")
        task = decode_row(task_row)
        if role == "code_review" and task.get("codex_thread_id") and thread_id == task.get("codex_thread_id"):
            raise ValueError("Review must use a conversation independent from the execution thread")
        if run_id:
            run_row = connection.execute(
                "SELECT * FROM task_runs WHERE id=?", (run_id,),
            ).fetchone()
            if not run_row:
                raise KeyError(f"Run not found: {run_id}")
            run = decode_row(run_row, RUN_JSON_FIELDS)
            if run["task_id"] != task_id:
                raise ValueError("Run does not belong to task")
            expected_role = run["run_type"]
            if role != expected_role:
                raise ValueError(f"Run type {run['run_type']} must use conversation role {expected_role}")
            if run["status"] not in ACTIVE_RUN_STATUSES:
                raise ValueError("Only an active run can be bound to a conversation")
            expected_task_status = "code_review" if role == "code_review" else "implementing"
            allowed_task_statuses = {expected_task_status}
            if role in {"execution", "rework", "bugfix"}:
                allowed_task_statuses.add("claimed")
            if task["status"] not in allowed_task_statuses:
                raise ValueError(f"Task must be {expected_task_status} before binding role {role}")
        existing = connection.execute(
            "SELECT task_id, role, thread_id FROM task_run_conversations WHERE run_id=?",
            (run_id,),
        ).fetchone() if run_id else None
        if existing and (existing["task_id"] != task_id or existing["thread_id"] != thread_id):
            raise ValueError("Run is already bound to a different conversation")
        canonical = connection.execute(
            "SELECT id FROM task_conversations WHERE task_id=? AND thread_id=? AND role!='source' ORDER BY created_at LIMIT 1",
            (task_id, thread_id),
        ).fetchone()
        if canonical:
            connection.execute(
                """UPDATE task_conversations SET run_id=?, role=?, title=?, status='active',
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (run_id, role, title, canonical["id"]),
            )
        else:
            connection.execute(
                "INSERT INTO task_conversations(task_id, run_id, role, thread_id, title) VALUES(?, ?, ?, ?, ?)",
                (task_id, run_id, role, thread_id, title),
            )
        if run_id:
            connection.execute(
                """INSERT INTO task_run_conversations(run_id, task_id, role, thread_id, title)
                   VALUES(?, ?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET role=excluded.role, thread_id=excluded.thread_id, title=excluded.title,
                   status='active', updated_at=CURRENT_TIMESTAMP""",
                (run_id, task_id, role, thread_id, title),
            )
            connection.execute(
                """UPDATE task_runs SET status='running', started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='awaiting_thread'""",
                (run_id,),
            )
        if role in {"execution", "rework", "bugfix"}:
            cursor = connection.execute(
                """UPDATE tasks SET status='implementing',
                   codex_thread_id=COALESCE(codex_thread_id, ?),
                   updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND active_run_id=? AND status IN ('claimed','implementing')""",
                (thread_id, task_id, run_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task changed before the native execution thread was bound")
        self._event(connection, "task", task_id, "conversation_bound", {"role": role, "thread_id": thread_id, "run_id": run_id})

    def bind_conversation(
        self, task_id: str, role: str, thread_id: str,
        run_id: str | None = None, title: str = "",
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            self._bind_conversation_in_connection(
                connection, task_id, role, thread_id, run_id, title,
            )
        return self.task_details(task_id)

    def update_conversation_summary(self, task_id: str, thread_id: str, summary: str, status: str = "completed") -> dict[str, Any]:
        with self.db.transaction() as connection:
            cursor = connection.execute(
                "UPDATE task_conversations SET summary=?, status=?, updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND thread_id=?",
                (summary.strip(), status, task_id, thread_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(f"Conversation not found for task {task_id}: {thread_id}")
            connection.execute(
                """UPDATE task_run_conversations SET summary=?, status=?, updated_at=CURRENT_TIMESTAMP
                   WHERE run_id=(SELECT run_id FROM task_run_conversations
                                 WHERE task_id=? AND thread_id=? ORDER BY created_at DESC LIMIT 1)""",
                (summary.strip(), status, task_id, thread_id),
            )
            self._queue_obsidian_sync(connection, "task", task_id)
        self.flush_integration_outbox()
        return self.task_details(task_id)

    def list_conversations(self, task_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM task_conversations WHERE task_id=? ORDER BY created_at", (task_id,),
            ).fetchall()
            mappings = connection.execute(
                """SELECT run_id, role, thread_id, title, summary, status, created_at, updated_at
                   FROM task_run_conversations WHERE task_id=? ORDER BY created_at""",
                (task_id,),
            ).fetchall()
        result = [dict(row) for row in rows]
        by_key = {(item["thread_id"], item["role"]): item for item in result}
        for row in mappings:
            item = dict(row)
            parent = by_key.get((item["thread_id"], item["role"]))
            if parent is not None:
                parent.setdefault("run_ids", []).append(item["run_id"])
                parent["status"] = item["status"]
                parent["title"] = item["title"] or parent.get("title", "")
                if item["summary"]:
                    parent["summary"] = item["summary"]
            else:
                item["run_ids"] = [item["run_id"]]
                result.append(item)
        return result
