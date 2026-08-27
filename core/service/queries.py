from __future__ import annotations

import json
from typing import Any

from .domain import ACTIVE_RUN_STATUSES, _decode_row


class TaskQueryMixin:
    """Read models used by API, MCP, lifecycle, and reporting surfaces."""

    def list_tasks(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT t.*,
                          (SELECT COUNT(*) FROM task_conversations c WHERE c.task_id=t.id) AS conversation_count,
                          CASE WHEN t.primary_run_id IS NULL THEN 0 ELSE 1 END AS run_count,
                          (SELECT r.status FROM task_runs r
                           WHERE r.id=t.active_run_id AND r.task_id=t.id) AS active_run_status
                   FROM tasks t ORDER BY t.created_at DESC"""
            ).fetchall()
            metric_rows = connection.execute(
                """SELECT task_id, run_type, COUNT(*) AS attempts,
                          COALESCE(SUM(token_used), 0) AS token_used,
                          COALESCE(SUM(effective_token_used), 0) AS effective_token_used,
                          COALESCE(SUM(input_tokens), 0) AS input_tokens,
                          COALESCE(SUM(cached_input_tokens), 0) AS cached_input_tokens,
                          COALESCE(SUM(output_tokens), 0) AS output_tokens,
                          COALESCE(SUM(reasoning_output_tokens), 0) AS reasoning_output_tokens
                   FROM task_runs GROUP BY task_id, run_type"""
            ).fetchall()
            conversation_rows = connection.execute(
                "SELECT * FROM task_conversations ORDER BY task_id, created_at"
            ).fetchall()
            mapping_rows = connection.execute(
                """SELECT task_id, run_id, role, thread_id, title, summary, status, created_at, updated_at
                   FROM task_run_conversations ORDER BY task_id, created_at"""
            ).fetchall()
        tasks = [_decode_row(row) for row in rows]
        metrics: dict[str, dict[str, dict[str, int]]] = {}
        for row in metric_rows:
            metrics.setdefault(row["task_id"], {})[row["run_type"]] = {
                "attempts": int(row["attempts"] or 0),
                "token_used": int(row["token_used"] or 0),
                "effective_token_used": int(row["effective_token_used"] or 0),
                "input_tokens": int(row["input_tokens"] or 0),
                "cached_input_tokens": int(row["cached_input_tokens"] or 0),
                "output_tokens": int(row["output_tokens"] or 0),
                "reasoning_output_tokens": int(row["reasoning_output_tokens"] or 0),
            }
        conversations: dict[str, list[dict[str, Any]]] = {}
        canonical: dict[tuple[str, str, str], dict[str, Any]] = {}
        for row in conversation_rows:
            item = dict(row)
            conversations.setdefault(item["task_id"], []).append(item)
            canonical[(item["task_id"], item["thread_id"], item["role"])] = item
        for row in mapping_rows:
            item = dict(row)
            parent = canonical.get((item["task_id"], item["thread_id"], item["role"]))
            if parent is not None:
                parent.setdefault("run_ids", []).append(item["run_id"])
                parent["status"] = item["status"]
                parent["title"] = item["title"] or parent.get("title", "")
                if item["summary"]:
                    parent["summary"] = item["summary"]
            else:
                item["run_ids"] = [item["run_id"]]
                conversations.setdefault(item["task_id"], []).append(item)
        with self.db.connection() as connection:
            for task in tasks:
                task["token_by_stage"] = metrics.get(task["id"], {})
                task["target_conflicts"] = self._target_conflicts(connection, task["id"])
                task["project_blockers"] = self._project_blockers(connection, task["id"])
                task["conversation_openable"] = task.get("active_run_status") not in ACTIVE_RUN_STATUSES
                task["conversations"] = [
                    {
                        "role": item["role"], "thread_id": item["thread_id"],
                        "title": item.get("title", ""), "status": item.get("status", ""),
                        "created_at": item.get("created_at", ""), "updated_at": item.get("updated_at", ""),
                    }
                    for item in conversations.get(task["id"], [])
                ]
        return tasks

    def get_task(self, task_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if not row:
            raise KeyError(f"Task not found: {task_id}")
        task = _decode_row(row)
        with self.db.connection() as connection:
            task["target_conflicts"] = self._target_conflicts(connection, task_id)
            task["project_blockers"] = self._project_blockers(connection, task_id)
            active = connection.execute(
                "SELECT status FROM task_runs WHERE id=? AND task_id=?",
                (task.get("active_run_id"), task_id),
            ).fetchone() if task.get("active_run_id") else None
            task["conversation_openable"] = not bool(active and active["status"] in ACTIVE_RUN_STATUSES)
        return task

    def task_details(self, task_id: str) -> dict[str, Any]:
        return {
            "task": self.get_task(task_id),
            "runs": self.list_runs(task_id),
            "conversations": self.list_conversations(task_id),
            "relations": self.task_relations(task_id),
            "reviews": self.list_reviews(task_id),
            "acceptance_results": self.list_acceptance_results(task_id),
            "acceptance_checks": self.list_acceptance_checks(task_id),
            "revisions": self.list_task_revisions(task_id),
            "events": self.list_events("task", task_id) + self.list_events("run", task_id=task_id),
        }

    def list_events(self, entity_type: str, entity_id: str | None = None, task_id: str | None = None) -> list[dict[str, Any]]:
        if task_id:
            query = """SELECT e.* FROM events e
                       JOIN task_runs r ON e.entity_type='run' AND e.entity_id=r.id
                       WHERE r.task_id=? ORDER BY e.created_at"""
            values: tuple[Any, ...] = (task_id,)
        else:
            query = "SELECT * FROM events WHERE entity_type=? AND entity_id=? ORDER BY created_at"
            values = (entity_type, entity_id)
        with self.db.connection() as connection:
            rows = connection.execute(query, values).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["payload"] = json.loads(item["payload"] or "{}")
            result.append(item)
        return result
