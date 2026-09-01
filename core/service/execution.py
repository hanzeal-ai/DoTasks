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
from ..self_healing import (
    SELF_HEAL_ATTEMPT_LIMIT,
    classify_recoverable_failure,
    normalize_self_heal_locations,
)
from .domain import (
    ACTIVE_RUN_STATUSES,
    DISPATCH_PREPARATION_LIMIT,
    DISPATCH_RETRY_DELAYS_SECONDS,
    JSON_FIELDS,
    REQUIREMENT_DECOMPOSITION_ATTEMPT_LIMIT,
    REVIEW_INTERRUPT_LIMIT,
    REVIEW_RETRY_DELAYS_SECONDS,
    RUN_JSON_FIELDS,
    TASK_TRANSITIONS,
    decode_row,
)


class TaskLifecycleMixin:
    """Task state transitions and atomic claims for execution and review stages."""

    def _self_heal_plan(
        self,
        task: dict[str, Any],
        failure_category: str,
        evidence: list[str],
        failure_locations: Any = None,
    ) -> dict[str, Any]:
        category = str(failure_category or "").strip().lower()
        if category not in {"project", "environment"}:
            return {
                "category": "implementation",
                "scheduled": False,
                "attempt": 0,
                "contract": task.get("implementation_contract") or {},
                "locations": [],
            }
        contract = dict(task.get("implementation_contract") or {})
        previous = contract.get("self_heal") or {}
        attempt = int(previous.get("attempt") or 0) + 1
        locations = []
        targets = [dict(item) for item in contract.get("targets") or [] if isinstance(item, dict)]
        target_by_file = {
            self._normalize_target_file(item.get("file")): item
            for item in targets if str(item.get("file") or "").strip()
        }
        evidence_text = "；".join(str(item).strip() for item in evidence if str(item).strip())
        for location in normalize_self_heal_locations(failure_locations):
            file = self._normalize_target_file(location["file"])
            project_file = Path(str(task.get("project") or "")) / file
            mode = str(location.get("mode") or "").strip().lower()
            if not mode:
                mode = "modify" if project_file.exists() else "create"
            action = (
                (location.get("tasks") or [{}])[0].get("action")
                or f"修复{category}问题：{evidence_text or file}"
            )
            normalized_location = {
                "file": file,
                "symbols": location["symbols"],
                "mode": mode,
                "tasks": location.get("tasks") or [{"action": action}],
            }
            locations.append(normalized_location)
            target = target_by_file.get(file)
            if target is None:
                target = dict(normalized_location)
                targets.append(target)
                target_by_file[file] = target
            else:
                target.setdefault("mode", mode)
                target.setdefault("symbols", [])
                target["symbols"] = list(dict.fromkeys([
                    *target["symbols"], *normalized_location["symbols"],
                ]))
                target.setdefault("tasks", [])
                for target_task in normalized_location["tasks"]:
                    if target_task not in target["tasks"]:
                        target["tasks"].append(target_task)
        contract["targets"] = targets
        contract["self_heal"] = {
            "category": category,
            "attempt": attempt,
            "attempt_limit": SELF_HEAL_ATTEMPT_LIMIT,
            "scheduled": attempt <= SELF_HEAL_ATTEMPT_LIMIT,
            "evidence": [str(item).strip() for item in evidence if str(item).strip()][-10:],
            "locations": locations,
        }
        return {
            "category": category,
            "scheduled": attempt <= SELF_HEAL_ATTEMPT_LIMIT,
            "attempt": attempt,
            "contract": contract,
            "locations": locations,
        }

    @staticmethod
    def _persist_self_heal_targets(
        connection: Any, task_id: str, locations: list[dict[str, Any]],
    ) -> None:
        for location in locations:
            symbols = location.get("symbols") or [""]
            for symbol in symbols:
                connection.execute(
                    "INSERT OR IGNORE INTO task_targets(task_id,file,symbol) VALUES(?,?,?)",
                    (task_id, location["file"], str(symbol or "")),
                )

    @staticmethod
    def _stage_token_reserve(task: dict[str, Any], run_type: str) -> int:
        """Keep enough budget for a stage before creating a run or model turn."""
        budget = max(0, int(task.get("token_budget") or 0))
        if budget <= 0:
            return 0
        if run_type == "code_review":
            return min(50_000, max(5_000, budget // 10))
        return min(150_000, max(10_000, budget // 5))

    def _pause_for_stage_budget_preflight(
        self, connection: Any, task: dict[str, Any], run_type: str,
    ) -> bool:
        reserve = self._stage_token_reserve(task, run_type)
        budget = int(task.get("token_budget") or 0)
        used = int(task.get("effective_token_used") or 0)
        remaining = max(0, budget - used)
        if reserve <= 0 or remaining >= reserve:
            return False
        reason = (
            f"阶段预算预检未通过：{run_type} 至少需要预留 {reserve} 有效 Token，"
            f"当前仅剩 {remaining}（已用 {used}/{budget}）"
        )
        connection.execute(
            """UPDATE tasks SET status='waiting_confirmation', active_run_id=NULL,
               assigned_to=NULL, auto_dispatch=0, last_failure_reason=?,
               last_failure_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
               WHERE id=?""",
            (reason, task["id"]),
        )
        self._event(
            connection,
            "task",
            task["id"],
            "stage_budget_preflight_blocked",
            {
                "run_type": run_type,
                "required_reserve": reserve,
                "remaining": remaining,
                "effective_token_used": used,
                "token_budget": budget,
            },
        )
        self._queue_obsidian_sync(connection, "task", task["id"])
        return True

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
                and (
                    int(current.get("effective_token_used") or 0)
                    >= int(current.get("token_budget") or 0)
                    or str(current.get("last_failure_reason") or "").startswith(
                        "阶段预算预检未通过："
                    )
                )
                and token_budget > int(current.get("effective_token_used") or 0)
                and token_budget > int(current.get("token_budget") or 0)
            )
            if not budget_restart:
                raise ValueError(
                    "Token budget can only be increased when restarting a budget-exhausted task"
                )
        same_stage_update = (
            current["status"] == status
            and status == "code_review"
            and set(updates) == {"auto_dispatch"}
        )
        restoring_stage = (
            current["status"] == "blocked"
            and current.get("blocked_from_status") == status
            and status == "code_review"
        )
        # The Code Review API persists its result and stage transition atomically;
        # allowing the generic transition endpoint would bypass that invariant.
        if (
            status in {"code_review", "done"}
            and not restoring_stage
            and not same_stage_update
        ):
            raise ValueError(
                "Review stage transitions must use the dedicated Code Review API"
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
                if previous == "code_review"
                else (
                    "rework"
                    if previous == "rework" or current.get("retry_run_type") == "rework"
                    else "ready"
                )
            )
            if status != expected:
                raise ValueError(f"Blocked task must return to {expected}")
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
                current["status"] == "code_review"
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
            status in {"ready", "rework"}
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
              AND other.status IN ('claimed','investigating','implementing','waiting_confirmation','code_review','failed','blocked')
        )"""

    @staticmethod
    def _task_requires_project_exclusive_lock(task: dict[str, Any]) -> bool:
        contract = task.get("implementation_contract") or {}
        exclusive_names = {
            "package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock",
            "requirements.lock", "uv.lock", "pyproject.toml", "go.mod", "go.sum",
            "cargo.toml", "cargo.lock",
        }
        for target in contract.get("targets") or []:
            if not isinstance(target, dict):
                continue
            file = str(target.get("file") or "").strip().lower()
            mode = str(target.get("mode") or "modify").strip().lower()
            parts = {part for part in file.split("/") if part}
            if mode in {"config", "delete"} or file.rsplit("/", 1)[-1] in exclusive_names:
                return True
            if parts & {"migrations", "migration", "schema"}:
                return True
        return False

    def _active_project_runs(self, connection: Any, project: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT DISTINCT task.* FROM tasks task
               JOIN task_runs run ON run.task_id=task.id
               WHERE task.project=? AND run.status IN ('awaiting_thread','running')""",
            (project,),
        ).fetchall()
        return [decode_row(row) for row in rows]

    @staticmethod
    def _locking_project_tasks(connection: Any, project: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT * FROM tasks WHERE project=?
               AND status IN ('claimed','investigating','implementing','waiting_confirmation',
                              'code_review','rework','failed','blocked')""",
            (project,),
        ).fetchall()
        return [decode_row(row) for row in rows]

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
        targets = context.get("targets") or []
        steps = [
            task
            for target in targets if isinstance(target, dict)
            for task in target.get("tasks") or [] if isinstance(task, dict)
        ]
        checks = context.get("verify") or []
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
                    os.environ.get("DOTASKS_TARGET_SNIPPET_LINES", "60")
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
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800,
        *, action: str = "claim", requirement_id: str = "",
        decomposition_run_id: str = "", child_tasks: list[dict[str, Any]] | None = None,
        error: str = "",
    ) -> dict[str, Any] | None:
        """Claim requirement decomposition before ordinary tasks and finalize it idempotently."""
        action = str(action or "claim").strip()

        def decode_requirement(row: Any) -> dict[str, Any]:
            item = dict(row)
            for field in (
                "modules", "scope", "out_of_scope", "acceptance_criteria",
                "decomposition_plan",
            ):
                item[field] = json.loads(item.get(field) or "[]")
            item["auto_dispatch"] = bool(item.get("auto_dispatch"))
            return item

        if action == "get_requirement":
            with self.db.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM requirements WHERE id=?", (requirement_id,),
                ).fetchone()
                if not row:
                    raise KeyError(f"Requirement not found: {requirement_id}")
                tasks = connection.execute(
                    """SELECT * FROM tasks WHERE requirement_id=?
                       ORDER BY requirement_task_key, created_at, id""",
                    (requirement_id,),
                ).fetchall()
                decomposition_runs = [dict(item) for item in connection.execute(
                    """SELECT * FROM requirement_decomposition_runs
                       WHERE requirement_id=? ORDER BY attempt, created_at""",
                    (requirement_id,),
                ).fetchall()]
                task_ids = [row["id"] for row in tasks]
                relations = []
                if task_ids:
                    placeholders = ",".join("?" for _ in task_ids)
                    relations = [dict(item) for item in connection.execute(
                        f"""SELECT * FROM task_relations
                            WHERE source_task_id IN ({placeholders})
                              AND target_task_id IN ({placeholders})
                            ORDER BY created_at, id""",
                        (*task_ids, *task_ids),
                    ).fetchall()]
            return {
                "intake_kind": "requirement",
                "requirement": decode_requirement(row),
                "tasks": [decode_row(item) for item in tasks],
                "relations": relations,
                "decomposition_runs": decomposition_runs,
            }

        if action == "renew_decomposition":
            lease_seconds = max(300, min(int(lease_seconds), 7200))
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    """UPDATE requirement_decomposition_runs
                       SET lease_expires_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND requirement_id=? AND status='running'""",
                    (f"+{lease_seconds} seconds", decomposition_run_id, requirement_id),
                )
                if cursor.rowcount != 1:
                    raise ValueError("An active requirement decomposition run is required")
            return {"requirement_id": requirement_id, "run_id": decomposition_run_id, "status": "running"}

        if action in {"submit_decomposition", "fail_decomposition"}:
            with self.db.transaction() as connection:
                requirement_row = connection.execute(
                    "SELECT * FROM requirements WHERE id=?", (requirement_id,),
                ).fetchone()
                run = connection.execute(
                    """SELECT * FROM requirement_decomposition_runs
                       WHERE id=? AND requirement_id=?""",
                    (decomposition_run_id, requirement_id),
                ).fetchone()
                if not requirement_row or not run:
                    raise ValueError("A decomposition run belonging to the requirement is required")
                if run["status"] == "completed":
                    return self.claim_next_task(
                        "", action="get_requirement", requirement_id=requirement_id,
                    )
                elif run["status"] != "running" or requirement_row["status"] != "decomposing":
                    raise ValueError("An active requirement decomposition run is required")
                elif action == "fail_decomposition":
                    reason = str(error or "需求拆解失败").strip()
                    exhausted = (
                        int(run["attempt"] or 0)
                        >= REQUIREMENT_DECOMPOSITION_ATTEMPT_LIMIT
                    )
                    connection.execute(
                        """UPDATE requirement_decomposition_runs SET status='failed', error=?,
                           completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (reason, decomposition_run_id),
                    )
                    connection.execute(
                        """UPDATE requirements SET status=?, auto_dispatch=?,
                           last_decomposition_error=?,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (
                            "failed" if exhausted else "ready",
                            0 if exhausted else int(bool(requirement_row["auto_dispatch"])),
                            reason,
                            requirement_id,
                        ),
                    )
                    self._event(
                        connection,
                        "requirement",
                        requirement_id,
                        "decomposition_failed",
                        {
                            "run_id": decomposition_run_id,
                            "error": reason,
                            "attempt": int(run["attempt"] or 0),
                            "retry_exhausted": exhausted,
                        },
                    )
            if action == "fail_decomposition":
                return self.claim_next_task(
                    "", action="get_requirement", requirement_id=requirement_id,
                )

            specs = child_tasks
            if specs is None:
                specs = json.loads(requirement_row["decomposition_plan"] or "[]")
            if not isinstance(specs, list) or not specs or any(
                not isinstance(item, dict) for item in specs
            ):
                raise ValueError("child_tasks must be a non-empty array of objects")
            requirement_modules = json.loads(requirement_row["modules"] or "[]")
            requirement_out_of_scope = json.loads(requirement_row["out_of_scope"] or "[]")

            # Validate the entire decomposition before creating any child. A
            # requirement run is submitted once, so a contract error in a later
            # child must not leave earlier children partially persisted.
            preflight_keys: set[str] = set()
            for position, spec in enumerate(specs, 1):
                key = str(spec.get("key") or position).strip()
                if not key or key in preflight_keys:
                    raise ValueError("Every decomposed task requires a unique non-empty key")
                preflight_keys.add(key)
                if not str(spec.get("title") or "").strip() or not str(spec.get("goal") or "").strip():
                    raise ValueError("Every decomposed task requires title and goal")
                if not str(spec.get("analysis_id") or "").strip():
                    raise ValueError("Every decomposed task requires analysis_id")
                evidence = spec.get("location_evidence")
                if not isinstance(evidence, dict) or not evidence:
                    raise ValueError("Every decomposed task requires location_evidence")
                targets = spec.get("targets")
                if not isinstance(targets, list) or not targets:
                    raise ValueError("Every decomposed task requires non-empty targets")
                if any(
                    not isinstance(target, dict) or not target.get("tasks") for target in targets
                ):
                    raise ValueError("Every decomposed task target requires tasks")
                if not isinstance(spec.get("acceptance_plan"), list) or not spec["acceptance_plan"]:
                    raise ValueError("Every decomposed task requires non-empty acceptance_plan")
                if not isinstance(spec.get("review_checks"), list) or not spec["review_checks"]:
                    raise ValueError("Every decomposed task requires non-empty review_checks")
                if spec.get("quality_gates") is None:
                    raise ValueError("Every decomposed task requires quality_gates")
                self._normalize_quality_gates(spec["quality_gates"])
                project = self._require_project_directory(
                    str(spec.get("project") or requirement_row["project"] or "")
                )
                try:
                    self._validate_connected_location_evidence(project, evidence)
                except ValueError as exc:
                    raise ValueError(
                        f"Decomposed task {key} has invalid location_evidence: {exc}"
                    ) from exc
                dependencies = spec.get("depends_on") or []
                if not isinstance(dependencies, list):
                    raise ValueError("depends_on must be an array of task keys")

            for position, spec in enumerate(specs, 1):
                for dependency in spec.get("depends_on") or []:
                    if str(dependency).strip() not in preflight_keys:
                        raise ValueError(f"Unknown decomposed task dependency: {dependency}")

            created: dict[str, str] = {}
            desired_auto_dispatch: dict[str, bool] = {}
            seen_keys: set[str] = set()
            for position, spec in enumerate(specs, 1):
                key = str(spec.get("key") or position).strip()
                if not key or key in seen_keys:
                    raise ValueError("Every decomposed task requires a unique non-empty key")
                seen_keys.add(key)
                desired_auto_dispatch[key] = bool(spec.get("auto_dispatch", True))
                with self.db.connection() as connection:
                    existing = connection.execute(
                        "SELECT id FROM tasks WHERE requirement_id=? AND requirement_task_key=?",
                        (requirement_id, key),
                    ).fetchone()
                if existing:
                    created[key] = existing["id"]
                    continue
                title = str(spec.get("title") or "").strip()
                child_goal = str(spec.get("goal") or "").strip()
                if not title or not child_goal:
                    raise ValueError("Every decomposed task requires title and goal")
                targets = spec.get("targets")
                acceptance_plan = spec.get("acceptance_plan")
                review_checks = spec.get("review_checks")
                quality_gates = spec.get("quality_gates")
                if not str(spec.get("analysis_id") or "").strip():
                    raise ValueError("Every decomposed task requires analysis_id")
                if not isinstance(spec.get("location_evidence"), dict) or not spec["location_evidence"]:
                    raise ValueError("Every decomposed task requires location_evidence")
                if not isinstance(targets, list) or not targets:
                    raise ValueError("Every decomposed task requires non-empty targets")
                if any(
                    not isinstance(target, dict) or not target.get("tasks") for target in targets
                ):
                    raise ValueError("Every decomposed task target requires tasks")
                if not isinstance(acceptance_plan, list) or not acceptance_plan:
                    raise ValueError("Every decomposed task requires non-empty acceptance_plan")
                if not isinstance(review_checks, list) or not review_checks:
                    raise ValueError("Every decomposed task requires non-empty review_checks")
                if quality_gates is None:
                    raise ValueError("Every decomposed task requires quality_gates")
                self._normalize_quality_gates(quality_gates)
                modules = list(dict.fromkeys([*requirement_modules, *(spec.get("modules") or [])]))
                scope = list(dict.fromkeys(spec.get("scope") or [child_goal]))
                out_of_scope = list(dict.fromkeys([*requirement_out_of_scope, *(spec.get("out_of_scope") or [])]))
                normalized_plan = [dict(item) for item in acceptance_plan]
                result = self.finalize_task_intake({
                    "intake_kind": "task",
                    "analysis_id": spec["analysis_id"],
                    "title": title,
                    "project": str(spec.get("project") or requirement_row["project"] or ""),
                    "modules": modules,
                    # A decomposed task owns only its declared child outcome.
                    # Requirement-wide scope and acceptance stay on the parent;
                    # copying them here makes every sibling responsible for the
                    # entire requirement and creates duplicate acceptance bugs.
                    "goal": child_goal,
                    "scope": scope,
                    "out_of_scope": out_of_scope,
                    "priority": str(spec.get("priority") or requirement_row["priority"] or "P2"),
                    "source_thread_id": requirement_row["source_thread_id"],
                    "type": str(spec.get("type") or "feature"),
                    "token_budget": int(spec.get("token_budget") or self.task_token_budget()),
                    # Keep every child non-dispatchable until the whole dependency
                    # graph has passed validation and is committed below.
                    "auto_dispatch": False,
                    "location_summary": str(spec.get("location_summary") or "需求拆解子任务定位已确认"),
                    "agent_id": str(spec.get("agent_id") or "requirement-decomposer"),
                    "location_evidence": spec["location_evidence"],
                    "targets": targets,
                    "review_checks": review_checks,
                    "quality_gates": quality_gates,
                    "acceptance_plan": normalized_plan,
                    "dependency_analysis": {"decision": "independent"},
                })
                task_id = str(result.get("task_id") or "")
                if not task_id or result.get("task_status") != "ready":
                    raise ValueError("Decomposed task did not pass normal ready task intake")
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE tasks SET requirement_id=?, requirement_task_key=?,
                           updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (requirement_id, key, task_id),
                    )
                created[key] = task_id

            with self.db.transaction() as connection:
                current_run = connection.execute(
                    """SELECT status FROM requirement_decomposition_runs
                       WHERE id=? AND requirement_id=?""",
                    (decomposition_run_id, requirement_id),
                ).fetchone()
                if not current_run or current_run["status"] != "running":
                    raise ValueError("Requirement decomposition run is no longer active")
                for position, spec in enumerate(specs, 1):
                    source_id = created[str(spec.get("key") or position).strip()]
                    dependencies = spec.get("depends_on") or []
                    if not isinstance(dependencies, list):
                        raise ValueError("depends_on must be an array of task keys")
                    for dependency in dependencies:
                        dependency_id = created.get(str(dependency).strip())
                        if not dependency_id:
                            raise ValueError(f"Unknown decomposed task dependency: {dependency}")
                        self._insert_relation_in_connection(
                            connection, source_id, dependency_id, "depends_on",
                            f"需求 {requirement_id} 拆解依赖",
                        )
                for key, task_id in created.items():
                    connection.execute(
                        "UPDATE tasks SET auto_dispatch=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                        (int(desired_auto_dispatch[key]), task_id),
                    )
                connection.execute(
                    """UPDATE requirement_decomposition_runs SET status='completed',
                       completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (decomposition_run_id,),
                )
                connection.execute(
                    """UPDATE requirements SET status='decomposed', decomposition_plan=?,
                       last_decomposition_error='', decomposed_at=CURRENT_TIMESTAMP,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (json.dumps(specs, ensure_ascii=False), requirement_id),
                )
                self._event(connection, "requirement", requirement_id, "decomposed", {"run_id": decomposition_run_id, "task_ids": list(created.values())})
            return self.claim_next_task(
                "", action="get_requirement", requirement_id=requirement_id,
            )

        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if not self.dispatcher_enabled():
            return None
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        self.recover_expired_runs()
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE requirement_decomposition_runs SET status='failed',
                   error='decomposition lease expired', completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE status='running' AND lease_expires_at<=CURRENT_TIMESTAMP"""
            )
            connection.execute(
                """UPDATE requirements SET status='ready',
                   last_decomposition_error='decomposition lease expired',
                   updated_at=CURRENT_TIMESTAMP
                   WHERE status='decomposing' AND NOT EXISTS(
                     SELECT 1 FROM requirement_decomposition_runs r
                     WHERE r.requirement_id=requirements.id AND r.status='running')"""
            )
            requirement_filters = ["status='ready'", "auto_dispatch=1"]
            requirement_values: list[Any] = []
            if project:
                requirement_filters.append("project=?")
                requirement_values.append(self._normalize_project(project))
            requirement_row = connection.execute(
                f"""SELECT * FROM requirements WHERE {' AND '.join(requirement_filters)}
                    ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                             WHEN 'P2' THEN 2 ELSE 3 END, created_at, id LIMIT 1""",
                requirement_values,
            ).fetchone()
            if requirement_row:
                attempt = int(requirement_row["decomposition_attempts"] or 0) + 1
                run_id = self.db.next_id(connection, "RDRUN")
                lease_token = uuid.uuid4().hex
                modifier = f"+{lease_seconds} seconds"
                connection.execute(
                    """INSERT INTO requirement_decomposition_runs(
                           id, requirement_id, attempt, status, claimed_by,
                           lease_token, lease_expires_at
                       ) VALUES(?, ?, ?, 'running', ?, ?, datetime('now', ?))""",
                    (run_id, requirement_row["id"], attempt, worker_id, lease_token, modifier),
                )
                connection.execute(
                    """UPDATE requirements SET status='decomposing',
                       decomposition_attempts=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (attempt, requirement_row["id"]),
                )
                requirement = decode_requirement(requirement_row)
                requirement["status"] = "decomposing"
                requirement["decomposition_attempts"] = attempt
                return {
                    "kind": "requirement_decomposition",
                    "requirement": requirement,
                    "run": {"id": run_id, "run_type": "requirement_decomposition", "status": "running"},
                    "lease_token": lease_token,
                    "dispatch_prompt": (
                        "将该需求拆解为可独立执行且具有明确依赖关系的 ready 任务；"
                        "完成后调用 submit_requirement_decomposition。"
                    ),
                }
            # Parallel development is admitted only for isolated worktrees.
            # File locks stay conservative in the first release: two tasks that
            # touch the same file never run concurrently, even at different symbols.
            filters = [
                "status IN ('ready', 'rework')",
                "auto_dispatch=1",
                "(dispatch_retry_after IS NULL OR dispatch_retry_after<=CURRENT_TIMESTAMP)",
                self._dependencies_satisfied_sql(),
                self._conflicting_active_relation_sql(),
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
                    JOIN tasks locked_task ON locked_task.id=locked_target.task_id
                    WHERE candidate_target.task_id=tasks.id
                      AND locked_task.project=tasks.project
                      AND locked_task.status IN ('claimed','investigating','implementing','waiting_confirmation','code_review','failed','blocked')
                      AND NOT EXISTS (
                        SELECT 1 FROM execution_batch_tasks mine
                        JOIN execution_batch_tasks theirs ON theirs.batch_id=mine.batch_id
                        WHERE mine.task_id=tasks.id AND theirs.task_id=locked_task.id
                      )
                )""",
            ]
            values: list[Any] = []
            if project:
                filters.append("project = ?")
                values.append(self._normalize_project(project))
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE {" AND ".join(filters)}
                    ORDER BY retry_required DESC,
                             CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                             created_at ASC LIMIT 100""",
                values,
            ).fetchall()
            row = None
            execution_policy: dict[str, Any] = {}
            for candidate_row in rows:
                candidate = decode_row(candidate_row)
                candidate_project = str(candidate.get("project") or "")
                policy = self._project_execution_policy(candidate_project, connection)
                if (
                    policy["execution_environment"] == "worktree"
                    and self._task_has_unmanaged_workspace_conflict(candidate, policy)
                ):
                    continue
                active_tasks = self._active_project_runs(connection, candidate_project)
                locking_tasks = [
                    item for item in self._locking_project_tasks(connection, candidate_project)
                    if item.get("id") != candidate.get("id")
                ]
                if policy["capacity"] <= 1 and active_tasks:
                    continue
                active_development = sum(
                    1
                    for active in active_tasks
                    if active.get("status") in {"claimed", "investigating", "implementing", "rework"}
                )
                if active_development >= int(policy["capacity"]):
                    continue
                if locking_tasks and (
                    self._task_requires_project_exclusive_lock(candidate)
                    or any(self._task_requires_project_exclusive_lock(active) for active in locking_tasks)
                ):
                    continue
                row = candidate_row
                execution_policy = policy
                break
            if not row:
                return None
            task = decode_row(row)
            run_type = (
                "rework"
                if task["status"] == "rework" or task.get("retry_run_type") == "rework"
                else ("bugfix" if task.get("type") == "bug" else "execution")
            )
            if self._pause_for_stage_budget_preflight(connection, task, run_type):
                return None
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
            base_revision = str(
                (execution_policy.get("baseline") or {}).get("revision") or ""
            )
            base_ref = ""
            if execution_policy["execution_environment"] == "worktree":
                base_ref = self._create_run_base_ref(
                    str(task.get("project") or ""), run_id, base_revision
                )
            connection.execute(
                """INSERT INTO task_runs(
                       id, task_id, parent_run_id, run_type, attempt, status,
                       claimed_by, lease_token, lease_expires_at,
                       execution_environment, base_revision, base_ref
                   ) VALUES(?, ?, ?, ?, ?, 'awaiting_thread', ?, ?, datetime('now', ?), ?, ?, ?)""",
                (
                    run_id,
                    task["id"],
                    task.get("primary_run_id"),
                    run_type,
                    attempt,
                    worker_id,
                    lease_token,
                    modifier,
                    execution_policy["execution_environment"],
                    base_revision,
                    base_ref,
                ),
            )
            cursor = connection.execute(
                """UPDATE tasks SET status='claimed', assigned_to=?, active_run_id=?, retry_required=0,
                   primary_run_id=?, retry_run_type=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status=?""",
                (worker_id, run_id, run_id, task["id"], task["status"]),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task changed concurrently; retry dispatch")
            batch_id = self._ensure_execution_batch(connection, task, run_id, run_type)
            self._event(
                connection,
                "run",
                run_id,
                "claimed",
                {
                    "task_id": task["id"],
                    "worker_id": worker_id,
                    "task_status": "claimed",
                    "execution_environment": execution_policy["execution_environment"],
                    "parallel_fallback_reason": execution_policy["fallback_reason"],
                },
            )
        try:
            context = self.build_execution_context(task["id"], task.get("project"))
            if batch_id:
                context = self._merge_batch_execution_context(context, batch_id)
            context["workspace_baseline"] = retry_baseline or execution_policy["baseline"]
            context["execution_environment"] = execution_policy["execution_environment"]
            context["parallel_fallback_reason"] = execution_policy["fallback_reason"]
            context["base_revision"] = str(
                (execution_policy.get("baseline") or {}).get("revision") or ""
            )
            context["base_ref"] = base_ref
            context["retry_chain_root_run_id"] = retry_chain_root_run_id or run_id
            context["execution_profile"] = self._execution_profile(
                task, context, run_type
            )
            if context["execution_profile"]["risk"] == "low":
                snippet = self._target_snippet(
                    task.get("project"), context["targets"][0]
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
            task = decode_row(row)
            if self._pause_for_stage_budget_preflight(
                connection, task, "code_review"
            ):
                return None
            delivery = connection.execute(
                "SELECT * FROM task_runs WHERE id=? AND status='waiting_review'",
                (task["primary_run_id"],),
            ).fetchone()
            previous_review = connection.execute(
                """SELECT mapping.thread_id FROM task_run_conversations mapping
                   JOIN task_runs prior ON prior.id=mapping.run_id
                   WHERE prior.task_id=? AND prior.run_type='code_review'
                     AND mapping.thread_id IS NOT NULL AND trim(mapping.thread_id)!=''
                   ORDER BY prior.attempt, prior.created_at LIMIT 1""",
                (task["id"],),
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
            batch_row = connection.execute(
                """SELECT batch.* FROM execution_batches batch
                   JOIN execution_batch_runs mapping ON mapping.batch_id=batch.id
                   WHERE mapping.run_id=?""",
                (delivery["id"],),
            ).fetchone()
            batch = dict(batch_row) if batch_row else None
            if batch:
                self._attach_batch_run(connection, batch["id"], run_id, "review")
        delivery_snapshot = decode_row(delivery, RUN_JSON_FIELDS)
        context = self.build_code_review_context(task["id"], task.get("project"))
        review_delivery = self._model_delivery(delivery_snapshot, include_diff=False)
        review_delivery.pop("acceptance_evidence", None)
        context.update(
            {
                "delivery_run_id": delivery["id"],
                # The reviewer derives the patch from the trusted server
                # baseline and the locked changed files. Development output is
                # never used as the review diff source.
                "delivery": review_delivery,
                "diff_scope": {
                    "base_revision": str(
                        ((delivery_snapshot.get("artifact_snapshot") or {}).get("diff") or {}).get("base_revision")
                        or ((delivery_snapshot.get("context_snapshot") or {}).get("workspace_baseline") or {}).get("revision")
                        or ""
                    ),
                    "changed_files": [
                        str(item.get("file") or "")
                        for item in delivery_snapshot.get("changed_locations") or []
                        if str(item.get("file") or "").strip()
                    ],
                    "workspace_path": str(delivery_snapshot.get("workspace_path") or ""),
                    "artifact_path": str(delivery_snapshot.get("artifact_path") or ""),
                    "artifact_sha256": str(delivery_snapshot.get("artifact_sha256") or ""),
                },
            }
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
        confirmed_checks.extend(
            str(item).strip()
            for item in task.get("acceptance_criteria") or []
            if str(item).strip()
        )
        confirmed_checks = list(dict.fromkeys(confirmed_checks))
        if batch and int(batch.get("appended_count") or 0) > 0:
            context, confirmed_checks = self._merge_batch_code_review_context(
                context, batch["id"]
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
        failure_category: str | None = None,
        failure_locations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        batch = self._batch_for_run(run_id)
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
        def review_result_label(item: Any) -> str:
            if isinstance(item, dict):
                return str(
                    item.get("id")
                    or item.get("criterion")
                    or item.get("description")
                    or ""
                ).strip()
            return str(item or "").strip()

        passed_items = [
            review_result_label(item)
            for item in (passed_items or [])
            if review_result_label(item)
        ]
        failed_criteria = [
            review_result_label(item)
            for item in (failed_criteria or [])
            if review_result_label(item)
        ]
        contract = task.get("review_contract") or {}
        review_items = (
            self._batch_review_items(task_id)
            if batch and int(batch.get("appended_count") or 0) > 0
            else []
        )
        if not review_items:
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
            review_items.extend(
                str(item).strip()
                for item in task.get("acceptance_criteria") or []
                if str(item).strip()
            )
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
        if verdict == "pass":
            self._assert_required_acceptance_checks_passed(
                run_id, (run.get("context_snapshot") or {}).get("acceptance") or [],
            )
            delivery_run_id = str(run.get("delivery_run_id") or "")
            try:
                self._integrate_delivery_artifact(task, delivery_run_id)
            except Exception as exc:
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE task_runs SET integration_status='failed',
                           integration_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (str(exc)[:2000], delivery_run_id),
                    )
                    self._event(
                        connection,
                        "run",
                        delivery_run_id,
                        "delivery_integration_failed",
                        {"task_id": task_id, "error": str(exc)[:2000]},
                    )
                raise
        detected_category = "implementation"
        self_heal = self._self_heal_plan(task, "implementation", [])
        if verdict == "fail":
            detected_category = str(failure_category or "").strip().lower()
            if detected_category not in {"project", "environment", "implementation"}:
                detected_category = classify_recoverable_failure(
                    *reasons, *failed_criteria
                )
            if detected_category in {"project", "environment"} and not normalize_self_heal_locations(
                failure_locations
            ):
                raise ValueError(
                    "Project and environment failures require at least one exact failure_location file"
                )
            self_heal = self._self_heal_plan(
                task,
                detected_category,
                reasons,
                failure_locations,
            )
        completed_batch_task_ids: list[str] = []
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
                """UPDATE task_conversations SET status='completed',
                   updated_at=CURRENT_TIMESTAMP WHERE run_id=?""",
                (run_id,),
            )
            connection.execute(
                """UPDATE task_run_conversations SET status='completed',
                   updated_at=CURRENT_TIMESTAMP WHERE run_id=?""",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_runs SET status='review_failed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                (run.get("delivery_run_id"),),
            ) if verdict == "fail" else connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                (run.get("delivery_run_id"),),
            )
            next_status = (
                (
                    "rework"
                    if self_heal["category"] == "implementation"
                    or self_heal["scheduled"]
                    else "waiting_confirmation"
                )
                if verdict == "fail"
                else "done"
            )
            connection.execute(
                """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                   last_review_reasons=?, last_failed_criteria=?,
                   review_rework_count=review_rework_count+?, implementation_contract=?,
                   auto_dispatch=? WHERE id=?""",
                (
                    next_status,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(failed_criteria, ensure_ascii=False),
                    int(verdict == "fail"),
                    json.dumps(self_heal["contract"], ensure_ascii=False),
                    int(next_status != "waiting_confirmation"),
                    task_id,
                ),
            )
            if verdict == "fail" and self_heal["category"] in {
                "project", "environment",
            }:
                self._persist_self_heal_targets(
                    connection, task_id, self_heal["locations"]
                )
                self._event(
                    connection,
                    "task",
                    task_id,
                    "self_heal_scheduled" if self_heal["scheduled"] else "self_heal_exhausted",
                    {
                        "stage": "code_review",
                        "failure_category": self_heal["category"],
                        "attempt": self_heal["attempt"],
                        "attempt_limit": SELF_HEAL_ATTEMPT_LIMIT,
                        "locations": self_heal["locations"],
                    },
                )
            if batch:
                completed_batch_task_ids = self._propagate_batch_review(
                    connection,
                    batch,
                    task_id,
                    verdict,
                    reasons,
                    passed_items,
                    failed_criteria,
                )
            self._event(
                connection,
                "task",
                task_id,
                "code_reviewed",
                {
                    "verdict": verdict,
                    "reasons": reasons,
                    "next_stage": next_status,
                    "failure_category": detected_category if verdict == "fail" else "",
                    "self_heal_scheduled": bool(
                        verdict == "fail" and self_heal["scheduled"]
                    ),
                },
            )
            self._queue_obsidian_sync(connection, "task", task_id)
        updated = self.get_task(task_id)
        if updated["status"] == "done":
            self._create_experience(updated)
        for completed_task_id in completed_batch_task_ids:
            self._create_experience(self.get_task(completed_task_id))
        self.flush_integration_outbox()
        return updated

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
                "这是 Code Review 不通过后的原开发会话返工。Code Review 失败原因："
                + "；".join(review_reasons)
                + "。未通过的代码审查项："
                + "；".join(task.get("last_failed_criteria") or [])
                + "。只修复失败审查项及其直接相关问题；不要把已通过项当作返工要求。"
                "请像收到人工反馈一样在当前开发会话继续修复，并重新提交交付。"
            )
            self_heal = (task.get("implementation_contract") or {}).get("self_heal") or {}
            if self_heal.get("scheduled"):
                retry_note += (
                    f"这是第 {self_heal.get('attempt')}/{self_heal.get('attempt_limit')} 次自动自愈，"
                    f"错误分类为 {self_heal.get('category')}。"
                    "允许修复 RUN_CONTEXT_JSON.targets 中 mode=create/config 的测试或项目配置目标；"
                    "先修复根因并重跑失败检查，不能用跳过、降级或删除检查制造通过。"
                )
        if task.get("retry_required"):
            retry_note += (
                f"这是失败或中断后的显式重试；上次停止原因：{task.get('last_failure_reason') or '未记录'}。"
                "继续使用保存的重试链工作区基线，交付时必须报告失败前后累计产生的全部改动。"
            )
        batch = context.get("batch") or {}
        batch_note = (
            f"这是执行批次 {batch.get('id')} 的第 {batch.get('revision')} 版；"
            f"交付时 submit_task_delivery 必须传 batch_revision={batch.get('revision')}，"
            "并完整覆盖 RUN_CONTEXT_JSON.tasks 中的全部任务。"
            if int(batch.get("appended_count") or 0) > 0
            else ""
        )
        return (
            "$dotasks-lifecycle\n\n"
            f"任务 {task['id']}（运行 {run_id}，类型 {run_type}）。"
            f"{retry_note}{batch_note}"
            "以 RUN_CONTEXT_JSON 为唯一任务输入，只读取已定位目标及正确性所需的直接依赖，不再获取任务详情。"
            "实现并完成开发阶段的自动化验证后调用 submit_task_delivery。acceptance_evidence 每项必须包含 "
            "criterion、status（passed/failed/blocked/pending）和 evidence；自动检查必须真实通过后才能标记 passed，"
            "static_review/manual_runtime 只能标记 pending 或 blocked，留给 Code Review 阶段执行。"
            "需要用户决策或无法继续时调用 report_run_blocked。"
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
            "$dotasks-lifecycle\n\n"
            "Code Review 阶段只使用提示内的 RUN_CONTEXT_JSON，不要搜索工具目录、数据库或任务详情。"
            "先使用 RUN_CONTEXT_JSON.diff_scope.base_revision 和 changed_files 在项目中自行执行 "
            "git diff（必要时分别执行 git diff <base> -- <files> 与 git diff -- <files>），"
            "再仅根据任务目标、约束、review_checks 和该实际 diff 检查正确性、安全、权限边界、回归与无关修改，不修改代码。"
            "不得要求开发阶段传入或复述 diff，也不得把 delivery.acceptance_evidence 当作代码正确性的证明；"
            "确定性自动检查由服务端工具执行；需要时先调用 run_acceptance_checks，然后仅对实际 diff 作 Code Review，最后调用 review_code。"
            "失败时区分 implementation、project、environment；缺测试用例、测试脚本或项目配置属于 project，"
            "依赖/模块缺失或工具环境异常属于 environment。可恢复错误必须在 review_code 中提交 failure_category，"
            "并用 failure_locations 精确列出需新增或修改的测试/配置文件，以便服务自动安排受控返工；不得直接阻塞整个项目。"
            f"任务 {task['id']}（运行 {run_id}）。passed_items 与 failed_criteria 必须且只能完整划分"
            f"这些 Code Review 检查项（已包含验收标准）：{json.dumps(confirmed_checks, ensure_ascii=False)}。"
            f"\n\nRUN_CONTEXT_JSON={prompt_context(context)}"
        )
