from __future__ import annotations

from typing import Any


DEFAULT_TASK_TOKEN_BUDGET = 60_000
DEFAULT_MAX_BATCH_APPENDED_TASKS = 3
MAX_BATCH_APPENDED_TASKS_LIMIT = 20


class TaskSettingsMixin:
    """Persist and validate user-configurable task defaults."""

    def task_token_budget(self) -> int:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key='task_token_budget'"
            ).fetchone()
        if not row:
            return DEFAULT_TASK_TOKEN_BUDGET
        try:
            value = int(row["value"])
        except (TypeError, ValueError):
            return DEFAULT_TASK_TOKEN_BUDGET
        return value if value > 0 else DEFAULT_TASK_TOKEN_BUDGET

    def task_settings(self) -> dict[str, int]:
        return {
            "task_token_budget": self.task_token_budget(),
            "max_batch_appended_tasks": self.max_batch_appended_tasks(),
        }

    def max_batch_appended_tasks(self) -> int:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key='max_batch_appended_tasks'"
            ).fetchone()
        try:
            value = int(row["value"]) if row else DEFAULT_MAX_BATCH_APPENDED_TASKS
        except (TypeError, ValueError):
            return DEFAULT_MAX_BATCH_APPENDED_TASKS
        return value if 0 <= value <= MAX_BATCH_APPENDED_TASKS_LIMIT else DEFAULT_MAX_BATCH_APPENDED_TASKS

    def update_task_settings(self, payload: dict[str, Any]) -> dict[str, int]:
        value = payload.get("task_token_budget")
        appended = payload.get(
            "max_batch_appended_tasks", self.max_batch_appended_tasks()
        )
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("task_token_budget must be a positive integer")
        if (
            isinstance(appended, bool)
            or not isinstance(appended, int)
            or not 0 <= appended <= MAX_BATCH_APPENDED_TASKS_LIMIT
        ):
            raise ValueError(
                f"max_batch_appended_tasks must be an integer between 0 and {MAX_BATCH_APPENDED_TASKS_LIMIT}"
            )
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES('task_token_budget', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (str(value),),
            )
            connection.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES('max_batch_appended_tasks', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (str(appended),),
            )
            self._event(
                connection,
                "system",
                "settings",
                "task_settings_updated",
                {
                    "task_token_budget": value,
                    "max_batch_appended_tasks": appended,
                },
            )
        return {
            "task_token_budget": value,
            "max_batch_appended_tasks": appended,
        }
