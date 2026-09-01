from __future__ import annotations

from typing import Any


DEFAULT_TASK_TOKEN_BUDGET = 60_000
DEFAULT_MAX_BATCH_APPENDED_TASKS = 3
MAX_BATCH_APPENDED_TASKS_LIMIT = 20
DEFAULT_PARALLEL_DEVELOPMENT_ENABLED = False
DEFAULT_MAX_PARALLEL_DEVELOPMENT = 2
MAX_PARALLEL_DEVELOPMENT_LIMIT = 8


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

    def task_settings(self) -> dict[str, int | bool]:
        return {
            "task_token_budget": self.task_token_budget(),
            "max_batch_appended_tasks": self.max_batch_appended_tasks(),
            "parallel_development_enabled": self.parallel_development_enabled(),
            "max_parallel_development": self.max_parallel_development(),
        }

    def parallel_development_enabled(self) -> bool:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key='parallel_development_enabled'"
            ).fetchone()
        if not row:
            return DEFAULT_PARALLEL_DEVELOPMENT_ENABLED
        return str(row["value"]).strip().lower() in {"1", "true", "yes", "on"}

    def max_parallel_development(self) -> int:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key='max_parallel_development'"
            ).fetchone()
        try:
            value = int(row["value"]) if row else DEFAULT_MAX_PARALLEL_DEVELOPMENT
        except (TypeError, ValueError):
            return DEFAULT_MAX_PARALLEL_DEVELOPMENT
        return (
            value
            if 1 <= value <= MAX_PARALLEL_DEVELOPMENT_LIMIT
            else DEFAULT_MAX_PARALLEL_DEVELOPMENT
        )

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

    def update_task_settings(self, payload: dict[str, Any]) -> dict[str, int | bool]:
        value = payload.get("task_token_budget")
        appended = payload.get(
            "max_batch_appended_tasks", self.max_batch_appended_tasks()
        )
        parallel_enabled = payload.get(
            "parallel_development_enabled", self.parallel_development_enabled()
        )
        max_parallel = payload.get(
            "max_parallel_development", self.max_parallel_development()
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
        if not isinstance(parallel_enabled, bool):
            raise ValueError("parallel_development_enabled must be boolean")
        if (
            isinstance(max_parallel, bool)
            or not isinstance(max_parallel, int)
            or not 1 <= max_parallel <= MAX_PARALLEL_DEVELOPMENT_LIMIT
        ):
            raise ValueError(
                f"max_parallel_development must be an integer between 1 and {MAX_PARALLEL_DEVELOPMENT_LIMIT}"
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
            connection.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES('parallel_development_enabled', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                ("1" if parallel_enabled else "0",),
            )
            connection.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES('max_parallel_development', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (str(max_parallel),),
            )
            self._event(
                connection,
                "system",
                "settings",
                "task_settings_updated",
                {
                    "task_token_budget": value,
                    "max_batch_appended_tasks": appended,
                    "parallel_development_enabled": parallel_enabled,
                    "max_parallel_development": max_parallel,
                },
            )
        return {
            "task_token_budget": value,
            "max_batch_appended_tasks": appended,
            "parallel_development_enabled": parallel_enabled,
            "max_parallel_development": max_parallel,
        }
