from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from ..run_context import (
    RUN_CONTEXT_SCHEMA_VERSION,
    compact_acceptance_commands,
    freeze_run_context,
    lifecycle_tool_schema_version,
    model_run_context,
)
from .domain import RELATION_TYPES, _search_tokens
from ..workflow import task_requires_attention


class TaskReportingMixin:
    """Experiences, relations, bounded context, analytics, and workspace reporting."""

    def _create_experience(self, task: dict[str, Any]) -> dict[str, Any]:
        content = "\n\n".join(filter(None, [
            f"目标：{task.get('goal', '')}",
            f"交付：{task.get('delivery_summary', '')}",
            f"验证：{task.get('verification_result', '')}",
            "适用范围：" + "、".join(task.get("modules", [])),
        ]))
        keywords = list(dict.fromkeys([task["title"], *task.get("modules", [])]))[:12]
        with self.db.transaction() as connection:
            experience_id = self.db.next_id(connection, "EXP")
            connection.execute(
                """INSERT INTO experiences(id, title, project, modules, keywords, content, source_tasks)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (experience_id, task["title"], task.get("project"), json.dumps(task.get("modules", []), ensure_ascii=False),
                 json.dumps(keywords, ensure_ascii=False), content, json.dumps([task["id"]], ensure_ascii=False)),
            )
            self._event(connection, "experience", experience_id, "created", {"task_id": task["id"]})
            self._queue_obsidian_sync(connection, "experience", experience_id)
        experience = self.get_experience(experience_id)
        return experience

    def get_experience(self, experience_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM experiences WHERE id=?", (experience_id,)).fetchone()
        if not row:
            raise KeyError(f"Experience not found: {experience_id}")
        item = dict(row)
        for field in ("modules", "keywords", "source_tasks"):
            item[field] = json.loads(item[field] or "[]")
        return item

    def add_relation(self, source_task_id: str, target_task_id: str, relation_type: str, description: str = "") -> dict[str, Any]:
        if relation_type not in RELATION_TYPES:
            raise ValueError(f"Invalid relation type: {relation_type}")
        if source_task_id == target_task_id:
            raise ValueError("A task cannot relate to itself")
        self.get_task(source_task_id)
        self.get_task(target_task_id)
        with self.db.transaction() as connection:
            if relation_type in {"depends_on", "blocks"}:
                # Represent all scheduling edges as dependent -> prerequisite.
                dependent, prerequisite = (
                    (source_task_id, target_task_id)
                    if relation_type == "depends_on" else (target_task_id, source_task_id)
                )
                rows = connection.execute(
                    """SELECT source_task_id, target_task_id, relation_type FROM task_relations
                       WHERE relation_type IN ('depends_on','blocks')"""
                ).fetchall()
                adjacency: dict[str, set[str]] = {}
                for row in rows:
                    left, right = (
                        (row["source_task_id"], row["target_task_id"])
                        if row["relation_type"] == "depends_on"
                        else (row["target_task_id"], row["source_task_id"])
                    )
                    adjacency.setdefault(left, set()).add(right)
                pending = [prerequisite]
                seen: set[str] = set()
                while pending:
                    current = pending.pop()
                    if current == dependent:
                        raise ValueError("Dependency relation would create a cycle")
                    if current not in seen:
                        seen.add(current)
                        pending.extend(adjacency.get(current, set()))
            connection.execute(
                "INSERT OR IGNORE INTO task_relations(source_task_id, target_task_id, relation_type, description) VALUES(?, ?, ?, ?)",
                (source_task_id, target_task_id, relation_type, description),
            )
            self._event(connection, "task", source_task_id, "relation_added", {"target": target_task_id, "type": relation_type})
            self._queue_obsidian_sync(connection, "task", source_task_id)
            self._queue_obsidian_sync(connection, "task", target_task_id)
        task = self.get_task(source_task_id)
        relations = self.task_relations(source_task_id)
        self.flush_integration_outbox()
        return {"task": task, "relations": relations}

    def suggest_relations(self, task_id: str, limit: int = 8) -> list[dict[str, Any]]:
        task = self.get_task(task_id)
        candidates = [item for item in self.list_tasks() if item["id"] != task_id and item.get("project") == task.get("project")]
        task_modules = {item.lower() for item in task.get("modules", [])}
        task_terms = _search_tokens(f"{task['title']} {task.get('goal', '')}")
        suggestions = []
        for candidate in candidates:
            modules = {item.lower() for item in candidate.get("modules", [])}
            terms = _search_tokens(f"{candidate['title']} {candidate.get('goal', '')}")
            module_overlap = sorted(task_modules & modules)
            term_overlap = sorted(task_terms & terms)
            score = len(module_overlap) * 5 + len(term_overlap)
            if score == 0:
                continue
            active = candidate["status"] not in {"done", "cancelled"}
            suggestions.append({
                "task_id": candidate["id"], "title": candidate["title"], "status": candidate["status"],
                "score": score, "module_overlap": module_overlap, "term_overlap": term_overlap[:5],
                "recommended_relation": "conflicts_with" if active else "changed_from",
                "requires_confirmation": True,
            })
        return sorted(suggestions, key=lambda item: (-item["score"], item["task_id"]))[:max(1, min(limit, 20))]

    def task_relations(self, task_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT *, CASE WHEN source_task_id=? THEN 'outgoing' ELSE 'incoming' END AS direction
                   FROM task_relations WHERE source_task_id=? OR target_task_id=? ORDER BY created_at""",
                (task_id, task_id, task_id),
            ).fetchall()
        return [dict(row) for row in rows]

    def build_context(self, task_id: str, project_path: str | None = None) -> dict[str, Any]:
        task = self.get_task(task_id)
        if project_path and self._normalize_project(project_path) != task.get("project"):
            raise ValueError("project_path must match the task project")
        relations = self.task_relations(task_id)
        relation_summaries = []
        for relation in relations[:5]:
            other_id = relation["target_task_id"] if relation["source_task_id"] == task_id else relation["source_task_id"]
            other = self.get_task(other_id)
            relation_summaries.append({
                "task_id": other["id"], "direction": relation["direction"],
                "relation_type": relation["relation_type"], "title": other["title"],
                "goal": other["goal"], "status": other["status"],
            })
        conversations = [
            {"role": item["role"], "thread_id": item["thread_id"], "summary": item["summary"]}
            for item in self.list_conversations(task_id) if item["summary"]
        ][-3:]
        task_context = {
            key: task.get(key)
            for key in (
                "id", "title", "project", "type", "priority", "status", "goal", "modules",
                "scope", "out_of_scope", "acceptance_criteria", "context_version", "last_failure_reason",
            )
            if task.get(key) not in (None, "", [], {})
        }
        status = task.get("status")
        if status in {"review", "code_review"}:
            mode = "review"
            action = "审查该任务；只报告问题与结论，不修改代码"
        elif status in {"acceptance", "acceptance_blocked"}:
            mode = "acceptance"
            action = "验收该任务；只按验收计划验证，不修改实现"
        else:
            mode = "implementation"
            action = "完成该任务的实现"
        instruction = (
            f"在项目 {task.get('project') or '当前项目'} 中{action}：{task['id']}《{task['title']}》。"
            "以当前源码为准，先读取 located_targets 中的文件与直接依赖，只处理 task.scope，"
            "遵守 task.out_of_scope；完成后按 acceptance_plan 验证，并报告改动、验证结果和剩余风险。"
        )
        location_context = task.get("location_context", {})
        return {
            "instruction": instruction,
            "mode": mode,
            "task": task_context,
            "located_targets": location_context.get("targets", []),
            "location_evidence": {
                "obsidian": location_context.get("obsidian_evidence", {}),
                "source": location_context.get("location_evidence", {}),
            },
            "acceptance_plan": task.get("acceptance_plan", []),
            "dependency_analysis": task.get("dependency_analysis", {}),
            "implementation_contract": task.get("implementation_contract", {}),
            "review_contract": task.get("review_contract", {}),
            "direct_relations": relation_summaries,
            "conversation_summaries": conversations,
        }

    def build_execution_context(self, task_id: str, project_path: str | None = None) -> dict[str, Any]:
        """Build the minimal immutable input needed by an implementation worker."""
        task = self.get_task(task_id)
        if project_path and self._normalize_project(project_path) != task.get("project"):
            raise ValueError("project_path must match the task project")
        location_context = task.get("location_context") or {}
        implementation_contract = task.get("implementation_contract") or {}
        task_context = {
            key: task.get(key)
            for key in ("id", "title", "goal", "scope", "out_of_scope")
            if task.get(key) not in (None, "", [], {})
        }
        acceptance_commands = compact_acceptance_commands(task.get("acceptance_plan") or [])
        return {
            "task": task_context,
            "located_targets": location_context.get("targets") or [],
            "implementation_steps": implementation_contract.get("ordered_steps") or [],
            "visual_references": implementation_contract.get("visual_references") or [],
            "acceptance_commands": acceptance_commands,
        }

    def build_code_review_context(
        self, task_id: str, project_path: str | None = None,
    ) -> dict[str, Any]:
        """Build only the requirement, implementation, and checks needed for code review."""
        task = self.get_task(task_id)
        if project_path and self._normalize_project(project_path) != task.get("project"):
            raise ValueError("project_path must match the task project")
        implementation = task.get("implementation_contract") or {}
        review = task.get("review_contract") or {}
        return {
            "task": {
                key: task.get(key)
                for key in (
                    "id", "title", "goal", "scope", "out_of_scope", "acceptance_criteria",
                )
                if task.get(key) not in (None, "", [], {})
            },
            "implementation": {
                "targets": implementation.get("targets") or [],
                "steps": implementation.get("ordered_steps") or [],
            },
            "visual_references": implementation.get("visual_references") or [],
            "review_checks": review.get("checks") or [],
        }

    def build_acceptance_context(
        self, task_id: str, project_path: str | None = None,
    ) -> dict[str, Any]:
        """Build a criterion-level bundle for model-assisted functional acceptance."""
        task = self.get_task(task_id)
        if project_path and self._normalize_project(project_path) != task.get("project"):
            raise ValueError("project_path must match the task project")
        acceptance = []
        for item in task.get("acceptance_plan") or []:
            acceptance.append({
                key: item.get(key)
                for key in (
                    "criterion", "file", "symbol", "method", "command", "expected",
                    "check_type", "required", "timeout_seconds", "artifact_refs",
                )
                if item.get(key) not in (None, "", [], {})
            })
        return {
            "task": {
                key: task.get(key)
                for key in ("id", "title", "goal", "scope", "out_of_scope")
                if task.get(key) not in (None, "", [], {})
            },
            "acceptance_criteria": task.get("acceptance_criteria") or [],
            "acceptance": acceptance,
            "visual_references": (
                (task.get("implementation_contract") or {}).get("visual_references") or []
            ),
        }

    def get_run_context(
        self, task_id: str, run_id: str, project_path: str | None = None,
    ) -> dict[str, Any]:
        """Return the immutable run snapshot plus a small live concurrency guard."""
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        if run["task_id"] != task_id:
            raise ValueError("run_id does not belong to task_id")
        if project_path and self._normalize_project(project_path) != task.get("project"):
            raise ValueError("project_path must match the task project")
        snapshot = run.get("context_snapshot") or {}
        metadata = snapshot.get("cache_metadata") if isinstance(snapshot, dict) else None
        if isinstance(metadata, dict) and int(metadata.get("context_version") or 0) != int(task.get("context_version") or 1):
            raise ValueError("Run context is stale because the task context_version changed")
        metadata_refreshed = False
        if (
            not isinstance(metadata, dict)
            or int(metadata.get("snapshot_schema_version") or 0) != RUN_CONTEXT_SCHEMA_VERSION
            or str(metadata.get("tool_schema_version") or "") != lifecycle_tool_schema_version()
        ):
            # Active runs created by pre-cache builds keep their saved snapshot;
            # enrich or upgrade it in place instead of repeating Obsidian/CodeGraph queries.
            snapshot = freeze_run_context(
                snapshot or self.build_context(task_id, project_path),
                task=task, run_id=run_id, stage=str(run.get("run_type") or "execution"),
                delivery_run_id=str(run.get("delivery_run_id") or ""),
            )
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE task_runs SET context_snapshot=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (json.dumps(snapshot, ensure_ascii=False), run_id),
                )
            metadata = snapshot["cache_metadata"]
            metadata_refreshed = True
        return {
            "context_snapshot": model_run_context(snapshot),
            "guard": {
                "task_status": task["status"],
                "active_run_id": task.get("active_run_id"),
                "run_status": run["status"],
                "context_version": int(task.get("context_version") or 1),
                "is_active": task.get("active_run_id") == run_id,
            },
            "cache": {
                "hit": True,
                "metadata_refreshed": metadata_refreshed,
                "run_id": run_id,
                "snapshot_schema_version": int(metadata["snapshot_schema_version"]),
                "context_version": int(metadata["context_version"]),
                "tool_schema_version": metadata.get("tool_schema_version", ""),
            },
        }

    def board(self) -> dict[str, Any]:
        tasks = self.list_tasks()
        projects = sorted({str(task.get("project") or "").strip() for task in tasks if str(task.get("project") or "").strip()})
        return {
            "tasks": tasks,
            "pending_task_changes": self.list_pending_task_changes(),
            "projects": projects,
            "token_analytics": self.token_analytics(),
            "dispatcher": {"enabled": self.dispatcher_enabled()},
            "counts": {
                "tasks": len(tasks),
                "review": sum(task["status"] == "review" for task in tasks),
                "code_review": sum(task["status"] == "code_review" for task in tasks),
                "acceptance": sum(task["status"] in {"acceptance", "acceptance_blocked"} for task in tasks),
                "blocked": sum(task["status"] == "blocked" for task in tasks),
                "attention": sum(task_requires_attention(task) for task in tasks),
            },
            "integrations": {"obsidian": self.obsidian.status()},
        }

    @staticmethod
    def _token_event_timestamp(value: str) -> datetime:
        normalized = str(value or "").strip().replace(" ", "T")
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)

    def token_analytics(self, now: datetime | None = None) -> dict[str, Any]:
        """Aggregate Token deltas into local-day, local-week, and local-month buckets."""
        current = now or datetime.now().astimezone()
        if current.tzinfo is None:
            current = current.replace(tzinfo=datetime.now().astimezone().tzinfo)
        local_timezone = current.tzinfo
        day_start = current.replace(hour=0, minute=0, second=0, microsecond=0)
        week_start = day_start - timedelta(days=day_start.weekday())
        month_start = day_start.replace(day=1)
        earliest = min(week_start, month_start).astimezone(timezone.utc)
        earliest_value = earliest.replace(tzinfo=None).strftime("%Y-%m-%d %H:%M:%S")
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT token_delta, recorded_at FROM token_usage_events
                   WHERE recorded_at >= ? ORDER BY recorded_at""",
                (earliest_value,),
            ).fetchall()

        totals = {"today": 0, "week": 0, "month": 0}
        daily_totals: dict[str, int] = {}
        for row in rows:
            occurred_at = self._token_event_timestamp(row["recorded_at"]).astimezone(local_timezone)
            token_delta = int(row["token_delta"] or 0)
            if occurred_at >= day_start:
                totals["today"] += token_delta
            if occurred_at >= week_start:
                totals["week"] += token_delta
            if occurred_at >= month_start:
                totals["month"] += token_delta
                key = occurred_at.date().isoformat()
                daily_totals[key] = daily_totals.get(key, 0) + token_delta

        daily = []
        cursor = month_start
        while cursor.date() <= current.date():
            key = cursor.date().isoformat()
            daily.append({
                "date": key,
                "label": f"{cursor.month}月{cursor.day}日",
                "token_used": daily_totals.get(key, 0),
            })
            cursor += timedelta(days=1)
        return {"periods": totals, "daily": daily}

    def workspace_projects(self) -> list[str]:
        return self._workspace_path_setting("workspace_projects")

    def archived_workspace_projects(self) -> list[str]:
        return self._workspace_path_setting("archived_workspace_projects")

    def _workspace_path_setting(self, key: str) -> list[str]:
        connection = self.db.connect()
        try:
            row = connection.execute(
                "SELECT value FROM system_settings WHERE key=?", (key,)
            ).fetchone()
        finally:
            connection.close()
        if not row:
            return []
        try:
            values = json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return []
        projects: list[str] = []
        for value in values if isinstance(values, list) else []:
            normalized = self._normalize_project(value)
            if normalized and normalized not in projects:
                projects.append(normalized)
        return projects

    def _write_workspace_path_setting(self, key: str, projects: list[str]) -> None:
        connection = self.db.connect()
        try:
            with connection:
                connection.execute(
                    """INSERT INTO system_settings(key, value, updated_at)
                       VALUES(?, ?, CURRENT_TIMESTAMP)
                       ON CONFLICT(key) DO UPDATE SET
                         value=excluded.value, updated_at=CURRENT_TIMESTAMP""",
                    (key, json.dumps(projects, ensure_ascii=False)),
                )
        finally:
            connection.close()

    def _require_normalized_project_path(self, project: str | Path | None) -> str:
        value = str(project or "").strip()
        if not value or not Path(value).is_absolute():
            raise ValueError("project must be a normalized absolute path")
        normalized = self._normalize_project(value)
        if value != normalized:
            raise ValueError("project must be a normalized absolute path")
        return normalized

    def archive_workspace_project(self, project: str | Path | None) -> dict[str, Any]:
        normalized = self._require_normalized_project_path(project)
        projects = self.archived_workspace_projects()
        changed = normalized not in projects
        if changed:
            projects.append(normalized)
            projects.sort(key=lambda value: (Path(value).name.casefold(), value.casefold()))
            self._write_workspace_path_setting("archived_workspace_projects", projects)
        return {"path": normalized, "archived": True, "changed": changed}

    def restore_workspace_project(self, project: str | Path | None) -> dict[str, Any]:
        normalized = self._require_normalized_project_path(project)
        projects = self.archived_workspace_projects()
        changed = normalized in projects
        if changed:
            self._write_workspace_path_setting(
                "archived_workspace_projects", [item for item in projects if item != normalized],
            )
        return {"path": normalized, "archived": False, "changed": changed}

    def add_workspace_project(self, project: str | Path | None) -> str:
        normalized = self._require_project_directory(project)
        projects = self.workspace_projects()
        if normalized not in projects:
            projects.append(normalized)
            projects.sort(key=lambda value: Path(value).name.casefold())
            self._write_workspace_path_setting("workspace_projects", projects)
        return normalized

    def latest_event_id(self) -> int:
        connection = self.db.connect()
        try:
            row = connection.execute("SELECT COALESCE(MAX(id), 0) AS id FROM events").fetchone()
            return int(row["id"])
        finally:
            connection.close()

    def integration_status(self, project_path: str | None = None) -> dict[str, Any]:
        return {"obsidian": self.obsidian.status(), "location": self.location_status(project_path)}
