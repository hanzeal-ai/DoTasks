from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..run_context import freeze_run_context, prompt_context
from .domain import (
    ACTIVE_RUN_STATUSES,
    DISPATCH_PREPARATION_LIMIT,
    DISPATCH_RETRY_DELAYS_SECONDS,
    JSON_FIELDS,
    REVIEW_INTERRUPT_LIMIT,
    REVIEW_RETRY_DELAYS_SECONDS,
    RUN_JSON_FIELDS,
    TASK_TRANSITIONS,
    _decode_row,
)


class TaskLifecycleMixin:
    """Task state transitions and atomic claims for execution and review stages."""

    def transition_task(
        self, task_id: str, status: str, reason: str = "", **updates: Any
    ) -> dict[str, Any]:
        current = self.get_task(task_id)
        self._validate_transition(task_id, current, status, updates)
        changes, closes_active_run = self._transition_changes(
            current, status, reason, updates
        )
        self._persist_transition(
            task_id, current, status, reason, changes, closes_active_run
        )
        task = self.get_task(task_id)
        self.flush_integration_outbox()
        return task

    def report_run_blocked(
        self,
        task_id: str,
        run_id: str,
        status: str,
        reason: str,
    ) -> dict[str, Any]:
        """Expose only the exceptional execution exits needed by a stage worker."""
        status = str(status or "").strip()
        reason = str(reason or "").strip()
        if status not in {"waiting_confirmation", "blocked"}:
            raise ValueError("status must be waiting_confirmation or blocked")
        if not reason:
            raise ValueError("reason is required")
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        if (
            run.get("task_id") != task_id
            or task.get("active_run_id") != run_id
            or run.get("run_type") not in {"execution", "rework", "bugfix"}
            or run.get("status") not in ACTIVE_RUN_STATUSES
        ):
            raise ValueError(
                "An active execution run belonging to the task is required"
            )
        return self.transition_task(task_id, status, reason)

    def _validate_transition(
        self,
        task_id: str,
        current: dict[str, Any],
        status: str,
        updates: dict[str, Any],
    ) -> None:
        if status not in TASK_TRANSITIONS:
            raise ValueError(f"Unknown task status: {status}")
        if "token_budget" in updates:
            token_budget = int(updates["token_budget"])
            budget_restart = (
                current["status"] == "waiting_confirmation"
                and status in {"ready", "rework"}
                and int(current.get("effective_token_used") or 0)
                >= int(current.get("token_budget") or 0)
                and token_budget > int(current.get("effective_token_used") or 0)
                and token_budget > int(current.get("token_budget") or 0)
            )
            if not budget_restart:
                raise ValueError(
                    "Token budget can only be increased when restarting a budget-exhausted task"
                )
        same_v2_stage_update = (
            current["status"] == status
            and status in {"code_review", "acceptance"}
            and set(updates) == {"auto_dispatch"}
        )
        restoring_v2_stage = (
            current["status"] == "blocked"
            and current.get("blocked_from_status") == status
            and status in {"code_review", "acceptance"}
        )
        # V2 review APIs persist their result and stage transition atomically;
        # allowing the generic transition endpoint would bypass that invariant.
        if (
            int(current.get("workflow_version") or 1) >= 2
            and status in {"code_review", "acceptance", "done", "acceptance_blocked"}
            and not restoring_v2_stage
            and not same_v2_stage_update
        ):
            raise ValueError(
                "Workflow v2 stage transitions must use their dedicated review/acceptance API"
            )
        if (
            status != current["status"]
            and status not in TASK_TRANSITIONS[current["status"]]
        ):
            raise ValueError(
                f"Invalid task transition: {current['status']} -> {status}"
            )
        if (
            current["status"] == "blocked"
            and status != current["status"]
            and status not in {"paused", "cancelled"}
        ):
            previous = current.get("blocked_from_status") or "ready"
            expected = (
                previous
                if previous in {"review", "code_review", "acceptance"}
                else (
                    "rework"
                    if previous == "rework" or current.get("retry_run_type") == "rework"
                    else "ready"
                )
            )
            if status != expected:
                raise ValueError(f"Blocked task must return to {expected}")
        if status == "review" and status != current["status"]:
            with self.db.connection() as connection:
                delivery = connection.execute(
                    """SELECT 1 FROM task_runs WHERE task_id=? AND id=(SELECT primary_run_id FROM tasks WHERE id=?)
                       AND acceptance_evidence!='[]' LIMIT 1""",
                    (task_id, task_id),
                ).fetchone()
            if current["status"] != "blocked" or not delivery:
                raise ValueError(
                    "Only a blocked submitted delivery can return to review"
                )
        if status == "ready":
            self._assert_ready_payload(
                current, {field: current[field] for field in JSON_FIELDS}
            )
        if status in {"investigating", "implementing"}:
            active = (
                self.get_run(current["active_run_id"])
                if current.get("active_run_id")
                else None
            )
            if (
                not active
                or active["status"] not in ACTIVE_RUN_STATUSES
                or active["run_type"] not in {"execution", "rework", "bugfix"}
            ):
                raise ValueError(
                    f"Task cannot enter {status} without an active execution run"
                )

    def _transition_changes(
        self,
        current: dict[str, Any],
        status: str,
        reason: str,
        updates: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        allowed_updates = {
            "codex_thread_id",
            "assigned_to",
            "token_used",
            "token_budget",
            "auto_dispatch",
        }
        changes = {
            key: value for key, value in updates.items() if key in allowed_updates
        }
        if "token_used" in changes and int(changes["token_used"]) < 0:
            raise ValueError("token_used must be non-negative")
        if "token_budget" in changes:
            changes["token_budget"] = int(changes["token_budget"])
        if "auto_dispatch" in changes:
            changes["auto_dispatch"] = int(bool(changes["auto_dispatch"]))
            if (
                current["status"] in {"review", "code_review", "acceptance"}
                and changes["auto_dispatch"]
            ):
                changes["review_interrupt_count"] = 0
                changes["review_retry_after"] = None
        changes["status"] = status
        active = (
            self.get_run(current["active_run_id"])
            if current.get("active_run_id")
            else None
        )
        if status == "paused" and status != current["status"]:
            changes["paused_from_status"] = current["status"]
            if active and active["run_type"] in {"execution", "rework"}:
                changes["retry_required"] = 1
                changes["retry_run_type"] = active["run_type"]
        if status == "blocked" and status != current["status"]:
            changes["blocked_from_status"] = current["status"]
        elif current["status"] == "blocked" and status != "blocked":
            changes["blocked_from_status"] = None
        if (
            status in {"blocked", "waiting_confirmation"}
            and status != current["status"]
        ):
            changes["last_failure_reason"] = reason or (
                "等待用户确认" if status == "waiting_confirmation" else "任务执行被阻塞"
            )
        if status == "failed":
            changes["last_failure_reason"] = reason or "任务执行意外中断"
            changes["last_failure_at"] = datetime.now(timezone.utc).isoformat()
            changes["retry_required"] = 1
            changes["dispatch_failure_count"] = 0
            changes["dispatch_retry_after"] = None
            changes["last_dispatch_error"] = ""
            if active:
                changes["retry_run_type"] = active["run_type"]
        if current["status"] in {"blocked", "waiting_confirmation"} and status in {
            "ready",
            "rework",
        }:
            retry_required = bool(current.get("retry_run_type"))
            changes["retry_required"] = int(retry_required)
            changes["retry_run_type"] = (
                ("rework" if status == "rework" else "execution")
                if retry_required
                else None
            )
        if active and status in {"ready", "rework", "blocked", "waiting_confirmation"}:
            if active["run_type"] in {"execution", "rework"}:
                changes["retry_required"] = 1
                changes["retry_run_type"] = active["run_type"]
        if status in {"done", "cancelled"}:
            changes["retry_required"] = 0
            changes["retry_run_type"] = None
        if (
            status in {"ready", "rework", "review"}
            and status != current["status"]
            and current["status"] in {"blocked", "failed", "waiting_confirmation"}
        ):
            changes["dispatch_failure_count"] = 0
            changes["dispatch_retry_after"] = None
            changes["last_dispatch_error"] = ""
            if current["status"] == "blocked" and not current.get("auto_dispatch"):
                changes["auto_dispatch"] = 1
        closes_active_run = (
            bool(current.get("active_run_id"))
            and status != current["status"]
            and status
            in {
                "ready",
                "failed",
                "paused",
                "blocked",
                "waiting_confirmation",
                "cancelled",
            }
        )
        if closes_active_run:
            changes["active_run_id"] = None
            changes["assigned_to"] = None
        return changes, closes_active_run

    def _persist_transition(
        self,
        task_id: str,
        current: dict[str, Any],
        status: str,
        reason: str,
        changes: dict[str, Any],
        closes_active_run: bool,
    ) -> None:
        assignments = ", ".join(f"{key} = ?" for key in changes)
        with self.db.transaction() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {assignments}, updated_at = CURRENT_TIMESTAMP WHERE id = ? AND status = ?",
                (*changes.values(), task_id, current["status"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task changed concurrently; refresh before retrying")
            if closes_active_run:
                self._interrupt_active_run(connection, current["active_run_id"])
            self._event(
                connection,
                "task",
                task_id,
                "transitioned",
                {"from": current["status"], "to": status, "reason": reason},
            )
            self._queue_obsidian_sync(connection, "task", task_id)

    @staticmethod
    def _interrupt_active_run(connection: Any, run_id: str) -> None:
        connection.execute(
            """UPDATE task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE id=? AND status IN ('awaiting_thread','running')""",
            (run_id,),
        )
        for table in ("task_conversations", "task_run_conversations"):
            connection.execute(
                f"UPDATE {table} SET status='interrupted', updated_at=CURRENT_TIMESTAMP "
                "WHERE run_id=? AND status='active'",
                (run_id,),
            )

    def _dependencies_satisfied_sql(self) -> str:
        return """NOT EXISTS (
            SELECT 1 FROM task_relations rel JOIN tasks dependency
              ON dependency.id = CASE
                   WHEN rel.relation_type IN ('depends_on','continues_from') THEN rel.target_task_id
                   ELSE rel.source_task_id END
            WHERE (((rel.relation_type IN ('depends_on','continues_from')) AND rel.source_task_id=tasks.id)
                OR (rel.relation_type='blocks' AND rel.target_task_id=tasks.id))
              AND dependency.status != 'done'
        )"""

    @staticmethod
    def _conflicting_active_relation_sql() -> str:
        return """NOT EXISTS (
            SELECT 1 FROM task_relations conflict
            JOIN tasks other ON other.id=CASE
              WHEN conflict.source_task_id=tasks.id THEN conflict.target_task_id ELSE conflict.source_task_id END
            WHERE conflict.relation_type='conflicts_with'
              AND (conflict.source_task_id=tasks.id OR conflict.target_task_id=tasks.id)
              AND other.status IN ('claimed','investigating','implementing','waiting_confirmation','review','code_review','acceptance','failed','blocked')
        )"""

    @staticmethod
    def _retry_workspace_baseline(
        connection: Any, task_id: str, run_type: str
    ) -> tuple[dict[str, Any] | None, str]:
        rows = connection.execute(
            """SELECT id, context_snapshot FROM task_runs
               WHERE task_id=? AND run_type=? AND status IN ('interrupted','expired')
               ORDER BY attempt DESC, created_at DESC""",
            (task_id, run_type),
        ).fetchall()
        for row in rows:
            try:
                context = json.loads(row["context_snapshot"] or "{}")
            except (TypeError, json.JSONDecodeError):
                continue
            baseline = context.get("workspace_baseline")
            if isinstance(baseline, dict) and "available" in baseline:
                return baseline, str(
                    context.get("retry_chain_root_run_id") or row["id"]
                )
        return None, ""

    @staticmethod
    def _execution_profile(
        task: dict[str, Any], context: dict[str, Any], run_type: str
    ) -> dict[str, Any]:
        """Classify the narrow, repeatable subset safe for low reasoning."""
        targets = context.get("located_targets") or []
        steps = context.get("implementation_steps") or []
        checks = context.get("acceptance_commands") or []
        one_target = (
            len(targets) == 1
            and len(targets[0].get("symbols") or []) == 1
            and len(steps) == 1
        )
        automated = bool(checks) and all(
            item.get("check_type") == "automated"
            and str(item.get("command") or "").strip()
            for item in checks
        )
        command_text = "\n".join(str(item.get("command") or "") for item in checks)
        literal_pairs: list[tuple[str, str]] = []
        for step in steps:
            before = str(step.get("before") or "").strip()
            after = str(step.get("after") or "").strip()
            if before and after:
                literal_pairs.append((before, after))
            match = re.search(
                r"将(.{1,80}?)替换为(.{1,80})$", str(step.get("action") or "").strip()
            )
            if match:
                literal_pairs.append((match.group(1).strip(), match.group(2).strip()))
        mechanical_change = any(
            before != after and after in command_text for before, after in literal_pairs
        )
        review_contract = task.get("review_contract") or {}
        sensitive_text = json.dumps(
            {
                "title": task.get("title"),
                "goal": task.get("goal"),
                "scope": task.get("scope"),
                "targets": targets,
            },
            ensure_ascii=False,
        ).lower()
        sensitive_terms = (
            "security",
            "auth",
            "permission",
            "payment",
            "migration",
            "database",
            "schema",
            "encryption",
            "安全",
            "认证",
            "权限",
            "支付",
            "迁移",
            "数据库",
            "加密",
        )
        low_risk = (
            run_type == "execution"
            and task.get("priority") not in {"P0", "P1"}
            and not bool(review_contract.get("separate_acceptance_session"))
            and one_target
            and automated
            and mechanical_change
            and not any(term in sensitive_text for term in sensitive_terms)
        )
        return {
            "risk": "low" if low_risk else "standard",
            "reasoning_effort": "low" if low_risk else "medium",
            "single_file_single_symbol": one_target,
        }

    def _target_snippet(
        self,
        project: str | None,
        target: dict[str, Any],
        line_count: int | None = None,
    ) -> dict[str, Any] | None:
        """Read one bounded UTF-8 target window and bind it to the full-file hash."""
        normalized_project = self._normalize_project(project)
        if not normalized_project:
            return None
        relative = self._normalize_target_file(target.get("file"))
        root = Path(normalized_project).resolve(strict=True)
        candidate = (root / relative).resolve(strict=True)
        try:
            candidate.relative_to(root)
        except ValueError:
            return None
        if not candidate.is_file() or candidate.stat().st_size > 1024 * 1024:
            return None
        raw = candidate.read_bytes()
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            return None
        lines = content.splitlines()
        if not lines:
            return None
        configured = line_count
        if configured is None:
            try:
                configured = int(
                    os.environ.get("CODEX_TASKBOARD_TARGET_SNIPPET_LINES", "60")
                )
            except ValueError:
                configured = 60
        window = max(40, min(int(configured), 80))
        symbol = str((target.get("symbols") or [""])[0]).strip()
        center = 0
        if symbol:
            pattern = re.compile(rf"\b{re.escape(symbol)}\b")
            center = next(
                (index for index, line in enumerate(lines) if pattern.search(line)), 0
            )
        start = max(0, center - window // 2)
        end = min(len(lines), start + window)
        start = max(0, end - window)
        return {
            "file": relative,
            "symbol": symbol,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "start_line": start + 1,
            "end_line": end,
            "total_lines": len(lines),
            "content": "\n".join(lines[start:end]),
        }

    @staticmethod
    def _model_delivery(
        delivery: dict[str, Any],
        *,
        include_diff: bool,
    ) -> dict[str, Any]:
        """Strip server-only workspace state while preserving review evidence."""
        result = {
            "changed_locations": delivery.get("changed_locations") or [],
            "acceptance_evidence": delivery.get("acceptance_evidence") or [],
            "summary": delivery.get("delivery_summary") or "",
            "verification": delivery.get("verification_result") or "",
        }
        if include_diff:
            diff = (delivery.get("artifact_snapshot") or {}).get("diff") or {}
            result["diff"] = {
                key: diff.get(key)
                for key in (
                    "available",
                    "base_revision",
                    "revision",
                    "content",
                    "sha256",
                    "truncated",
                )
                if diff.get(key) not in (None, "")
            }
        return result

    def claim_next_task(
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800
    ) -> dict[str, Any] | None:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if not self.dispatcher_enabled():
            return None
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        self.recover_expired_runs()
        with self.db.transaction() as connection:
            # One active run per project protects workspace baselines, while
            # target locks prevent separate tasks from claiming the same symbol.
            filters = [
                "status IN ('ready', 'rework')",
                "auto_dispatch=1",
                "(dispatch_retry_after IS NULL OR dispatch_retry_after<=CURRENT_TIMESTAMP)",
                self._dependencies_satisfied_sql(),
                self._conflicting_active_relation_sql(),
                """NOT EXISTS (
                    SELECT 1 FROM task_runs project_run
                    JOIN tasks project_task ON project_task.id=project_run.task_id
                    WHERE project_task.id != tasks.id
                      AND project_task.project=tasks.project
                      AND project_run.status IN ('awaiting_thread','running')
                )""",
                """NOT EXISTS (
                    SELECT 1 FROM tasks project_task
                    WHERE project_task.id != tasks.id
                      AND project_task.project=tasks.project
                      AND (
                        project_task.status IN ('failed','blocked','waiting_confirmation')
                        OR (project_task.status='paused' AND project_task.paused_from_status IN ('claimed','investigating','implementing','review','code_review','acceptance','acceptance_blocked','rework'))
                        OR (project_task.retry_required=1 AND project_task.status NOT IN ('done','cancelled'))
                      )
                )""",
                """NOT EXISTS (
                    SELECT 1 FROM task_runs active_run
                    WHERE active_run.task_id=tasks.id
                      AND active_run.run_type IN ('execution','rework')
                      AND active_run.status IN ('awaiting_thread','running')
                )""",
                """NOT EXISTS (
                    SELECT 1 FROM task_targets candidate_target
                    JOIN task_targets locked_target
                      ON locked_target.task_id != candidate_target.task_id
                     AND locked_target.file=candidate_target.file
                     AND (candidate_target.symbol='' OR locked_target.symbol='' OR locked_target.symbol=candidate_target.symbol)
                    JOIN tasks locked_task ON locked_task.id=locked_target.task_id
                    WHERE candidate_target.task_id=tasks.id
                      AND locked_task.project=tasks.project
                      AND locked_task.status IN ('claimed','investigating','implementing','waiting_confirmation','review','failed','blocked')
                )""",
            ]
            values: list[Any] = []
            if project:
                filters.append("project = ?")
                values.append(self._normalize_project(project))
            row = connection.execute(
                f"""SELECT * FROM tasks WHERE {" AND ".join(filters)}
                    ORDER BY retry_required DESC,
                             CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                             created_at ASC LIMIT 1""",
                values,
            ).fetchone()
            if not row:
                return None
            task = _decode_row(row)
            run_type = (
                "rework"
                if task["status"] == "rework" or task.get("retry_run_type") == "rework"
                else ("bugfix" if task.get("type") == "bug" else "execution")
            )
            retry_baseline: dict[str, Any] | None = None
            retry_chain_root_run_id = ""
            if task.get("retry_required"):
                retry_baseline, retry_chain_root_run_id = (
                    self._retry_workspace_baseline(
                        connection,
                        task["id"],
                        run_type,
                    )
                )
            resume_thread_id = str(task.get("codex_thread_id") or "")
            if not resume_thread_id:
                continuation = connection.execute(
                    """SELECT t.codex_thread_id FROM task_relations r JOIN tasks t ON t.id=r.target_task_id
                       WHERE r.source_task_id=? AND r.relation_type IN ('continues_from','defect_of')
                         AND t.codex_thread_id IS NOT NULL AND trim(t.codex_thread_id)!=''
                       ORDER BY r.created_at DESC LIMIT 1""",
                    (task["id"],),
                ).fetchone()
                resume_thread_id = (
                    str(continuation["codex_thread_id"] or "") if continuation else ""
                )
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt), 0) + 1 AS value FROM task_runs WHERE task_id = ?",
                (task["id"],),
            ).fetchone()["value"]
            lease_token = uuid.uuid4().hex
            modifier = f"+{lease_seconds} seconds"
            run_id = self.db.next_id(connection, "RUN")
            connection.execute(
                """INSERT INTO task_runs(
                       id, task_id, parent_run_id, run_type, attempt, status,
                       claimed_by, lease_token, lease_expires_at
                   ) VALUES(?, ?, ?, ?, ?, 'awaiting_thread', ?, ?, datetime('now', ?))""",
                (
                    run_id,
                    task["id"],
                    task.get("primary_run_id"),
                    run_type,
                    attempt,
                    worker_id,
                    lease_token,
                    modifier,
                ),
            )
            cursor = connection.execute(
                """UPDATE tasks SET status='implementing', assigned_to=?, active_run_id=?, retry_required=0,
                   primary_run_id=?, retry_run_type=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status=?""",
                (worker_id, run_id, run_id, task["id"], task["status"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task changed concurrently; retry dispatch")
            self._event(
                connection,
                "run",
                run_id,
                "claimed",
                {
                    "task_id": task["id"],
                    "worker_id": worker_id,
                    "task_status": "implementing",
                },
            )
        try:
            context = self.build_execution_context(task["id"], task.get("project"))
            context["workspace_baseline"] = retry_baseline or self._workspace_state(
                task.get("project")
            )
            context["retry_chain_root_run_id"] = retry_chain_root_run_id or run_id
            context["execution_profile"] = self._execution_profile(
                task, context, run_type
            )
            if context["execution_profile"]["risk"] == "low":
                snippet = self._target_snippet(
                    task.get("project"), context["located_targets"][0]
                )
                if snippet:
                    context["target_snippet"] = snippet
                else:
                    context["execution_profile"].update(
                        {
                            "risk": "standard",
                            "reasoning_effort": "medium",
                            "snippet_unavailable": True,
                        }
                    )
            context = freeze_run_context(
                context,
                task=task,
                run_id=run_id,
                stage=run_type,
                delivery_run_id=str(task.get("primary_run_id") or ""),
            )
        except Exception as exc:
            failure_count = int(task.get("dispatch_failure_count") or 0) + 1
            delay = DISPATCH_RETRY_DELAYS_SECONDS[
                min(failure_count - 1, len(DISPATCH_RETRY_DELAYS_SECONDS) - 1)
            ]
            exhausted = failure_count >= DISPATCH_PREPARATION_LIMIT
            next_status = "blocked" if exhausted else task["status"]
            failure_reason = f"调度上下文准备失败：{exc}"
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE task_runs SET status='expired', completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='awaiting_thread'""",
                    (run_id,),
                )
                connection.execute(
                    """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                       retry_required=?, retry_run_type=?, dispatch_failure_count=?,
                       dispatch_retry_after=CASE WHEN ? THEN NULL ELSE datetime('now', ?) END,
                       last_dispatch_error=?, auto_dispatch=?,
                       blocked_from_status=CASE WHEN ? THEN ? ELSE blocked_from_status END,
                       last_failure_reason=CASE WHEN ? THEN ? ELSE last_failure_reason END,
                       updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND active_run_id=?""",
                    (
                        next_status,
                        int(bool(task.get("retry_required"))),
                        task.get("retry_run_type"),
                        failure_count,
                        int(exhausted),
                        f"+{delay} seconds",
                        failure_reason,
                        0 if exhausted else int(bool(task.get("auto_dispatch", 1))),
                        int(exhausted),
                        task["status"],
                        int(exhausted),
                        failure_reason,
                        task["id"],
                        run_id,
                    ),
                )
                self._event(
                    connection,
                    "run",
                    run_id,
                    "context_build_failed",
                    {
                        "error": str(exc)[:2000],
                        "attempt": failure_count,
                        "retry_seconds": 0 if exhausted else delay,
                        "auto_dispatch_paused": exhausted,
                    },
                )
                self._queue_obsidian_sync(connection, "task", task["id"])
            self.flush_integration_outbox()
            raise
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_runs SET context_snapshot=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(context, ensure_ascii=False), run_id),
            )
            connection.execute(
                """UPDATE tasks SET dispatch_failure_count=0, dispatch_retry_after=NULL,
                   last_dispatch_error='', updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND active_run_id=?""",
                (task["id"], run_id),
            )
        claimed = self.get_task(task["id"])
        return {
            "task": claimed,
            "run": self.get_run(run_id),
            "lease_token": lease_token,
            "resume_thread_id": resume_thread_id,
            "dispatch_prompt": self._dispatch_prompt(task, run_id, run_type, context),
        }

    def claim_next_review_task(
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800
    ) -> dict[str, Any] | None:
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if not self.dispatcher_enabled():
            return None
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        self.recover_expired_runs()
        with self.db.transaction() as connection:
            filters = [
                "tasks.status IN ('review','code_review')",
                "tasks.auto_dispatch=1",
                "(tasks.review_retry_after IS NULL OR tasks.review_retry_after<=CURRENT_TIMESTAMP)",
                self._conflicting_active_relation_sql(),
                """EXISTS (
                    SELECT 1 FROM task_runs delivery
                    WHERE delivery.id=tasks.primary_run_id
                      AND delivery.status='waiting_review'
                )""",
                """NOT EXISTS (
                    SELECT 1 FROM task_runs active_review
                    WHERE active_review.task_id=tasks.id
                      AND active_review.run_type='review'
                      AND active_review.status IN ('awaiting_thread','running')
                )""",
                """NOT EXISTS (
                    SELECT 1 FROM task_runs project_run
                    JOIN tasks project_task ON project_task.id=project_run.task_id
                    WHERE project_task.id != tasks.id
                      AND project_task.project=tasks.project
                      AND project_run.status IN ('awaiting_thread','running')
                )""",
                """NOT EXISTS (
                    SELECT 1 FROM tasks project_task
                    WHERE project_task.id != tasks.id
                      AND project_task.project=tasks.project
                      AND (
                        project_task.status IN ('failed','blocked','waiting_confirmation')
                        OR (project_task.status='paused' AND project_task.paused_from_status IN ('claimed','investigating','implementing','review','code_review','acceptance','acceptance_blocked','rework'))
                        OR (project_task.retry_required=1 AND project_task.status NOT IN ('done','cancelled'))
                      )
                )""",
            ]
            values: list[Any] = []
            if project:
                filters.append("tasks.project=?")
                values.append(self._normalize_project(project))
            row = connection.execute(
                f"""SELECT tasks.* FROM tasks WHERE {" AND ".join(filters)}
                    ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                             updated_at ASC LIMIT 1""",
                values,
            ).fetchone()
            if not row:
                return None
            task = _decode_row(row)
            # Compatibility entry point: callers explicitly using the legacy
            # review API may consume a v2 code_review row as a legacy review.
            if task["status"] == "code_review":
                connection.execute(
                    "UPDATE tasks SET status='review' WHERE id=? AND status='code_review'",
                    (task["id"],),
                )
                task["status"] = "review"
            delivery = connection.execute(
                "SELECT id, run_type FROM task_runs WHERE id=? AND task_id=? AND status='waiting_review'",
                (task.get("primary_run_id"), task["id"]),
            ).fetchone()
            if not delivery or delivery["run_type"] not in {"execution", "rework"}:
                raise ValueError("Task has no current delivery run")
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 AS value FROM task_runs WHERE task_id=?",
                (task["id"],),
            ).fetchone()["value"]
            lease_token = uuid.uuid4().hex
            previous_review = connection.execute(
                """SELECT mapping.thread_id FROM task_run_conversations mapping
                   JOIN task_runs review ON review.id=mapping.run_id
                   WHERE review.task_id=? AND review.run_type='review'
                     AND review.delivery_run_id=? AND review.status IN ('interrupted','expired')
                   ORDER BY review.attempt DESC, review.created_at DESC LIMIT 1""",
                (task["id"], delivery["id"]),
            ).fetchone()
            run_id = self.db.next_id(connection, "RUN")
            connection.execute(
                """INSERT INTO task_runs(
                       id, task_id, parent_run_id, delivery_run_id, run_type, attempt,
                       status, claimed_by, lease_token, lease_expires_at
                   ) VALUES(?, ?, ?, ?, 'review', ?, 'awaiting_thread', ?, ?, datetime('now', ?))""",
                (
                    run_id,
                    task["id"],
                    delivery["id"],
                    delivery["id"],
                    attempt,
                    worker_id,
                    lease_token,
                    f"+{lease_seconds} seconds",
                ),
            )
            connection.execute(
                "UPDATE tasks SET assigned_to=?, active_run_id=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='review'",
                (worker_id, run_id, task["id"]),
            )
            self._event(
                connection,
                "run",
                run_id,
                "review_claimed",
                {"task_id": task["id"], "worker_id": worker_id},
            )
        try:
            prepared = self.prepare_review_location(task["id"])
            context = {
                "task_id": task["id"],
                "review_location_analysis_id": prepared["analysis_id"],
                "delivery_run_id": delivery["id"],
                "location_plan": prepared["location"],
                "obsidian_evidence": prepared["obsidian"],
                "changed_locations": prepared["changed_locations"],
                "acceptance_evidence": prepared["acceptance_evidence"],
                "acceptance_criteria": prepared["acceptance_criteria"],
            }
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE task_runs SET context_snapshot=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(context, ensure_ascii=False), run_id),
                )
        except Exception as exc:
            interrupt_count = int(task.get("review_interrupt_count") or 0) + 1
            delay = REVIEW_RETRY_DELAYS_SECONDS[
                min(interrupt_count - 1, len(REVIEW_RETRY_DELAYS_SECONDS) - 1)
            ]
            exhausted = interrupt_count >= REVIEW_INTERRUPT_LIMIT
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE task_runs SET status='expired', completed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (run_id,),
                )
                connection.execute(
                    """UPDATE tasks SET active_run_id=NULL, assigned_to=NULL,
                       review_interrupt_count=?, review_retry_after=CASE WHEN ? THEN NULL ELSE datetime('now', ?) END,
                       auto_dispatch=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND active_run_id=?""",
                    (
                        interrupt_count,
                        int(exhausted),
                        f"+{delay} seconds",
                        0 if exhausted else 1,
                        task["id"],
                        run_id,
                    ),
                )
                self._event(
                    connection,
                    "run",
                    run_id,
                    "review_preparation_failed",
                    {
                        "reason": str(exc)[:2000],
                        "attempt": interrupt_count,
                        "retry_seconds": 0 if exhausted else delay,
                        "auto_dispatch_paused": exhausted,
                    },
                )
                self._queue_obsidian_sync(connection, "task", task["id"])
            self.flush_integration_outbox()
            raise
        return {
            "task": self.get_task(task["id"]),
            "run": self.get_run(run_id),
            "lease_token": lease_token,
            "resume_thread_id": str(
                previous_review["thread_id"] if previous_review else ""
            ),
            "dispatch_prompt": self._review_location_prompt(task, run_id, prepared),
        }

    def claim_next_code_review_task(
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800
    ) -> dict[str, Any] | None:
        """Claim the independent v2 code-review stage."""
        worker_id = str(worker_id or "").strip()
        if not worker_id or not self.dispatcher_enabled():
            return None
        with self.db.transaction() as connection:
            values: list[Any] = []
            where = [
                "t.status='code_review'",
                "t.auto_dispatch=1",
                "(t.review_retry_after IS NULL OR t.review_retry_after<=CURRENT_TIMESTAMP)",
                "EXISTS (SELECT 1 FROM task_runs d WHERE d.id=t.primary_run_id AND d.status='waiting_review')",
                "NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=t.id AND r.run_type='code_review' AND r.status IN ('awaiting_thread','running'))",
                "NOT EXISTS (SELECT 1 FROM task_runs pr JOIN tasks pt ON pt.id=pr.task_id WHERE pt.id!=t.id AND pt.project=t.project AND pr.status IN ('awaiting_thread','running'))",
                "NOT EXISTS (SELECT 1 FROM tasks pt WHERE pt.id!=t.id AND pt.project=t.project AND (pt.status IN ('failed','blocked','waiting_confirmation') OR (pt.status='paused' AND pt.paused_from_status IN ('claimed','investigating','implementing','review','code_review','acceptance','acceptance_blocked','rework')) OR (pt.retry_required=1 AND pt.status NOT IN ('done','cancelled'))))",
            ]
            if project:
                where.append("t.project=?")
                values.append(self._normalize_project(project))
            row = connection.execute(
                f"SELECT t.* FROM tasks t WHERE {' AND '.join(where)} ORDER BY t.created_at LIMIT 1",
                values,
            ).fetchone()
            if not row:
                return None
            task = _decode_row(row)
            delivery = connection.execute(
                "SELECT * FROM task_runs WHERE id=? AND status='waiting_review'",
                (task["primary_run_id"],),
            ).fetchone()
            previous_review = connection.execute(
                """SELECT mapping.thread_id FROM task_run_conversations mapping
                   JOIN task_runs prior ON prior.id=mapping.run_id
                   WHERE prior.task_id=? AND prior.run_type='code_review'
                     AND prior.delivery_run_id=? AND prior.status IN ('interrupted','expired')
                     AND mapping.thread_id IS NOT NULL AND trim(mapping.thread_id)!=''
                   ORDER BY prior.attempt DESC, prior.created_at DESC LIMIT 1""",
                (task["id"], delivery["id"]),
            ).fetchone()
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 value FROM task_runs WHERE task_id=?",
                (task["id"],),
            ).fetchone()["value"]
            run_id = self.db.next_id(connection, "RUN")
            lease_token = uuid.uuid4().hex
            connection.execute(
                """INSERT INTO task_runs(id, task_id, parent_run_id, delivery_run_id, run_type, attempt,
                   status, claimed_by, lease_token, lease_expires_at) VALUES(?,?,?,?, 'code_review', ?, 'awaiting_thread', ?, ?, datetime('now', ?))""",
                (
                    run_id,
                    task["id"],
                    delivery["id"],
                    delivery["id"],
                    attempt,
                    worker_id,
                    lease_token,
                    f"+{max(300, min(int(lease_seconds), 7200))} seconds",
                ),
            )
            connection.execute(
                "UPDATE tasks SET active_run_id=?, assigned_to=? WHERE id=? AND status='code_review'",
                (run_id, worker_id, task["id"]),
            )
        delivery_snapshot = _decode_row(delivery, RUN_JSON_FIELDS)
        context = self.build_code_review_context(task["id"], task.get("project"))
        context.update(
            {
                "delivery_run_id": delivery["id"],
                "delivery": self._model_delivery(delivery_snapshot, include_diff=True),
            }
        )
        context = freeze_run_context(
            context,
            task=task,
            run_id=run_id,
            stage="code_review",
            delivery_run_id=delivery["id"],
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_runs SET context_snapshot=? WHERE id=?",
                (json.dumps(context, ensure_ascii=False), run_id),
            )
        confirmed_checks = [
            str(
                item.get("id") or item.get("criterion") or item.get("description") or ""
            ).strip()
            if isinstance(item, dict)
            else str(item).strip()
            for item in (task.get("review_contract") or {}).get("checks", [])
        ]
        confirmed_checks = [item for item in confirmed_checks if item]
        return {
            "task": self.get_task(task["id"]),
            "run": self.get_run(run_id),
            "lease_token": lease_token,
            "resume_thread_id": str(
                previous_review["thread_id"] if previous_review else ""
            ),
            "dispatch_prompt": self._code_review_prompt(
                task,
                run_id,
                confirmed_checks,
                context,
            ),
        }

    def review_code(
        self,
        task_id: str,
        run_id: str,
        verdict: str,
        reasons: list[str] | None = None,
        passed_items: list[str] | None = None,
        failed_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        if (
            task.get("active_run_id") != run_id
            or task["status"] != "code_review"
            or run["run_type"] != "code_review"
            or run["status"] != "running"
        ):
            raise ValueError("A running code_review run is required")
        verdict = str(verdict).lower().strip()
        if verdict not in {"pass", "fail"}:
            raise ValueError("verdict must be pass or fail")
        reasons = [str(x).strip() for x in (reasons or []) if str(x).strip()]
        passed_items = [str(x).strip() for x in (passed_items or []) if str(x).strip()]
        failed_criteria = [
            str(x).strip() for x in (failed_criteria or []) if str(x).strip()
        ]
        contract = task.get("review_contract") or {}
        review_items = []
        for item in contract.get("checks", []) or []:
            if isinstance(item, dict):
                review_items.append(
                    str(
                        item.get("id")
                        or item.get("criterion")
                        or item.get("description")
                        or ""
                    ).strip()
                )
            else:
                review_items.append(str(item).strip())
        if not review_items:
            review_items = [
                str(item).strip() for item in (contract.get("rules", []) or [])
            ]
        review_items = [item for item in review_items if item]
        criteria = set(review_items)
        if len(passed_items) != len(set(passed_items)) or len(failed_criteria) != len(
            set(failed_criteria)
        ):
            raise ValueError("Review result items must not contain duplicates")
        if set(passed_items) & set(failed_criteria):
            raise ValueError("Review passed_items and failed_criteria must not overlap")
        if set(passed_items) | set(failed_criteria) != criteria:
            raise ValueError(
                "Review result must exactly cover every confirmed criterion; expected: "
                + json.dumps(review_items, ensure_ascii=False)
            )
        if verdict == "pass":
            if failed_criteria:
                raise ValueError("A passing code review cannot contain failed_criteria")
            if set(passed_items) != criteria:
                raise ValueError(
                    "A passing code review must cover every confirmed criterion"
                )
        elif not reasons or not failed_criteria:
            raise ValueError(
                "A failed code review requires reasons and failed_criteria"
            )
        with self.db.transaction() as connection:
            round_no = connection.execute(
                "SELECT COALESCE(MAX(round),0)+1 value FROM reviews WHERE task_id=?",
                (task_id,),
            ).fetchone()["value"]
            connection.execute(
                "INSERT INTO reviews(task_id,round,verdict,reasons,passed_items,failed_criteria) VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    round_no,
                    verdict,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(passed_items, ensure_ascii=False),
                    json.dumps(failed_criteria, ensure_ascii=False),
                ),
            )
            connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_runs SET status='review_failed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                (run.get("delivery_run_id"),),
            ) if verdict == "fail" else connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                (run.get("delivery_run_id"),),
            )
            next_status = "rework" if verdict == "fail" else "acceptance"
            connection.execute(
                "UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL, last_review_reasons=?, last_failed_criteria=?, review_rework_count=review_rework_count+? WHERE id=?",
                (
                    next_status,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(failed_criteria, ensure_ascii=False),
                    int(verdict == "fail"),
                    task_id,
                ),
            )
            self._event(
                connection,
                "task",
                task_id,
                "code_reviewed",
                {"verdict": verdict, "reasons": reasons},
            )
        return self.get_task(task_id)

    def claim_next_acceptance_task(
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800
    ) -> dict[str, Any] | None:
        worker_id = str(worker_id or "").strip()
        if not worker_id or not self.dispatcher_enabled():
            return None
        with self.db.transaction() as connection:
            values: list[Any] = []
            where = [
                "t.status='acceptance'",
                "t.auto_dispatch=1",
                "(t.review_retry_after IS NULL OR t.review_retry_after<=CURRENT_TIMESTAMP)",
                "NOT EXISTS (SELECT 1 FROM task_runs r WHERE r.task_id=t.id AND r.run_type='acceptance' AND r.status IN ('awaiting_thread','running'))",
                "NOT EXISTS (SELECT 1 FROM task_runs pr JOIN tasks pt ON pt.id=pr.task_id WHERE pt.id!=t.id AND pt.project=t.project AND pr.status IN ('awaiting_thread','running'))",
                "NOT EXISTS (SELECT 1 FROM tasks pt WHERE pt.id!=t.id AND pt.project=t.project AND (pt.status IN ('failed','blocked','waiting_confirmation') OR (pt.status='paused' AND pt.paused_from_status IN ('claimed','investigating','implementing','review','code_review','acceptance','acceptance_blocked','rework')) OR (pt.retry_required=1 AND pt.status NOT IN ('done','cancelled'))))",
            ]
            if project:
                where.append("t.project=?")
                values.append(self._normalize_project(project))
            row = connection.execute(
                f"SELECT t.* FROM tasks t WHERE {' AND '.join(where)} ORDER BY t.created_at LIMIT 1",
                values,
            ).fetchone()
            if not row:
                return None
            task = _decode_row(row)
            delivery = connection.execute(
                "SELECT * FROM task_runs WHERE task_id=? AND run_type IN ('execution','rework','bugfix') AND status='completed' ORDER BY attempt DESC LIMIT 1",
                (task["id"],),
            ).fetchone()
            if not delivery:
                raise ValueError("Acceptance has no completed delivery")
            previous_acceptance = connection.execute(
                """SELECT mapping.thread_id FROM task_run_conversations mapping
                   JOIN task_runs prior ON prior.id=mapping.run_id
                   WHERE prior.task_id=? AND prior.run_type='acceptance'
                     AND prior.delivery_run_id=? AND prior.status IN ('interrupted','expired')
                     AND mapping.thread_id IS NOT NULL AND trim(mapping.thread_id)!=''
                   ORDER BY prior.attempt DESC, prior.created_at DESC LIMIT 1""",
                (task["id"], delivery["id"]),
            ).fetchone()
            verifier_thread = None
            if not previous_acceptance and not bool(
                (task.get("review_contract") or {}).get(
                    "separate_acceptance_session", False
                )
            ):
                verifier_thread = connection.execute(
                    """SELECT mapping.thread_id FROM task_run_conversations mapping
                       JOIN task_runs review ON review.id=mapping.run_id
                       WHERE review.task_id=? AND review.run_type='code_review'
                         AND review.delivery_run_id=? AND review.status='completed'
                         AND mapping.thread_id IS NOT NULL AND trim(mapping.thread_id)!=''
                       ORDER BY review.attempt DESC, review.created_at DESC LIMIT 1""",
                    (task["id"], delivery["id"]),
                ).fetchone()
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 value FROM task_runs WHERE task_id=?",
                (task["id"],),
            ).fetchone()["value"]
            run_id = self.db.next_id(connection, "RUN")
            token = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO task_runs(id,task_id,parent_run_id,delivery_run_id,run_type,attempt,status,claimed_by,lease_token,lease_expires_at) VALUES(?,?,?,?, 'acceptance', ?, 'awaiting_thread', ?, ?, datetime('now', ?))",
                (
                    run_id,
                    task["id"],
                    delivery["id"],
                    delivery["id"],
                    attempt,
                    worker_id,
                    token,
                    f"+{max(300, min(int(lease_seconds), 7200))} seconds",
                ),
            )
            connection.execute(
                "UPDATE tasks SET active_run_id=?, assigned_to=? WHERE id=? AND status='acceptance'",
                (run_id, worker_id, task["id"]),
            )
        delivery_snapshot = _decode_row(delivery, RUN_JSON_FIELDS)
        context = self.build_acceptance_context(task["id"], task.get("project"))
        needs_diff = any(
            str(item.get("check_type") or "") != "automated"
            for item in context.get("acceptance") or []
        )
        context.update(
            {
                "delivery_run_id": delivery["id"],
                "delivery": self._model_delivery(
                    delivery_snapshot, include_diff=needs_diff
                ),
            }
        )
        context = freeze_run_context(
            context,
            task=task,
            run_id=run_id,
            stage="acceptance",
            delivery_run_id=delivery["id"],
        )
        with self.db.transaction() as connection:
            connection.execute(
                "UPDATE task_runs SET context_snapshot=? WHERE id=?",
                (json.dumps(context, ensure_ascii=False), run_id),
            )
        resume_thread_id = str(
            previous_acceptance["thread_id"]
            if previous_acceptance
            else (verifier_thread["thread_id"] if verifier_thread else "")
        )
        return {
            "task": self.get_task(task["id"]),
            "run": self.get_run(run_id),
            "lease_token": token,
            "resume_thread_id": resume_thread_id,
            "dispatch_prompt": self._acceptance_prompt(task, run_id, context),
        }

    def accept_task(
        self,
        task_id: str,
        run_id: str,
        verdict: str,
        reasons: list[str] | None = None,
        passed_criteria: list[str] | None = None,
        failed_criteria: list[str] | None = None,
        failure_locations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        if (
            task.get("active_run_id") != run_id
            or task["status"] != "acceptance"
            or run["run_type"] != "acceptance"
            or run["status"] != "running"
        ):
            raise ValueError("A running acceptance run is required")
        verdict = str(verdict).lower().strip()
        reasons = [str(x).strip() for x in (reasons or []) if str(x).strip()]
        criteria = [str(x) for x in task.get("acceptance_criteria", [])]
        passed_criteria = [str(x) for x in (passed_criteria or [])]
        failed_criteria = [str(x) for x in (failed_criteria or [])]
        if verdict not in {"pass", "fail"}:
            raise ValueError("verdict must be pass or fail")
        if len(passed_criteria) != len(set(passed_criteria)) or len(
            failed_criteria
        ) != len(set(failed_criteria)):
            raise ValueError("Acceptance result criteria must not contain duplicates")
        if set(passed_criteria) & set(failed_criteria):
            raise ValueError("passed_criteria and failed_criteria must not overlap")
        if set(passed_criteria) | set(failed_criteria) != set(criteria):
            raise ValueError("Acceptance result must exactly cover every criterion")
        if verdict == "pass":
            if failed_criteria or set(passed_criteria) != set(criteria):
                raise ValueError(
                    "A passing acceptance must have exactly all criteria in passed_criteria and none failed"
                )
        elif not failed_criteria or not reasons:
            raise ValueError("A failed acceptance requires reasons and failed_criteria")
        if verdict == "pass":
            snapshot = run.get("context_snapshot") or {}
            self._assert_required_acceptance_checks_passed(
                run_id,
                snapshot.get("acceptance") or snapshot.get("acceptance_plan") or [],
            )
        bug_id = None
        parent_task_id = None
        with self.db.transaction() as connection:
            round_no = connection.execute(
                "SELECT COALESCE(MAX(round),0)+1 value FROM acceptance_results WHERE task_id=?",
                (task_id,),
            ).fetchone()["value"]
            connection.execute(
                "INSERT INTO acceptance_results(task_id,run_id,delivery_run_id,round,verdict,reasons,passed_criteria,failed_criteria,failure_locations) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    task_id,
                    run_id,
                    run.get("delivery_run_id"),
                    round_no,
                    verdict,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(passed_criteria, ensure_ascii=False),
                    json.dumps(failed_criteria, ensure_ascii=False),
                    json.dumps(failure_locations or [], ensure_ascii=False),
                ),
            )
            connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (run_id,),
            )
            if verdict == "pass":
                connection.execute(
                    "UPDATE tasks SET status='done', active_run_id=NULL, assigned_to=NULL WHERE id=?",
                    (task_id,),
                )
                parent = connection.execute(
                    "SELECT target_task_id FROM task_relations WHERE source_task_id=? AND relation_type='defect_of' LIMIT 1",
                    (task_id,),
                ).fetchone()
                if parent:
                    cursor = connection.execute(
                        "UPDATE tasks SET status='acceptance', active_run_id=NULL, assigned_to=NULL WHERE id=? AND status='acceptance_blocked'",
                        (parent["target_task_id"],),
                    )
                    if cursor.rowcount:
                        parent_task_id = parent["target_task_id"]
                        self._event(
                            connection,
                            "task",
                            parent_task_id,
                            "acceptance_unblocked",
                            {"resolved_bug_task_id": task_id},
                        )
                        self._queue_obsidian_sync(
                            connection, "task", parent_task_id
                        )
            elif task.get("type") == "bug":
                connection.execute(
                    "UPDATE tasks SET status='rework', active_run_id=NULL, assigned_to=NULL, last_failure_reason=?, last_failed_criteria=? WHERE id=?",
                    (
                        "；".join(reasons),
                        json.dumps(failed_criteria, ensure_ascii=False),
                        task_id,
                    ),
                )
            else:
                bug_id = self.db.next_id(connection, "BUG")
                connection.execute(
                    "INSERT INTO tasks(id, title, type, project, modules, status, priority, goal, scope, out_of_scope, acceptance_criteria, codex_thread_id, token_budget, location_context, acceptance_plan, dependency_analysis, implementation_contract, review_contract, parent_acceptance_task_id, workflow_version) SELECT ?, ?, 'bug', project, modules, 'ready', priority, ?, scope, out_of_scope, ?, codex_thread_id, token_budget, location_context, acceptance_plan, dependency_analysis, implementation_contract, review_contract, ?, workflow_version FROM tasks WHERE id=?",
                    (
                        bug_id,
                        f"{task_id} 验收缺陷",
                        "；".join(reasons) or "修复验收失败",
                        json.dumps(failed_criteria, ensure_ascii=False),
                        task_id,
                        task_id,
                    ),
                )
                connection.execute(
                    "INSERT INTO task_relations(source_task_id,target_task_id,relation_type,description) VALUES(?,?, 'defect_of', ?)",
                    (bug_id, task_id, "验收自动创建"),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO task_targets(task_id,file,symbol) SELECT ?,file,symbol FROM task_targets WHERE task_id=?",
                    (bug_id, task_id),
                )
                connection.execute(
                    "UPDATE acceptance_results SET created_bug_task_id=? WHERE task_id=? AND run_id=?",
                    (bug_id, task_id, run_id),
                )
                connection.execute(
                    "UPDATE tasks SET status='acceptance_blocked', active_run_id=NULL, assigned_to=NULL, last_failure_reason=? WHERE id=?",
                    ("；".join(reasons) or "验收失败", task_id),
                )
                self._event(
                    connection,
                    "task",
                    bug_id,
                    "created_from_acceptance_failure",
                    {"source_task_id": task_id, "acceptance_run_id": run_id},
                )
                self._queue_obsidian_sync(connection, "task", bug_id)
            connection.execute(
                "UPDATE task_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_run_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            self._event(
                connection,
                "task",
                task_id,
                "acceptance_completed",
                {
                    "round": round_no,
                    "verdict": verdict,
                    "reasons": reasons,
                    "passed_criteria": passed_criteria,
                    "failed_criteria": failed_criteria,
                    "created_bug_task_id": bug_id,
                    "unblocked_parent_task_id": parent_task_id,
                },
            )
            self._queue_obsidian_sync(connection, "task", task_id)
        updated = self.get_task(task_id)
        if verdict == "pass":
            self._create_experience(updated)
        self.flush_integration_outbox()
        return updated

    def auto_accept_automated_task(self, task_id: str, run_id: str) -> dict[str, Any]:
        """Complete acceptance without a model when every criterion is an automated check."""
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        snapshot = run.get("context_snapshot") or {}
        plans = list(
            snapshot.get("acceptance") or snapshot.get("acceptance_plan") or []
        )
        criteria = [str(item) for item in task.get("acceptance_criteria", [])]
        plan_criteria = [
            str(item.get("criterion") or "") for item in plans if isinstance(item, dict)
        ]
        eligible = (
            bool(criteria)
            and len(plans) == len(criteria)
            and Counter(plan_criteria) == Counter(criteria)
        )
        eligible = eligible and all(
            isinstance(item, dict)
            and bool(item.get("required", True))
            and str(
                item.get("check_type")
                or ("automated" if item.get("command") else "static_review")
            )
            == "automated"
            and bool(str(item.get("command") or "").strip())
            for item in plans
        )
        if not eligible:
            return {"eligible": False, "completed": False}
        if (
            task.get("active_run_id") != run_id
            or task.get("status") != "acceptance"
            or run.get("run_type") != "acceptance"
            or run.get("status") not in {"awaiting_thread", "running"}
        ):
            raise ValueError(
                "An active acceptance run is required for automatic acceptance"
            )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE task_runs SET status='running', started_at=COALESCE(started_at, CURRENT_TIMESTAMP),
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='awaiting_thread'""",
                (run_id,),
            )
        checks = self.run_acceptance_checks(task_id, run_id)
        if not checks.get("all_required_passed"):
            return {"eligible": True, "completed": False, "checks": checks}
        updated = self.accept_task(
            task_id,
            run_id,
            "pass",
            ["全部自动化验收检查通过"],
            criteria,
            [],
            [],
        )
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE task_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP
                   WHERE task_id=? AND role='code_review' AND status='active'""",
                (task_id,),
            )
            connection.execute(
                """UPDATE task_run_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP
                   WHERE task_id=? AND role='code_review' AND status='active'""",
                (task_id,),
            )
        return {"eligible": True, "completed": True, "checks": checks, "task": updated}

    def list_acceptance_results(self, task_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM acceptance_results WHERE task_id=? ORDER BY round",
                (task_id,),
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            for field in (
                "reasons",
                "passed_criteria",
                "failed_criteria",
                "failure_locations",
            ):
                item[field] = json.loads(item[field] or "[]")
            result.append(item)
        return result

    @staticmethod
    def _review_location_prompt(
        task: dict[str, Any], run_id: str, prepared: dict[str, Any]
    ) -> str:
        changed = "; ".join(
            f"{item.get('file')}#{item.get('symbol') or ','.join(item.get('symbols', [])) or '-'}"
            for item in prepared.get("changed_locations", [])[:10]
        )
        return (
            "$codex-taskboard-lifecycle\n\n"
            f"独立验收 Codex Taskboard 任务 {task['id']}（验收运行 {run_id}）。"
            f"验收定位分析 ID：{prepared['analysis_id']}；实际改动位置：{changed}。"
            "先以这些改动位置为种子按 CodeGraph、GitNexus、直接源码匹配的顺序选择首个可用定位方式，"
            "只补充直接相关调用方与既有测试，并调用 report_location_status 上报所选证据；"
            "再调用 complete_location_analysis 保存精确 targets 与逐条 acceptance_plan，随后调用 prepare_task_review。"
            f"prepare_task_review 必须复用验收运行 {run_id}。读取返回的 context_snapshot 后逐条验收，"
            "不得重新扫描整个项目、不得修改实现；必须先调用 run_acceptance_checks 执行计划中的自动化检查，"
            "最后调用 review_task，并传入当前验收运行 ID。"
            "passed_items 与 failed_criteria 必须逐项使用原始验收标准的精确文本并完整覆盖全部标准；"
            "通过时 passed_items 必须包含全部标准，范围外问题必须另建任务，不得据此打回。"
        )

    @staticmethod
    def _dispatch_prompt(
        task: dict[str, Any],
        run_id: str,
        run_type: str,
        context: dict[str, Any],
    ) -> str:
        retry_note = ""
        if run_type == "rework":
            review_reasons = task.get("last_review_reasons") or (
                [task.get("last_failure_reason")]
                if task.get("last_failure_reason")
                else []
            )
            retry_note = (
                "这是验收不通过后的原会话返工。验收失败原因："
                + "；".join(review_reasons)
                + "。未通过标准："
                + "；".join(task.get("last_failed_criteria") or [])
                + "。请像收到人工反馈一样在当前会话继续修复，并重新提交交付。"
            )
        if task.get("retry_required"):
            retry_note += (
                f"这是失败或中断后的显式重试；上次停止原因：{task.get('last_failure_reason') or '未记录'}。"
                "继续使用保存的重试链工作区基线，交付时必须报告失败前后累计产生的全部改动。"
            )
        return (
            "$codex-taskboard-lifecycle\n\n"
            f"任务 {task['id']}（运行 {run_id}，类型 {run_type}）。"
            f"{retry_note}"
            "以 RUN_CONTEXT_JSON 为唯一任务输入，只读取已定位目标及正确性所需的直接依赖，不再获取任务详情。"
            "实现并验证后调用 submit_task_delivery；需要用户决策或无法继续时调用 report_run_blocked。"
            f"\n\nRUN_CONTEXT_JSON={prompt_context(context)}"
        )

    @staticmethod
    def _code_review_prompt(
        task: dict[str, Any],
        run_id: str,
        confirmed_checks: list[str],
        context: dict[str, Any],
    ) -> str:
        return (
            "$codex-taskboard-lifecycle\n\n"
            "Code Review 阶段只使用提示内的 RUN_CONTEXT_JSON，不要搜索工具目录、数据库或任务详情。"
            "直接检查 delivery.diff、implementation 和 review_checks，不修改代码；"
            "完成后调用 review_code。"
            f"任务 {task['id']}（运行 {run_id}）。passed_items 与 failed_criteria 必须且只能完整划分"
            f"这些 review_checks（不要混入 acceptance_criteria）：{json.dumps(confirmed_checks, ensure_ascii=False)}。"
            f"\n\nRUN_CONTEXT_JSON={prompt_context(context)}"
        )

    @staticmethod
    def _acceptance_prompt(
        task: dict[str, Any],
        run_id: str,
        context: dict[str, Any],
    ) -> str:
        return (
            "$codex-taskboard-lifecycle\n\n"
            "功能验收阶段只使用提示内的 RUN_CONTEXT_JSON，不要搜索工具目录、数据库或任务详情。"
            "只按 acceptance 和 delivery 验证，不修改代码；存在自动检查时调用 run_acceptance_checks，"
            "该工具会复用相同交付和工作区指纹下已通过的结果；最后调用 accept_task。"
            f"任务 {task['id']}（运行 {run_id}）。"
            f"\n\nRUN_CONTEXT_JSON={prompt_context(context)}"
        )
