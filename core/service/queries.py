from __future__ import annotations

from .domain import target_conflicts

import json
from typing import Any

from .domain import ACTIVE_RUN_STATUSES, decode_row


class TaskQueryMixin:
    """Read models used by API, MCP, lifecycle, and reporting surfaces."""

    def list_execution_logs(self) -> list[dict[str, Any]]:
        """Return the task/run event stream with its owning task and conversation."""
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT e.*, r.task_id AS run_task_id, r.run_type, r.attempt
                   FROM events e
                   LEFT JOIN task_runs r
                     ON e.entity_type='run' AND e.entity_id=r.id
                   WHERE (e.entity_type='task'
                      OR (e.entity_type='run' AND r.task_id IS NOT NULL))
                     AND e.event_type != 'token_usage_updated'
                   ORDER BY e.id DESC"""
            ).fetchall()
            conversation_rows = connection.execute(
                """SELECT run_id, thread_id FROM task_run_conversations
                   WHERE trim(thread_id) != ''"""
            ).fetchall()

        threads_by_run = {
            row["run_id"]: row["thread_id"] for row in conversation_rows
        }
        logs = []
        for row in rows:
            item = dict(row)
            payload = json.loads(item.pop("payload") or "{}")
            run_id = (
                item["entity_id"]
                if item["entity_type"] == "run"
                else str(payload.get("run_id") or "")
            )
            task_id = (
                item["entity_id"]
                if item["entity_type"] == "task"
                else str(item.pop("run_task_id") or payload.get("task_id") or "")
            )
            item.pop("run_task_id", None)
            logs.append(
                {
                    **item,
                    "task_id": task_id,
                    "run_id": run_id,
                    "thread_id": str(
                        payload.get("thread_id")
                        or threads_by_run.get(run_id)
                        or ""
                    ),
                    "payload": payload,
                }
            )
        return logs

    def list_requirements(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT r.*,
                          COUNT(t.id) AS child_task_count,
                          (SELECT nd.thread_id FROM native_dispatches nd
                           WHERE nd.entity_type='requirement'
                             AND nd.entity_id=r.id
                             AND trim(nd.thread_id) != ''
                           ORDER BY nd.created_at DESC, nd.run_id DESC LIMIT 1)
                            AS codex_thread_id
                   FROM requirements r
                   LEFT JOIN tasks t ON t.requirement_id=r.id
                   WHERE r.source_type != 'web_task'
                   GROUP BY r.id
                   ORDER BY r.created_at DESC"""
            ).fetchall()
        requirements = []
        for row in rows:
            requirement = dict(row)
            for field in (
                "modules", "scope", "out_of_scope", "acceptance_criteria",
                "decomposition_plan",
            ):
                requirement[field] = json.loads(requirement.get(field) or "[]")
            requirement["auto_dispatch"] = bool(requirement.get("auto_dispatch"))
            requirement["child_task_count"] = int(requirement.get("child_task_count") or 0)
            requirements.append(requirement)
        return requirements

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
            usage_rows = connection.execute(
                "SELECT task_id, json_extract(context_snapshot, '$.execution_usage') AS execution_usage FROM task_runs"
            ).fetchall()
            mapping_rows = connection.execute(
                """SELECT task_id, run_id, role, thread_id, title, summary, status, created_at, updated_at
                   FROM task_run_conversations ORDER BY task_id, created_at"""
            ).fetchall()
        tasks = [decode_row(row) for row in rows]
        usage_statuses: dict[str, list[tuple[bool, bool]]] = {}
        for row in usage_rows:
            receipts = json.loads(row["execution_usage"] or "{}")
            usage_statuses.setdefault(row["task_id"], []).append((bool(receipts) and all(item is not None for item in receipts.values()), any(item is not None for item in receipts.values())))
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
            scheduler_row = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            scheduler_state = dict(scheduler_row) if scheduler_row else {}
            dispatcher_enabled = self.dispatcher_enabled()
            policy_cache: dict[str, dict[str, Any]] = {}
            for task in tasks:
                task["token_by_stage"] = metrics.get(task["id"], {})
                statuses = usage_statuses.get(task["id"], [])
                task["token_usage_status"] = "reported" if statuses and all(complete for complete, _ in statuses) else "partial" if any(reported for _, reported in statuses) else "unavailable"
                task["target_conflicts"] = target_conflicts(connection, task["id"])
                task["dispatch_blockers"] = self._development_dispatch_blockers(
                    connection,
                    task,
                    policy_cache=policy_cache,
                    include_controller_state=True,
                    scheduler_state=scheduler_state,
                    dispatcher_enabled=dispatcher_enabled,
                )
                task["conversation_openable"] = task.get("active_run_status") not in ACTIVE_RUN_STATUSES
                task["execution_batch"] = self.execution_batch(task["id"])
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
        task = decode_row(row)
        with self.db.connection() as connection:
            task["target_conflicts"] = target_conflicts(connection, task_id)
            scheduler_row = connection.execute(
                "SELECT * FROM scheduler_state WHERE id=1"
            ).fetchone()
            task["dispatch_blockers"] = self._development_dispatch_blockers(
                connection,
                task,
                include_controller_state=True,
                scheduler_state=dict(scheduler_row) if scheduler_row else {},
                dispatcher_enabled=self.dispatcher_enabled(),
            )
            active = connection.execute(
                "SELECT status FROM task_runs WHERE id=? AND task_id=?",
                (task.get("active_run_id"), task_id),
            ).fetchone() if task.get("active_run_id") else None
            task["conversation_openable"] = not bool(active and active["status"] in ACTIVE_RUN_STATUSES)
        task["execution_batch"] = self.execution_batch(task_id)
        return task

    def task_details(self, task_id: str) -> dict[str, Any]:
        return {
            "task": self.get_task(task_id),
            "runs": self.list_runs(task_id),
            "conversations": self.list_conversations(task_id),
            "relations": self.task_relations(task_id),
            "reviews": self.list_reviews(task_id),
            "revisions": self.list_task_revisions(task_id),
            "events": self.list_events("task", task_id) + self.list_events("run", task_id=task_id),
            "execution_batch": self.execution_batch(task_id),
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
