from __future__ import annotations

from typing import Any
from .domain import decode_row


class TaskSchedulingMixin:
    """Durable, coalesced scheduling wakeups and one leased controller cycle."""

    def scheduler_snapshot(self) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
        if not row:
            raise RuntimeError("scheduler_state is not initialized")
        item = dict(row)
        item["pending"] = bool(item.get("pending"))
        item["dispatcher_enabled"] = self.dispatcher_enabled()
        return item

    @staticmethod
    def _request_schedule(
        connection: Any,
        trigger: str,
        entity_type: str = "",
        entity_id: str = "",
    ) -> None:
        """Coalesce a queue-relevant state change inside its owning transaction."""
        connection.execute(
            """UPDATE scheduler_state
               SET generation=generation+1, pending=1, last_trigger=?,
                   last_entity_type=?, last_entity_id=?, updated_at=CURRENT_TIMESTAMP
               WHERE id=1""",
            (
                str(trigger or "state_changed")[:200],
                str(entity_type or "")[:40],
                str(entity_id or "")[:100],
            ),
        )

    def claim_schedule_cycle(
        self,
        worker_id: str,
        project: str | None = None,
        lease_seconds: int = 1800,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        """Lease one global cycle, then recover Review and fill development slots."""
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        if not self.dispatcher_enabled():
            return {
                "status": "paused",
                "worker_id": worker_id,
                "scheduler": self.scheduler_snapshot(),
                "code_review": {"stage": "code_review", "capacity": 1, "dispatches": []},
                "development": {"stage": "development", "capacity": 0, "dispatches": []},
            }

        with self.db.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            if not state:
                raise RuntimeError("scheduler_state is not initialized")
            if not force and not bool(state["pending"]):
                return {
                    "status": "idle",
                    "worker_id": worker_id,
                    "scheduler": {**dict(state), "pending": False, "dispatcher_enabled": True},
                    "code_review": {"stage": "code_review", "capacity": 1, "dispatches": []},
                    "development": {"stage": "development", "capacity": 0, "dispatches": []},
                }
            leased = connection.execute(
                """UPDATE scheduler_state
                   SET lease_owner=?, lease_expires_at=datetime('now', ?),
                       updated_at=CURRENT_TIMESTAMP
                   WHERE id=1 AND (
                     lease_owner='' OR lease_expires_at IS NULL
                     OR lease_expires_at<=CURRENT_TIMESTAMP
                   )""",
                (worker_id, f"+{lease_seconds} seconds"),
            )
            if leased.rowcount != 1:
                return {
                    "status": "busy",
                    "worker_id": worker_id,
                    "scheduler": {**dict(state), "pending": bool(state["pending"]), "dispatcher_enabled": True},
                    "code_review": {"stage": "code_review", "capacity": 1, "dispatches": []},
                    "development": {"stage": "development", "capacity": 0, "dispatches": []},
                }

        try:
            # Review is an independent one-slot lane. Development/rework uses
            # the configured capacity and the existing atomic eligibility gates.
            code_review = self._claim_native_dispatch_batch(
                worker_id, project, lease_seconds, "code_review"
            )
            development = self._claim_native_dispatch_batch(
                worker_id, project, lease_seconds, "development"
            )
        except Exception:
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE scheduler_state SET lease_owner='', lease_expires_at=NULL,
                       updated_at=CURRENT_TIMESTAMP WHERE id=1 AND lease_owner=?""",
                    (worker_id,),
                )
            raise

        snapshot = self.scheduler_snapshot()
        return {
            "status": "claimed",
            "worker_id": worker_id,
            "cycle_generation": int(state["generation"]),
            "scheduler": snapshot,
            "code_review": code_review,
            "development": development,
        }

    def complete_schedule_cycle(
        self, worker_id: str, generation: int
    ) -> dict[str, Any]:
        worker_id = str(worker_id or "").strip()
        if not worker_id:
            raise ValueError("worker_id is required")
        generation = max(0, int(generation))
        with self.db.transaction() as connection:
            state = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            if not state:
                raise RuntimeError("scheduler_state is not initialized")
            if state["lease_owner"] != worker_id:
                raise ValueError("Schedule cycle is leased by another worker")
            if generation > int(state["generation"]):
                raise ValueError("Schedule cycle generation is invalid")
            handled = max(int(state["handled_generation"]), generation)
            unresolved_native = connection.execute(
                "SELECT 1 FROM native_dispatches WHERE status='pending_thread' LIMIT 1"
            ).fetchone()
            pending = int(
                int(state["generation"]) > handled or unresolved_native is not None
            )
            connection.execute(
                """UPDATE scheduler_state SET handled_generation=?, pending=?,
                   lease_owner='', lease_expires_at=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=1""",
                (handled, pending),
            )
        return self.scheduler_snapshot()

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
               WHERE COALESCE(task.project, '')=?
                 AND run.status IN ('awaiting_thread','running')""",
            (project,),
        ).fetchall()
        return [decode_row(row) for row in rows]

    @staticmethod
    def _locking_project_tasks(connection: Any, project: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT * FROM tasks WHERE COALESCE(project, '')=?
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
        if self._mobile_task_locked(connection, task['id']):
            return [self._dispatch_blocker('mobile_conversation_active', '手机对话正在执行或等待核对')]

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
