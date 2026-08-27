from __future__ import annotations

from typing import Any


DEFAULT_TASK_TOKEN_BUDGET = 60_000


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
        return {"task_token_budget": self.task_token_budget()}

    def update_task_settings(self, payload: dict[str, Any]) -> dict[str, int]:
        value = payload.get("task_token_budget")
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ValueError("task_token_budget must be a positive integer")
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO system_settings(key, value, updated_at)
                   VALUES('task_token_budget', ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                (str(value),),
            )
            self._event(
                connection,
                "system",
                "settings",
                "task_settings_updated",
                {"task_token_budget": value},
            )
        return {"task_token_budget": value}
