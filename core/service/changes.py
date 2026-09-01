from __future__ import annotations

import json
from collections import Counter
from typing import Any

from .domain import (
    GENERIC_MATCH_TERMS,
    JSON_FIELDS,
    decode_row,
    search_tokens,
    specific_modules,
)

TERMINAL_TASK_STATUSES = {"done", "cancelled"}
REVISION_FIELDS = (
    "title",
    "type",
    "project",
    "modules",
    "priority",
    "goal",
    "scope",
    "out_of_scope",
    "acceptance_criteria",
    "source_thread_id",
    "token_budget",
    "location_context",
    "acceptance_plan",
    "dependency_analysis",
    "implementation_contract",
    "review_contract",
    "context_version",
)


class TaskChangeMixin:
    """Detect, confirm, and atomically apply mid-course requirement changes."""

    def detect_task_change(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        if not title or not goal:
            raise ValueError("title and goal are required")
        project = self._require_project_directory(payload.get("project"))
        modules = payload.get("modules") or []
        if not isinstance(modules, list) or any(
            not isinstance(item, str) for item in modules
        ):
            raise ValueError("modules must be an array of strings")
        source_thread_id = str(payload.get("source_thread_id") or "").strip()
        explicit_task_id = str(payload.get("task_id") or "").strip()
        query_tokens = search_tokens(" ".join((title, goal, *modules)))
        normalized_modules = specific_modules(modules)

        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM tasks WHERE project=? ORDER BY updated_at DESC, id DESC",
                (project,),
            ).fetchall()
            thread_task_ids = set()
            if source_thread_id:
                thread_task_ids = {
                    row["task_id"]
                    for row in connection.execute(
                        "SELECT DISTINCT task_id FROM task_conversations WHERE thread_id=?",
                        (source_thread_id,),
                    ).fetchall()
                }

        candidates: list[dict[str, Any]] = []
        for row in rows:
            task = decode_row(row)
            if explicit_task_id and task["id"] != explicit_task_id:
                continue
            # Automatic change detection is intentionally limited to active
            # work. Historical relations remain available when the caller
            # explicitly names a terminal task.
            if task["status"] in TERMINAL_TASK_STATUSES and not explicit_task_id:
                continue
            reasons: list[str] = []
            score = 0
            same_thread = False
            if task["id"] == explicit_task_id:
                score += 200
                reasons.append("明确指定此任务")
            if source_thread_id and (
                task.get("source_thread_id") == source_thread_id
                or task.get("codex_thread_id") == source_thread_id
                or task["id"] in thread_task_ids
            ):
                same_thread = True
                score += 100
                reasons.append("来自同一 Codex 会话")
            task_modules = specific_modules(task.get("modules", []))
            module_overlap = sorted(normalized_modules & task_modules)
            if module_overlap:
                score += min(50, 25 * len(module_overlap))
                reasons.append("模块重合：" + "、".join(module_overlap[:3]))
            task_tokens = search_tokens(
                " ".join(
                    (
                        task.get("title", ""),
                        task.get("goal", ""),
                        *task.get("modules", []),
                    )
                )
            )
            overlap = (query_tokens & task_tokens) - GENERIC_MATCH_TERMS
            if overlap:
                lexical_score = min(40, len(overlap) * 4)
                score += lexical_score
                reasons.append("需求语义重合")
            strong_match = bool(
                explicit_task_id
                or same_thread
                or module_overlap
                or len(overlap) >= 2
            )
            if strong_match and score >= 16:
                candidates.append(
                    {
                        "task_id": task["id"],
                        "title": task["title"],
                        "status": task["status"],
                        "context_version": task.get("context_version", 1),
                        "score": score,
                        "reasons": reasons,
                        "can_revise": task["status"] not in TERMINAL_TASK_STATUSES,
                    }
                )
        candidates.sort(key=lambda item: (-item["score"], item["task_id"]))
        top = candidates[0] if candidates else None
        if not top:
            return {
                "decision": "new_task",
                "requires_confirmation": False,
                "candidates": [],
            }
        if not top["can_revise"]:
            return {
                "decision": "new_task",
                "requires_confirmation": False,
                "relation": {
                    "relation_type": "changed_from",
                    "target_task_id": top["task_id"],
                },
                "candidates": candidates[:3],
            }
        return {
            "decision": "requires_confirmation",
            "requires_confirmation": True,
            "candidate": top,
            "candidates": candidates[:3],
            "options": [
                {
                    "value": "revise",
                    "label": f"修订 {top['task_id']}",
                    "recommended": True,
                },
                {"value": "create_new", "label": "创建新任务", "recommended": False},
            ],
        }

    def prepare_task_change_confirmation(
        self, payload: dict[str, Any]
    ) -> dict[str, Any]:
        candidate_task_id = str(payload.get("candidate_task_id") or "").strip()
        request_text = str(payload.get("request_text") or "").strip()
        proposed_task = payload.get("proposed_task")
        if (
            not candidate_task_id
            or not request_text
            or not isinstance(proposed_task, dict)
        ):
            raise ValueError(
                "candidate_task_id, request_text and proposed_task are required"
            )
        task = self.get_task(candidate_task_id)
        if task["status"] in TERMINAL_TASK_STATUSES:
            raise ValueError(
                "Completed or cancelled tasks cannot be revised; create a related task"
            )
        project = self._require_project_directory(proposed_task.get("project"))
        if self._normalize_project(project) != self._normalize_project(
            task.get("project")
        ):
            raise ValueError("Proposed task project must match the candidate task")
        analysis_id = str(proposed_task.get("location_analysis_id") or "").strip()
        analysis = self.get_location_analysis(analysis_id)
        if (
            analysis.get("stage") != "change"
            or analysis.get("task_id") != candidate_task_id
            or analysis.get("status") != "completed"
            or analysis.get("consumed_at")
        ):
            raise ValueError(
                "A completed, unused change location analysis bound to the candidate is required"
            )
        if self._normalize_project(analysis.get("project")) != self._normalize_project(
            project
        ):
            raise ValueError(
                "Location analysis project does not match the proposed task"
            )
        proposed_task = dict(proposed_task)
        implementation = (
            proposed_task.get("implementation_contract")
            or analysis.get("implementation_contract")
            or {}
        )
        review = (
            proposed_task.get("review_contract")
            or analysis.get("review_contract")
            or {}
        )
        implementation = self._validate_implementation_contract(
            implementation,
            analysis.get("targets") or [],
        )
        self._validate_revision_contracts(analysis, implementation, review)
        proposed_task["implementation_contract"] = implementation
        proposed_task["review_contract"] = review
        evidence = payload.get("evidence") or {}
        source_thread_id = str(
            payload.get("source_thread_id")
            or proposed_task.get("source_thread_id")
            or ""
        ).strip()
        serialized_task = json.dumps(proposed_task, ensure_ascii=False, sort_keys=True)
        with self.db.transaction() as connection:
            existing = connection.execute(
                """SELECT * FROM task_change_requests
                   WHERE candidate_task_id=? AND status='pending' AND proposed_task=?
                   ORDER BY created_at DESC LIMIT 1""",
                (candidate_task_id, serialized_task),
            ).fetchone()
            if existing:
                return self._decode_change_request(existing)
            change_id = self.db.next_id(connection, "CHANGE")
            connection.execute(
                """INSERT INTO task_change_requests(
                       id, candidate_task_id, source_thread_id, request_text, proposed_task, evidence
                   ) VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    change_id,
                    candidate_task_id,
                    source_thread_id or None,
                    request_text,
                    serialized_task,
                    json.dumps(evidence, ensure_ascii=False),
                ),
            )
            self._event(
                connection,
                "task",
                candidate_task_id,
                "change_confirmation_requested",
                {"change_request_id": change_id},
            )
        return self.get_task_change_request(change_id)

    @staticmethod
    def _decode_change_request(row: Any) -> dict[str, Any]:
        item = dict(row)
        item["proposed_task"] = json.loads(item.get("proposed_task") or "{}")
        item["evidence"] = json.loads(item.get("evidence") or "{}")
        return item

    def get_task_change_request(self, change_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                """SELECT request.*, task.title AS candidate_title, task.status AS candidate_status
                   FROM task_change_requests request
                   JOIN tasks task ON task.id=request.candidate_task_id WHERE request.id=?""",
                (change_id,),
            ).fetchone()
        if not row:
            raise KeyError(f"Task change request not found: {change_id}")
        return self._decode_change_request(row)

    def list_pending_task_changes(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT request.*, task.title AS candidate_title, task.status AS candidate_status
                   FROM task_change_requests request
                   JOIN tasks task ON task.id=request.candidate_task_id
                   WHERE request.status='pending' ORDER BY request.created_at, request.id"""
            ).fetchall()
        return [self._decode_change_request(row) for row in rows]

    def list_task_revisions(self, task_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM task_revisions WHERE task_id=? ORDER BY version DESC",
                (task_id,),
            ).fetchall()
        revisions = []
        for row in rows:
            item = dict(row)
            item["before_snapshot"] = json.loads(item.get("before_snapshot") or "{}")
            item["after_snapshot"] = json.loads(item.get("after_snapshot") or "{}")
            revisions.append(item)
        return revisions

    def resolve_task_change_confirmation(
        self, change_id: str, decision: str
    ) -> dict[str, Any]:
        if decision not in {"revise", "create_new"}:
            raise ValueError("decision must be revise or create_new")
        with self.db.transaction() as connection:
            row = connection.execute(
                "SELECT * FROM task_change_requests WHERE id=?", (change_id,)
            ).fetchone()
            if not row:
                raise KeyError(f"Task change request not found: {change_id}")
            if row["status"] == "resolved":
                return self.get_task_change_request(change_id)
            if row["status"] != "pending":
                raise ValueError("Task change request is already being resolved")
            cursor = connection.execute(
                "UPDATE task_change_requests SET status='resolving', decision=?, error='', updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='pending'",
                (decision, change_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Task change request changed concurrently")
            request = self._decode_change_request(row)
        try:
            if decision == "revise":
                result = self._revise_task_from_change(request)
            else:
                proposed = dict(request["proposed_task"])
                proposed["relations"] = list(proposed.get("relations") or []) + [
                    {
                        "target_task_id": request["candidate_task_id"],
                        "relation_type": "references",
                        "description": "由同一需求变更确认拆分为独立任务",
                    }
                ]
                proposed["status"] = "ready"
                result = self.create_task(proposed)
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE task_change_requests SET status='resolved', result_task_id=?,
                       resolved_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (result["id"], change_id),
                )
            resolved = self.get_task_change_request(change_id)
            resolved["task"] = result
            return resolved
        except Exception as exc:
            with self.db.transaction() as connection:
                connection.execute(
                    "UPDATE task_change_requests SET status='pending', error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (str(exc)[:2000], change_id),
                )
            raise

    def _revise_task_from_change(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request["candidate_task_id"]
        proposed = dict(request["proposed_task"])
        current = self.get_task(task_id)
        if current["status"] in TERMINAL_TASK_STATUSES:
            raise ValueError(
                "Completed or cancelled tasks cannot be revised; create a related task"
            )
        title = str(proposed.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        list_values = {
            field: self._string_list(proposed, field) for field in JSON_FIELDS
        }
        self._assert_ready_payload(proposed, list_values)
        project = self._require_project_directory(proposed.get("project"))
        if self._normalize_project(project) != self._normalize_project(
            current.get("project")
        ):
            raise ValueError("A revision cannot move a task to another project")
        priority = str(proposed.get("priority", current.get("priority", "P2")))
        if priority not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("priority must be P0, P1, P2 or P3")
        with self.db.transaction() as connection:
            location = self._consume_location_analysis(
                connection,
                proposed.get("location_analysis_id"),
                project,
                allowed_stages=("change",),
                task_id=task_id,
            )
            planned = [
                str(item.get("criterion") or "").strip()
                for item in location.get("acceptance_plan", [])
            ]
            if Counter(planned) != Counter(list_values["acceptance_criteria"]):
                raise ValueError(
                    "Location acceptance plan must exactly match the revised acceptance criteria"
                )
            implementation = self._validate_implementation_contract(
                proposed.get("implementation_contract")
                or location.get("implementation_contract")
                or {},
                location.get("targets") or [],
            )
            review = (
                proposed.get("review_contract") or location.get("review_contract") or {}
            )
            self._validate_revision_contracts(location, implementation, review)
            before = {field: current.get(field) for field in REVISION_FIELDS}
            version = int(current.get("context_version") or 1) + 1
            has_runs = bool(
                connection.execute(
                    "SELECT 1 FROM task_runs WHERE task_id=? LIMIT 1", (task_id,)
                ).fetchone()
            )
            if current["status"] == "paused":
                next_status = "paused"
            else:
                next_status = "rework" if has_runs else "ready"
            connection.execute(
                """UPDATE task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE task_id=? AND status IN ('awaiting_thread','running')""",
                (task_id,),
            )
            connection.execute(
                """UPDATE task_run_conversations SET status='interrupted', updated_at=CURRENT_TIMESTAMP
                   WHERE task_id=? AND status='active' AND run_id IN (
                     SELECT id FROM task_runs WHERE task_id=? AND status='interrupted'
                   )""",
                (task_id, task_id),
            )
            connection.execute(
                """UPDATE task_conversations SET status='interrupted', updated_at=CURRENT_TIMESTAMP
                   WHERE task_id=? AND status='active' AND thread_id IN (
                     SELECT thread_id FROM task_run_conversations WHERE task_id=? AND status='interrupted'
                   )""",
                (task_id, task_id),
            )
            dependency = (
                proposed.get("dependency_analysis")
                or location.get("dependency_analysis")
                or {"decision": "independent"}
            )
            dependency = self._normalize_dependency_analysis(dependency)
            connection.execute(
                """UPDATE tasks SET title=?, type=?, modules=?, status=?, priority=?, goal=?, scope=?,
                   out_of_scope=?, acceptance_criteria=?, source_thread_id=COALESCE(?, source_thread_id),
                   token_budget=?, context_version=?, active_run_id=NULL, assigned_to=NULL,
                   delivery_summary='', verification_result='', location_context=?, acceptance_plan=?,
                   dependency_analysis=?, implementation_contract=?, review_contract=?,
                   retry_required=?, retry_run_type=?, last_failure_reason='', last_failure_at=NULL,
                   review_failed_at=NULL, last_review_reasons='[]', last_failed_criteria='[]',
                   auto_dispatch=CASE WHEN status='paused' THEN auto_dispatch ELSE 1 END,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    title,
                    proposed.get("type", current.get("type", "feature")),
                    json.dumps(list_values["modules"], ensure_ascii=False),
                    next_status,
                    priority,
                    str(proposed.get("goal") or "").strip(),
                    json.dumps(list_values["scope"], ensure_ascii=False),
                    json.dumps(list_values["out_of_scope"], ensure_ascii=False),
                    json.dumps(list_values["acceptance_criteria"], ensure_ascii=False),
                    proposed.get("source_thread_id"),
                    int(
                        proposed.get("token_budget", current.get("token_budget", 60000))
                    ),
                    version,
                    json.dumps(location, ensure_ascii=False),
                    json.dumps(location.get("acceptance_plan", []), ensure_ascii=False),
                    json.dumps(dependency, ensure_ascii=False),
                    json.dumps(implementation, ensure_ascii=False),
                    json.dumps(review, ensure_ascii=False),
                    int(next_status == "rework"),
                    "rework" if next_status == "rework" else None,
                    task_id,
                ),
            )
            connection.execute("DELETE FROM task_targets WHERE task_id=?", (task_id,))
            self._store_task_targets(connection, task_id, location.get("targets", []))
            connection.execute(
                "DELETE FROM task_relations WHERE source_task_id=? "
                "AND relation_type IN ('depends_on','continues_from','conflicts_with')",
                (task_id,),
            )
            for related_id in dependency["depends_tasks"]:
                self._insert_relation_in_connection(
                    connection, task_id, related_id, "depends_on", "revised scheduling dependency",
                )
            if dependency.get("continues_from_task_id"):
                self._insert_relation_in_connection(
                    connection, task_id, dependency["continues_from_task_id"],
                    "continues_from", "revised task continuation",
                )
            for related_id in dependency["conflicts_tasks"]:
                self._insert_relation_in_connection(
                    connection, task_id, related_id, "conflicts_with", "revised scheduling conflict",
                )
            after = {
                "title": title,
                "type": proposed.get("type", current.get("type", "feature")),
                "project": project,
                "modules": list_values["modules"],
                "priority": priority,
                "goal": str(proposed.get("goal") or "").strip(),
                "scope": list_values["scope"],
                "out_of_scope": list_values["out_of_scope"],
                "acceptance_criteria": list_values["acceptance_criteria"],
                "location_context": location,
                "acceptance_plan": location.get("acceptance_plan", []),
                "dependency_analysis": dependency,
                "implementation_contract": implementation,
                "review_contract": review,
                "context_version": version,
            }
            revision_id = self.db.next_id(connection, "REV")
            connection.execute(
                """INSERT INTO task_revisions(id, task_id, change_request_id, version, reason, before_snapshot, after_snapshot)
                   VALUES(?, ?, ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    task_id,
                    request["id"],
                    version,
                    request["request_text"],
                    json.dumps(before, ensure_ascii=False),
                    json.dumps(after, ensure_ascii=False),
                ),
            )
            source_thread = str(
                proposed.get("source_thread_id")
                or request.get("source_thread_id")
                or ""
            ).strip()
            if source_thread:
                connection.execute(
                    "INSERT OR IGNORE INTO task_conversations(task_id, role, thread_id, title) VALUES(?, 'source', ?, ?)",
                    (task_id, source_thread, f"{task_id} 需求修订 v{version}"),
                )
            self._event(
                connection,
                "task",
                task_id,
                "revised",
                {
                    "change_request_id": request["id"],
                    "revision_id": revision_id,
                    "from_version": version - 1,
                    "to_version": version,
                    "status": next_status,
                },
            )
            self._queue_obsidian_sync(connection, "task", task_id)
        self.flush_integration_outbox()
        return self.get_task(task_id)

    def _validate_revision_contracts(
        self,
        location: dict[str, Any],
        implementation: dict[str, Any],
        review: dict[str, Any],
    ) -> None:
        self._validate_implementation_contract(
            implementation, location.get("targets") or []
        )
        self._normalize_review_contract(review)
