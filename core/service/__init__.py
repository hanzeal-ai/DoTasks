from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

from ..db import Database
from taskboard.location import LocationAdapter
from taskboard.obsidian import ObsidianAdapter
from taskboard.decision_client import DecisionClient
from taskboard.project_guard import ProjectWorkspaceGuard
from .execution import TaskLifecycleMixin
from .batching import TaskBatchMixin
from .changes import TaskChangeMixin
from .planning import TaskPlanningMixin
from .queries import TaskQueryMixin
from .requirements import TaskRequirementMixin
from .reporting import TaskReportingMixin
from .review import TaskReviewMixin
from .runs import TaskRunMixin
from .native_dispatch import NativeDispatchMixin
from .scheduling import TaskSchedulingMixin
from .integration import TaskIntegrationMixin
from .settings import TaskSettingsMixin
from .visuals import TaskVisualMixin
from .mobile import MobileConversationMixin


class TaskboardService(
    MobileConversationMixin,
    TaskSettingsMixin,
    TaskVisualMixin,
    TaskPlanningMixin,
    TaskChangeMixin,
    TaskQueryMixin,
    TaskBatchMixin,
    TaskIntegrationMixin,
    TaskRequirementMixin,
    TaskLifecycleMixin,
    TaskRunMixin,
    NativeDispatchMixin,
    TaskSchedulingMixin,
    TaskReviewMixin,
    TaskReportingMixin,
):
    """Core facade composed from cohesive task lifecycle domains."""

    def __init__(
        self,
        home: str | Path | None = None,
        workspace_guard: ProjectWorkspaceGuard | None = None,
    ):
        package_home = Path(__file__).resolve().parents[2]
        self.package_home = package_home
        self.home = Path(home or package_home).resolve()
        if home is not None:
            data_home = self.home
        else:
            configured_home = os.environ.get("DOTASKS_HOME")
            application_home = Path.home() / "Library" / "Application Support" / "DoTasks"
            data_home = Path(configured_home).expanduser() if configured_home else application_home
        self.data_home = data_home.resolve()
        self.db = Database(self.data_home / "data" / "taskboard.db")
        self.decisions = DecisionClient(self.data_home)
        self.obsidian = ObsidianAdapter(self.data_home, decision_client=self.decisions)
        self.location = LocationAdapter()
        self.workspace_guard = workspace_guard or ProjectWorkspaceGuard()

    def _execution_admission(self, task_id: str | None, stage: str, run_id: str | None = None) -> None:
        """Personal execution needs no team approval; team runtimes override this gate."""
        return None

    def _normalize_project(self, project: str | Path | None) -> str:
        return self.workspace_guard.normalize_project(project)

    def _require_project_directory(self, project: str | Path | None) -> str:
        return self.workspace_guard.require_project_directory(project)

    def _normalize_target_file(self, value: Any) -> str:
        return self.workspace_guard.normalize_target_file(value)

    @staticmethod
    def _string_list(payload: dict[str, Any], field: str) -> list[str]:
        value = payload.get(field, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ValueError(f"{field} must be an array of strings")
        return [item.strip() for item in value if item.strip()]

    def _git(self, project: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return self.workspace_guard.git(project, *arguments)

    def _workspace_state(self, project: str | None) -> dict[str, Any]:
        return self.workspace_guard.workspace_state(project)

    def _workspace_diff(
        self, project: str | None, baseline_revision: str, paths: list[str],
    ) -> dict[str, Any]:
        return self.workspace_guard.workspace_diff(project, baseline_revision, paths)

    def dispatcher_enabled(self) -> bool:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key='dispatcher_enabled'"
            ).fetchone()
        return bool(row and row["value"] == "1")

    def set_dispatcher_enabled(self, enabled: bool) -> dict[str, Any]:
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES('dispatcher_enabled', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                ("1" if enabled else "0",),
            )
            self._event(connection, "system", "dispatcher", "dispatcher_state_changed", {"enabled": enabled})
            if enabled:
                self._request_schedule(
                    connection, "dispatcher_resumed", "system", "dispatcher"
                )
        return {"enabled": enabled}

    def pause_dispatcher(self) -> dict[str, Any]:
        """Stop new dispatch claims without changing task or active-run state."""
        self.set_dispatcher_enabled(False)
        return {"dispatcher_enabled": False}

    def resume_all_tasks(self) -> dict[str, Any]:
        with self.db.transaction() as connection:
            tasks = connection.execute("SELECT id, paused_from_status, retry_run_type FROM tasks WHERE status='paused'").fetchall()
            for task in tasks:
                previous = task["paused_from_status"] or "ready"
                target = self._resume_target(previous, task["retry_run_type"])
                retry_required = int(target in {"ready", "rework"} and bool(task["retry_run_type"]))
                connection.execute(
                    """UPDATE tasks SET status=?, paused_from_status=NULL, retry_required=?, retry_run_type=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        target, retry_required, task["retry_run_type"] if retry_required else None,
                        task["id"],
                    ),
                )
                self._event(connection, "task", task["id"], "resumed", {"to": target})
                self._queue_obsidian_sync(connection, "task", task["id"])
        self.set_dispatcher_enabled(True)
        self.flush_integration_outbox()
        return {
            "dispatcher_enabled": True,
            "resumed_tasks": len(tasks),
        }

    def resume_task(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "paused":
            raise ValueError("Only a paused task can be resumed")
        previous = task.get("paused_from_status") or "ready"
        target = self._resume_target(previous, task.get("retry_run_type"))
        retry_required = int(target in {"ready", "rework"} and bool(task.get("retry_run_type")))
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE tasks SET status=?, paused_from_status=NULL, retry_required=?, retry_run_type=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='paused'""",
                (
                    target, retry_required, task.get("retry_run_type") if retry_required else None,
                    task_id,
                ),
            )
            self._event(connection, "task", task_id, "resumed", {"to": target})
            self._queue_obsidian_sync(connection, "task", task_id)
        self.flush_integration_outbox()
        return self.get_task(task_id)

    @staticmethod
    def _resume_target(previous: str, retry_run_type: str | None) -> str:
        if previous == "draft":
            return "draft"
        if previous == "code_review":
            return previous
        if previous in {"waiting_confirmation", "blocked", "failed"}:
            return previous
        if previous == "rework" or retry_run_type == "rework":
            return "rework"
        return "ready"

    def _event(self, connection: Any, entity_type: str, entity_id: str, event_type: str, payload: dict[str, Any]) -> None:
        connection.execute(
            "INSERT INTO events(entity_type, entity_id, event_type, payload) VALUES(?, ?, ?, ?)",
            (entity_type, entity_id, event_type, json.dumps(payload, ensure_ascii=False)),
        )

    @staticmethod
    def _queue_obsidian_sync(connection: Any, entity_type: str, entity_id: str) -> None:
        connection.execute(
            """INSERT INTO integration_outbox(integration, entity_type, entity_id)
               VALUES('obsidian', ?, ?)""",
            (entity_type, entity_id),
        )

    def flush_integration_outbox(self, limit: int = 20) -> dict[str, int]:
        """Best-effort external synchronization; database commits stay authoritative."""
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT * FROM integration_outbox
                   WHERE status='pending' AND next_attempt_at<=CURRENT_TIMESTAMP
                   ORDER BY created_at, id LIMIT ?""",
                (max(1, min(int(limit), 100)),),
            ).fetchall()
        completed = failed = 0
        for row in rows:
            try:
                if row["entity_type"] == "task":
                    task = self.get_task(row["entity_id"])
                    self.obsidian.sync_task(
                        task, self.task_relations(task["id"]), self.task_delivery(task["id"]),
                    )
                elif row["entity_type"] == "experience":
                    self.obsidian.sync_experience(self.get_experience(row["entity_id"]))
                else:
                    raise ValueError(f"Unsupported outbox entity: {row['entity_type']}")
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE integration_outbox SET status='completed', attempts=attempts+1,
                           last_error='', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'""",
                        (row["id"],),
                    )
                completed += 1
            except Exception as exc:
                delay = min(3600, 30 * (2 ** min(int(row["attempts"]), 7)))
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE integration_outbox SET attempts=attempts+1, last_error=?,
                           next_attempt_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                           WHERE id=? AND status='pending'""",
                        (str(exc)[:2000], f"+{delay} seconds", row["id"]),
                    )
                    self._event(
                        connection, row["entity_type"], row["entity_id"],
                        "obsidian_sync_deferred", {"error": str(exc)[:2000], "retry_seconds": delay},
                    )
                failed += 1
        return {"completed": completed, "failed": failed}
