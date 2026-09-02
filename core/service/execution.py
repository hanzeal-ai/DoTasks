from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
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
        failure_category: str | None = None,
        failure_locations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Expose exceptional exits and schedule one safe, bounded repair when possible."""
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
        category = str(failure_category or "").strip().lower()
        if category and category not in {"project", "environment", "implementation"}:
            raise ValueError(
                "failure_category must be project, environment or implementation"
            )
        if status == "blocked":
            category = category or classify_recoverable_failure(reason)
            locations = normalize_self_heal_locations(failure_locations)
            if category in {"project", "environment"} and locations:
                self_heal = self._self_heal_plan(
                    task, category, [reason], locations,
                )
                # An execution worker gets one bounded repair for a concrete,
                # safely located project/environment problem. Further reports
                # enter attention instead of creating an automatic loop.
                scheduled = int(self_heal["attempt"]) == 1
                healing_state = self_heal["contract"].get("self_heal") or {}
                healing_state["attempt_limit"] = 1
                healing_state["scheduled"] = scheduled
                self_heal["contract"]["self_heal"] = healing_state
                if scheduled:
                    with self.db.transaction() as connection:
                        self._interrupt_active_run(connection, run_id)
                        connection.execute(
                            """UPDATE tasks SET status='rework', active_run_id=NULL,
                               assigned_to=NULL, retry_required=1, retry_run_type=?,
                               auto_dispatch=1, implementation_contract=?,
                               last_failure_reason=?, last_failure_at=CURRENT_TIMESTAMP,
                               updated_at=CURRENT_TIMESTAMP WHERE id=? AND active_run_id=?""",
                            (
                                run["run_type"],
                                json.dumps(self_heal["contract"], ensure_ascii=False),
                                reason,
                                task_id,
                                run_id,
                            ),
                        )
                        self._persist_self_heal_targets(
                            connection, task_id, self_heal["locations"],
                        )
                        self._event(
                            connection,
                            "task",
                            task_id,
                            "self_heal_scheduled",
                            {
                                "stage": "execution",
                                "failure_category": category,
                                "attempt": 1,
                                "attempt_limit": 1,
                                "locations": self_heal["locations"],
                            },
                        )
                        self._queue_obsidian_sync(connection, "task", task_id)
                    self.flush_integration_outbox()
                    return self.get_task(task_id)
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
            changes["execution_recovery_count"] = 0
            changes["last_recovery_reason"] = ""
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
    def _dispatch_blocker(code: str, message: str, **details: Any) -> dict[str, Any]:
        return {"code": code, "message": message, **details}

    def _development_dispatch_blockers(
        self,
        connection: Any,
        task: dict[str, Any],
        *,
        policy: dict[str, Any] | None = None,
        policy_cache: dict[str, dict[str, Any]] | None = None,
        include_controller_state: bool = False,
        scheduler_state: dict[str, Any] | None = None,
        dispatcher_enabled: bool | None = None,
    ) -> list[dict[str, Any]]:
        """Explain the same gates used to admit a development dispatch."""
        if task.get("status") not in {"ready", "rework"}:
            return []
        enabled = self.dispatcher_enabled() if dispatcher_enabled is None else dispatcher_enabled
        if not enabled:
            return [self._dispatch_blocker("dispatcher_paused", "全局调度已暂停")]
        if not bool(task.get("auto_dispatch")):
            return [self._dispatch_blocker("task_auto_dispatch_paused", "任务自动领取已暂停")]

        retry_after = str(task.get("dispatch_retry_after") or "").strip()
        if retry_after:
            waiting = connection.execute(
                "SELECT datetime(?) > CURRENT_TIMESTAMP", (retry_after,)
            ).fetchone()[0]
            if waiting:
                return [self._dispatch_blocker(
                    "dispatch_retry_backoff",
                    f"调度重试将在 {retry_after} 后恢复",
                    retry_after=retry_after,
                )]

        blockers: list[dict[str, Any]] = []
        dependency_rows = connection.execute(
            """SELECT DISTINCT dependency.id, dependency.title, dependency.status
                 FROM task_relations relation
                 JOIN tasks dependency ON dependency.id=CASE
                   WHEN relation.relation_type IN ('depends_on','continues_from')
                     THEN relation.target_task_id ELSE relation.source_task_id END
                WHERE (((relation.relation_type IN ('depends_on','continues_from'))
                          AND relation.source_task_id=?)
                    OR (relation.relation_type='blocks' AND relation.target_task_id=?))
                  AND dependency.status!='done'
                ORDER BY dependency.id""",
            (task["id"], task["id"]),
        ).fetchall()
        for row in dependency_rows:
            blockers.append(self._dispatch_blocker(
                "dependency_not_done",
                f"等待依赖任务 {row['id']} 完成",
                task_id=row["id"], title=row["title"], status=row["status"],
            ))

        relation_rows = connection.execute(
            """SELECT DISTINCT other.id, other.title, other.status
                 FROM task_relations relation
                 JOIN tasks other ON other.id=CASE
                   WHEN relation.source_task_id=? THEN relation.target_task_id
                   ELSE relation.source_task_id END
                WHERE relation.relation_type='conflicts_with'
                  AND (relation.source_task_id=? OR relation.target_task_id=?)
                  AND other.status IN (
                    'claimed','investigating','implementing','waiting_confirmation',
                    'code_review','failed','blocked'
                  )
                ORDER BY other.id""",
            (task["id"], task["id"], task["id"]),
        ).fetchall()
        for row in relation_rows:
            blockers.append(self._dispatch_blocker(
                "relation_conflict",
                f"等待冲突任务 {row['id']} 释放",
                task_id=row["id"], title=row["title"], status=row["status"],
            ))

        active_run = connection.execute(
            """SELECT id FROM task_runs WHERE task_id=?
                 AND run_type IN ('execution','rework')
                 AND status IN ('awaiting_thread','running') LIMIT 1""",
            (task["id"],),
        ).fetchone()
        if active_run:
            blockers.append(self._dispatch_blocker(
                "active_run_exists", "任务已有活动执行", run_id=active_run["id"]
            ))

        target_rows = connection.execute(
            """SELECT DISTINCT locked_task.id, locked_task.title, locked_task.status,
                               candidate_target.file
                 FROM task_targets candidate_target
                 JOIN task_targets locked_target
                   ON locked_target.task_id != candidate_target.task_id
                  AND locked_target.file=candidate_target.file
                 JOIN tasks locked_task ON locked_task.id=locked_target.task_id
                WHERE candidate_target.task_id=?
                  AND locked_task.project=?
                  AND locked_task.status IN (
                    'claimed','investigating','implementing','waiting_confirmation',
                    'code_review','failed','blocked'
                  )
                  AND NOT EXISTS (
                    SELECT 1 FROM execution_batch_tasks mine
                    JOIN execution_batch_tasks theirs ON theirs.batch_id=mine.batch_id
                    WHERE mine.task_id=? AND theirs.task_id=locked_task.id
                  )
                ORDER BY locked_task.id, candidate_target.file""",
            (task["id"], task.get("project"), task["id"]),
        ).fetchall()
        grouped_targets: dict[str, dict[str, Any]] = {}
        for row in target_rows:
            grouped = grouped_targets.setdefault(row["id"], {
                "title": row["title"], "status": row["status"], "files": [],
            })
            if row["file"] not in grouped["files"]:
                grouped["files"].append(row["file"])
        for task_id, item in grouped_targets.items():
            blockers.append(self._dispatch_blocker(
                "target_locked",
                f"目标文件被任务 {task_id} 占用：{'、'.join(item['files'])}",
                task_id=task_id, title=item["title"], status=item["status"],
                files=item["files"],
            ))
        if blockers:
            return blockers

        project = str(task.get("project") or "")
        if policy is None:
            if policy_cache is not None and project in policy_cache:
                policy = policy_cache[project]
            else:
                policy = self._project_execution_policy(
                    project,
                    connection,
                    initialize_integration=not include_controller_state,
                )
                if policy_cache is not None:
                    policy_cache[project] = policy

        if policy["execution_environment"] == "worktree":
            dirty_targets = self._task_unmanaged_workspace_conflicts(task, policy)
            if dirty_targets:
                blockers.append(self._dispatch_blocker(
                    "workspace_target_dirty",
                    f"目标文件存在未提交改动：{'、'.join(dirty_targets)}",
                    files=dirty_targets,
                ))

        active_tasks = self._active_project_runs(connection, project)
        locking_tasks = [
            item for item in self._locking_project_tasks(connection, project)
            if item.get("id") != task.get("id")
        ]
        if int(policy["capacity"]) <= 1 and active_tasks:
            blockers.append(self._dispatch_blocker(
                "development_capacity",
                "项目已有活动执行，当前开发容量为 1",
                capacity=1,
                task_ids=[item["id"] for item in active_tasks],
            ))
        else:
            active_development = sum(
                1 for active in active_tasks
                if active.get("status") in {"claimed", "investigating", "implementing", "rework"}
            )
            if active_development >= int(policy["capacity"]):
                blockers.append(self._dispatch_blocker(
                    "development_capacity",
                    f"项目开发槽位已满（{active_development}/{int(policy['capacity'])}）",
                    capacity=int(policy["capacity"]), active=active_development,
                    task_ids=[item["id"] for item in active_tasks],
                ))

        if locking_tasks and (
            self._task_requires_project_exclusive_lock(task)
            or any(self._task_requires_project_exclusive_lock(active) for active in locking_tasks)
        ):
            task_ids = [item["id"] for item in locking_tasks]
            blockers.append(self._dispatch_blocker(
                "project_exclusive_lock",
                f"等待同项目排他任务释放：{'、'.join(task_ids)}",
                task_ids=task_ids,
            ))
        if blockers or not include_controller_state:
            return blockers

        state = scheduler_state
        if state is None:
            row = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            state = dict(row) if row else {}
        if bool(state.get("pending")):
            lease_owner = str(state.get("lease_owner") or "")
            lease_expires_at = str(state.get("lease_expires_at") or "")
            lease_active = bool(lease_owner and lease_expires_at and connection.execute(
                "SELECT datetime(?) > CURRENT_TIMESTAMP", (lease_expires_at,)
            ).fetchone()[0])
            if lease_active:
                return [self._dispatch_blocker(
                    "controller_processing",
                    f"Controller 正在处理调度（{lease_owner}）",
                    worker_id=lease_owner,
                )]
            return [self._dispatch_blocker(
                "controller_pending", "调度已唤醒，等待 Controller 创建 Codex 任务"
            )]
        return [self._dispatch_blocker(
            "scheduler_wakeup_missing", "任务可执行，但调度唤醒未登记"
        )]

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

    def claim_next_task(
        self, worker_id: str, project: str | None = None, lease_seconds: int = 1800,
    ) -> dict[str, Any] | None:
        """Claim requirement decomposition before an ordinary implementation task."""
        worker_id = worker_id.strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        if not self.dispatcher_enabled():
            return None
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        self.recover_expired_runs()
        requirement_claim = self._claim_requirement_decomposition(
            worker_id, project, lease_seconds,
        )
        if requirement_claim:
            return requirement_claim
        with self.db.transaction() as connection:
            # Parallel development is admitted only for isolated worktrees.
            # File locks stay conservative in the first release: two tasks that
            # touch the same file never run concurrently, even at different symbols.
            filters = ["status IN ('ready', 'rework')"]
            values: list[Any] = []
            if project:
                filters.append("project = ?")
                values.append(self._normalize_project(project))
            rows = connection.execute(
                f"""SELECT * FROM tasks WHERE {" AND ".join(filters)}
                    ORDER BY CASE status WHEN 'rework' THEN 0 ELSE 1 END,
                             retry_required DESC,
                             CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 ELSE 3 END,
                             created_at ASC""",
                values,
            ).fetchall()
            row = None
            execution_policy: dict[str, Any] = {}
            policy_cache: dict[str, dict[str, Any]] = {}
            for candidate_row in rows:
                candidate = decode_row(candidate_row)
                candidate_project = str(candidate.get("project") or "")
                policy = policy_cache.get(candidate_project)
                if policy is None:
                    policy = self._project_execution_policy(candidate_project, connection)
                    policy_cache[candidate_project] = policy
                if self._development_dispatch_blockers(
                    connection,
                    candidate,
                    policy=policy,
                    dispatcher_enabled=True,
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
                   ORDER BY prior.attempt DESC, prior.created_at DESC LIMIT 1""",
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
        context.update(
            {
                "delivery_run_id": delivery["id"],
                # The reviewer derives the patch from the trusted server
                # baseline and the locked changed files. Development output is
                # never used as the review diff source.
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

    @staticmethod
    def _task_prompt_brief(
        task: dict[str, Any],
        heading: str,
        batch_tasks: list[dict[str, Any]] | None = None,
    ) -> str:
        lines = [heading, "", f"标题：{task.get('title') or task.get('id')}"]
        if task.get("goal"):
            lines.append(f"目标：{task['goal']}")
        for label, values in (
            ("范围", task.get("scope") or []),
            ("不包含", task.get("out_of_scope") or []),
            ("验收标准", task.get("acceptance_criteria") or []),
        ):
            normalized = [str(value).strip() for value in values if str(value).strip()]
            if normalized:
                lines.extend(["", f"{label}：", *[f"- {value}" for value in normalized]])
        visible_batch_tasks = [
            item for item in (batch_tasks or [])
            if isinstance(item, dict) and item.get("id") != task.get("id")
        ]
        if visible_batch_tasks:
            lines.extend(["", "同批任务："])
            for item in visible_batch_tasks:
                summary = f"- {item.get('id')}《{item.get('title') or ''}》"
                lines.append(summary)
                if item.get("goal"):
                    lines.append(f"  目标：{item['goal']}")
                for label, values in (
                    ("范围", item.get("scope") or []),
                    ("不包含", item.get("out_of_scope") or []),
                    ("验收标准", item.get("acceptance_criteria") or []),
                ):
                    normalized = [
                        str(value).strip()
                        for value in values
                        if str(value).strip()
                    ]
                    if normalized:
                        lines.append(f"  {label}：")
                        lines.extend(f"  - {value}" for value in normalized)
        return "\n".join(lines)

    @classmethod
    def _dispatch_prompt(
        cls,
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
        task_brief = cls._task_prompt_brief(
            task, "请完成以下任务：", context.get("tasks") or []
        )
        retry_section = f"返工或重试要求：{retry_note}\n\n" if retry_note else ""
        return (
            f"{task_brief}\n\n---\n\n"
            "$dotasks-lifecycle\n\n"
            "运行信息：\n"
            f"- 任务 ID：{task['id']}\n"
            f"- 运行 ID：{run_id}\n"
            f"- 类型：{run_type}\n\n"
            f"{retry_section}"
            "修改目标与验证要求：\n"
            f"RUN_CONTEXT_JSON={prompt_context(context)}\n\n"
            "完成后上报：\n"
            "- 完成：调用 submit_task_delivery，上报实际修改文件、验证结果和逐条验收证据，由 DoTasks 推进任务状态。\n"
            "- 无法继续：调用 report_run_blocked，上报 waiting_confirmation 或 blocked 及具体原因。"
        )

    @classmethod
    def _code_review_prompt(
        cls,
        task: dict[str, Any],
        run_id: str,
        confirmed_checks: list[str],
        context: dict[str, Any],
    ) -> str:
        task_brief = cls._task_prompt_brief(
            {"id": task.get("id"), "title": task.get("title")},
            "请审查以下代码变更：",
        )
        return (
            f"{task_brief}\n\n---\n\n"
            "$dotasks-lifecycle\n\n"
            "Code Review 阶段只使用提示内的 RUN_CONTEXT_JSON，不要搜索工具目录、数据库或任务详情。\n"
            "Diff 必须且只能通过现成 Git 命令获取：以 diff_scope.workspace_path 为工作区，先执行 "
            "git -C <workspace_path> status --short -- <changed_files>，再执行 "
            "git -C <workspace_path> diff --no-ext-diff --no-textconv <base_revision> -- <changed_files>；"
            "使用 git -C <workspace_path> ls-files --others --exclude-standard -- <changed_files> 识别未跟踪文件，"
            "并用 git -C <workspace_path> diff --no-index --no-ext-diff --no-textconv -- /dev/null <file> 查看新增文件，"
            "该命令返回 1 表示发现差异，不视为执行失败。"
            "不得读取交付补丁文件、拼接文件内容、自行实现 Diff，或要求开发阶段传入和复述 Diff。\n"
            "取得实际 Diff 后，必须调用当前环境已提供、项目已配置且与审查项相关的所有现成工具，"
            "包括源码导航、lint、类型检查、静态分析、安全扫描和聚焦测试；"
            "不得安装新工具、编写临时脚本或自制扫描器，也不得修改项目源码、配置和依赖。"
            "允许只读查看变更符号的直接依赖、调用方和相关项目配置，以判断局部正确性、安全性和耦合关系。\n"
            "仅根据 review_checks 检查代码质量、安全漏洞以及高内聚低耦合。"
            "不得检查任务目标或 acceptance_criteria 是否完成，不得运行验收计划；这些由现有人工验收负责。"
            "不得仅因任务目标看起来未完成而判定失败；但实际 Diff 中可复现的缺陷、回归风险、安全漏洞、"
            "明显维护性问题、职责混杂或不合理依赖仍必须判定对应质量项失败。"
            "只有具体、可执行且足以影响正确性、安全性或长期维护的发现才阻断；"
            "纯格式、命名偏好或非阻断建议不得放入 failed_criteria。\n"
            "失败原因必须指出准确文件及符号或行号、触发条件、影响和最小修复方向；"
            "完成后直接调用 review_code。\n"
            f"任务 {task['id']}（运行 {run_id}）。passed_items 与 failed_criteria 必须且只能完整划分"
            f"这些 Code Review 质量检查项：{json.dumps(confirmed_checks, ensure_ascii=False)}。"
            f"\n\nRUN_CONTEXT_JSON={prompt_context(context)}"
        )
