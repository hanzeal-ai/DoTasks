from __future__ import annotations

from .domain import insert_relation

import json
import uuid
from typing import Any

from ..workflow import default_code_review_checks
from .domain import REQUIREMENT_DECOMPOSITION_ATTEMPT_LIMIT, decode_row


class TaskRequirementMixin:
    @staticmethod
    def _decode_requirement(row: Any) -> dict[str, Any]:
        item = dict(row)
        for field in (
            "modules", "scope", "out_of_scope", "acceptance_criteria",
            "decomposition_plan", "visual_references",
        ):
            item[field] = json.loads(item.get(field) or "[]")
        item["auto_dispatch"] = bool(item.get("auto_dispatch"))
        return item

    def get_requirement(self, requirement_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute(
                "SELECT * FROM requirements WHERE id=?", (requirement_id,),
            ).fetchone()
            if not row:
                raise KeyError(f"Requirement not found: {requirement_id}")
            tasks = connection.execute(
                """SELECT * FROM tasks WHERE requirement_id=?
                   ORDER BY requirement_task_key, created_at, id""",
                (requirement_id,),
            ).fetchall()
            decomposition_runs = [dict(item) for item in connection.execute(
                """SELECT * FROM requirement_decomposition_runs
                   WHERE requirement_id=? ORDER BY attempt, created_at""",
                (requirement_id,),
            ).fetchall()]
            task_ids = [row["id"] for row in tasks]
            relations = []
            if task_ids:
                placeholders = ",".join("?" for _ in task_ids)
                relations = [dict(item) for item in connection.execute(
                    f"""SELECT * FROM task_relations
                        WHERE source_task_id IN ({placeholders})
                          AND target_task_id IN ({placeholders})
                        ORDER BY created_at, id""",
                    (*task_ids, *task_ids),
                ).fetchall()]
        return {
            "intake_kind": "requirement",
            "requirement": self._decode_requirement(row),
            "tasks": [decode_row(item) for item in tasks],
            "relations": relations,
            "decomposition_runs": decomposition_runs,
        }

    def delete_requirement(self, requirement_id: str) -> dict[str, Any]:
        """Delete a requirement while preserving its child tasks as standalone history."""
        with self.db.transaction() as connection:
            requirement = connection.execute(
                "SELECT id, title, visual_references FROM requirements WHERE id=?", (requirement_id,),
            ).fetchone()
            if not requirement:
                raise KeyError(f"Requirement not found: {requirement_id}")
            child_count = int(connection.execute(
                "SELECT COUNT(*) FROM tasks WHERE requirement_id=?", (requirement_id,),
            ).fetchone()[0])
            connection.execute(
                """UPDATE tasks SET requirement_id=NULL, requirement_task_key=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE requirement_id=?""",
                (requirement_id,),
            )
            connection.execute(
                "DELETE FROM native_dispatches WHERE entity_type='requirement' AND entity_id=?",
                (requirement_id,),
            )
            connection.execute("DELETE FROM requirements WHERE id=?", (requirement_id,))
            self._event(
                connection, "requirement", requirement_id, "deleted",
                {"title": requirement["title"], "preserved_task_count": child_count},
            )
        self._delete_unreferenced_visuals(
            json.loads(requirement["visual_references"] or "[]")
        )
        return {
            "status": "deleted",
            "requirement_id": requirement_id,
            "preserved_task_count": child_count,
        }

    def delete_task(self, task_id: str) -> dict[str, Any]:
        """Permanently remove one completed DoTasks record without deleting Codex sessions."""
        with self.db.transaction() as connection:
            task = connection.execute(
                "SELECT id, title, status, implementation_contract FROM tasks WHERE id=?", (task_id,),
            ).fetchone()
            if not task:
                raise KeyError(f"Task not found: {task_id}")
            if task["status"] != "done":
                raise ValueError("Only completed tasks can be deleted")

            run_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM task_runs WHERE task_id=?", (task_id,),
                ).fetchall()
            ]
            owned_batches = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM execution_batches WHERE owner_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            if owned_batches:
                placeholders = ",".join("?" for _ in owned_batches)
                unfinished_member = connection.execute(
                    f"""SELECT member.task_id FROM execution_batch_tasks member
                        JOIN tasks other ON other.id=member.task_id
                        WHERE member.batch_id IN ({placeholders})
                          AND member.task_id!=?
                          AND other.status NOT IN ('done','cancelled') LIMIT 1""",
                    (*owned_batches, task_id),
                ).fetchone()
                if unfinished_member:
                    raise ValueError(
                        "Completed task owns a batch with unfinished member tasks"
                    )
                connection.execute(
                    f"DELETE FROM execution_batches WHERE id IN ({placeholders})",
                    owned_batches,
                )

            change_ids = [
                row["id"]
                for row in connection.execute(
                    "SELECT id FROM task_change_requests WHERE candidate_task_id=?",
                    (task_id,),
                ).fetchall()
            ]
            if change_ids:
                placeholders = ",".join("?" for _ in change_ids)
                connection.execute(
                    f"UPDATE task_revisions SET change_request_id=NULL "
                    f"WHERE change_request_id IN ({placeholders})",
                    change_ids,
                )
                connection.execute(
                    f"DELETE FROM task_change_requests WHERE id IN ({placeholders})",
                    change_ids,
                )
            connection.execute(
                "UPDATE task_change_requests SET result_task_id=NULL WHERE result_task_id=?",
                (task_id,),
            )

            connection.execute(
                "DELETE FROM task_relations WHERE source_task_id=? OR target_task_id=?",
                (task_id, task_id),
            )
            connection.execute("DELETE FROM reviews WHERE task_id=?", (task_id,))
            connection.execute(
                "DELETE FROM task_conversations WHERE task_id=?", (task_id,)
            )
            connection.execute(
                "DELETE FROM location_analyses WHERE task_id=?", (task_id,)
            )
            connection.execute(
                "DELETE FROM native_dispatches WHERE entity_type='task' AND entity_id=?",
                (task_id,),
            )
            connection.execute(
                "DELETE FROM integration_outbox WHERE entity_type='task' AND entity_id=?",
                (task_id,),
            )
            connection.execute(
                "DELETE FROM events WHERE entity_type='task' AND entity_id=?",
                (task_id,),
            )

            if run_ids:
                placeholders = ",".join("?" for _ in run_ids)
                connection.execute(
                    f"DELETE FROM location_analyses "
                    f"WHERE delivery_run_id IN ({placeholders})",
                    run_ids,
                )
                connection.execute(
                    f"UPDATE task_conversations SET run_id=NULL "
                    f"WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                connection.execute(
                    f"UPDATE task_runs SET parent_run_id=NULL "
                    f"WHERE parent_run_id IN ({placeholders}) AND task_id!=?",
                    (*run_ids, task_id),
                )
                connection.execute(
                    f"UPDATE task_runs SET delivery_run_id=NULL "
                    f"WHERE delivery_run_id IN ({placeholders}) AND task_id!=?",
                    (*run_ids, task_id),
                )
                connection.execute(
                    f"UPDATE execution_batches SET owner_run_id=NULL "
                    f"WHERE owner_run_id IN ({placeholders})",
                    run_ids,
                )
                connection.execute(
                    f"UPDATE execution_batches SET delivery_run_id=NULL "
                    f"WHERE delivery_run_id IN ({placeholders})",
                    run_ids,
                )
                connection.execute(
                    f"DELETE FROM native_dispatches WHERE run_id IN ({placeholders})",
                    run_ids,
                )
                connection.execute(
                    f"DELETE FROM events WHERE entity_type='run' "
                    f"AND entity_id IN ({placeholders})",
                    run_ids,
                )
                connection.execute(
                    f"DELETE FROM task_runs WHERE id IN ({placeholders})",
                    run_ids,
                )

            connection.execute("DELETE FROM tasks WHERE id=?", (task_id,))
            self._event(
                connection,
                "task",
                task_id,
                "deleted",
                {"title": task["title"], "codex_sessions_preserved": True},
            )
        contract = json.loads(task["implementation_contract"] or "{}")
        self._delete_unreferenced_visuals(contract.get("visual_references") or [])
        return {"status": "deleted", "task_id": task_id}

    def redecompose_requirement(self, requirement_id: str) -> dict[str, Any]:
        """Reset a requirement for a fresh decomposition without duplicating active work."""
        replaceable_statuses = {"draft", "ready", "failed", "cancelled"}
        with self.db.transaction() as connection:
            requirement = connection.execute(
                "SELECT * FROM requirements WHERE id=?", (requirement_id,),
            ).fetchone()
            if not requirement:
                raise KeyError(f"Requirement not found: {requirement_id}")
            if requirement["source_type"] == "web_task":
                raise ValueError("Page-created task intake cannot be redecomposed")
            if requirement["status"] == "decomposing":
                raise ValueError("Requirement decomposition is already running")
            if not str(requirement["project"] or "").strip():
                raise ValueError("Requirement project is required before redecomposition")

            child_tasks = connection.execute(
                "SELECT id, status FROM tasks WHERE requirement_id=? ORDER BY id",
                (requirement_id,),
            ).fetchall()
            non_replaceable = [
                row["id"] for row in child_tasks
                if row["status"] not in replaceable_statuses
            ]
            if non_replaceable:
                raise ValueError(
                    "Requirement has child tasks that already started: "
                    + ", ".join(non_replaceable)
                )

            child_ids = [row["id"] for row in child_tasks]
            if child_ids:
                placeholders = ",".join("?" for _ in child_ids)
                active_run = connection.execute(
                    f"""SELECT task_id FROM task_runs
                        WHERE task_id IN ({placeholders})
                          AND status IN ('awaiting_thread','running') LIMIT 1""",
                    child_ids,
                ).fetchone()
                if active_run:
                    raise ValueError(
                        f"Requirement child task still has an active run: {active_run['task_id']}"
                    )
                connection.execute(
                    f"""UPDATE tasks SET status='cancelled', status_started_at=CURRENT_TIMESTAMP,
                        auto_dispatch=0, requirement_id=NULL, requirement_task_key=NULL,
                        last_failure_reason='已由需求重新拆解替换',
                        updated_at=CURRENT_TIMESTAMP
                        WHERE id IN ({placeholders})""",
                    child_ids,
                )

            connection.execute(
                "DELETE FROM requirement_decomposition_runs WHERE requirement_id=?",
                (requirement_id,),
            )
            connection.execute(
                """UPDATE requirements SET status='ready', auto_dispatch=1,
                   decomposition_plan='[]', decomposition_attempts=0,
                   last_decomposition_error='', decomposed_at=NULL,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (requirement_id,),
            )
            self._event(
                connection, "requirement", requirement_id, "redecomposition_requested",
                {"replaced_task_ids": child_ids},
            )
            self._request_schedule(
                connection, "requirement_redecomposition_requested",
                "requirement", requirement_id,
            )
        for child_id in child_ids:
            self.release_visuals_for_terminal_task(child_id)
        return {
            **self.get_requirement(requirement_id),
            **self._controller_kickoff_contract(True),
        }

    def renew_requirement_decomposition(
        self, requirement_id: str, run_id: str, lease_seconds: int = 1800,
    ) -> dict[str, Any]:
        lease_seconds = max(300, min(int(lease_seconds), 7200))
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE requirement_decomposition_runs
                   SET lease_expires_at=datetime('now', ?), updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND requirement_id=? AND status='running'""",
                (f"+{lease_seconds} seconds", run_id, requirement_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("An active requirement decomposition run is required")
        return {"requirement_id": requirement_id, "run_id": run_id, "status": "running"}

    def submit_requirement_decomposition(
        self, requirement_id: str, run_id: str, child_tasks: list[dict[str, Any]],
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            requirement_row = connection.execute(
                "SELECT * FROM requirements WHERE id=?", (requirement_id,),
            ).fetchone()
            run = connection.execute(
                """SELECT * FROM requirement_decomposition_runs
                   WHERE id=? AND requirement_id=?""",
                (run_id, requirement_id),
            ).fetchone()
            if not requirement_row or not run:
                raise ValueError("A decomposition run belonging to the requirement is required")
            if run["status"] == "completed":
                return self.get_requirement(requirement_id)
            if run["status"] != "running" or requirement_row["status"] != "decomposing":
                raise ValueError("An active requirement decomposition run is required")

        specs = child_tasks
        if not isinstance(specs, list) or not specs or any(
            not isinstance(item, dict) for item in specs
        ):
            raise ValueError("child_tasks must be a non-empty array of objects")
        direct_task_intake = requirement_row["source_type"] == "web_task"
        if direct_task_intake and (
            len(specs) != 1 or str(specs[0].get("key") or "").strip() != "direct"
        ):
            raise ValueError("A page-created task must produce exactly the direct task")
        requirement_modules = json.loads(requirement_row["modules"] or "[]")
        requirement_out_of_scope = json.loads(requirement_row["out_of_scope"] or "[]")
        requirement_visual_references = json.loads(
            requirement_row["visual_references"] or "[]"
        )

        # Validate the entire decomposition before creating any child. A
        # requirement run is submitted once, so a contract error in a later
        # child must not leave earlier children partially persisted.
        preflight_keys: set[str] = set()
        for position, spec in enumerate(specs, 1):
            key = str(spec.get("key") or position).strip()
            if not key or key in preflight_keys:
                raise ValueError("Every decomposed task requires a unique non-empty key")
            preflight_keys.add(key)
            if not str(spec.get("title") or "").strip() or not str(spec.get("goal") or "").strip():
                raise ValueError("Every decomposed task requires title and goal")
            if not str(spec.get("analysis_id") or "").strip():
                raise ValueError("Every decomposed task requires analysis_id")
            evidence = spec.get("location_evidence")
            if not isinstance(evidence, dict) or not evidence:
                raise ValueError("Every decomposed task requires location_evidence")
            targets = spec.get("targets") or []
            if not isinstance(targets, list):
                raise ValueError("Every decomposed task targets must be an array")
            if any(not isinstance(target, dict) for target in targets):
                raise ValueError("Every decomposed task target must be an object")
            if not isinstance(spec.get("acceptance_plan"), list) or not spec["acceptance_plan"]:
                raise ValueError("Every decomposed task requires non-empty acceptance_plan")
            review_checks = spec.get("review_checks")
            if review_checks is not None and not isinstance(review_checks, list):
                raise ValueError("Every decomposed task review_checks must be an array")
            quality_gates = spec.get("quality_gates")
            if quality_gates is None:
                raise ValueError(
                    "Every decomposed task requires quality_gates to classify the task"
                )
            normalized_gates = self._normalize_quality_gates(quality_gates)
            if normalized_gates["code_review"]["required"] and not targets:
                raise ValueError(
                    "Code-changing decomposed tasks require non-empty targets"
                )
            if normalized_gates["code_review"]["required"] and any(
                not target.get("tasks") for target in targets
            ):
                raise ValueError(
                    "Every code-changing decomposed task target requires tasks"
                )
            project = self._require_project_directory(
                str(spec.get("project") or requirement_row["project"] or "")
            )
            try:
                self._validate_connected_location_evidence(project, evidence)
            except ValueError as exc:
                raise ValueError(
                    f"Decomposed task {key} has invalid location_evidence: {exc}"
                ) from exc
            dependencies = spec.get("depends_on") or []
            if not isinstance(dependencies, list):
                raise ValueError("depends_on must be an array of task keys")

        for position, spec in enumerate(specs, 1):
            for dependency in spec.get("depends_on") or []:
                if str(dependency).strip() not in preflight_keys:
                    raise ValueError(f"Unknown decomposed task dependency: {dependency}")

        created: dict[str, str] = {}
        desired_auto_dispatch: dict[str, bool] = {}
        seen_keys: set[str] = set()
        for position, spec in enumerate(specs, 1):
            key = str(spec.get("key") or position).strip()
            if not key or key in seen_keys:
                raise ValueError("Every decomposed task requires a unique non-empty key")
            seen_keys.add(key)
            desired_auto_dispatch[key] = (
                bool(requirement_row["auto_dispatch"])
                if direct_task_intake
                else bool(spec.get("auto_dispatch", True))
            )
            with self.db.connection() as connection:
                existing = connection.execute(
                    "SELECT id, status, type FROM tasks WHERE requirement_id=? AND requirement_task_key=?",
                    (requirement_id, key),
                ).fetchone()
            existing_task_id = (
                str(existing["id"])
                if existing and direct_task_intake and existing["status"] == "draft"
                else None
            )
            if existing and not existing_task_id:
                created[key] = existing["id"]
                continue
            title = str(
                requirement_row["title"] if direct_task_intake else spec.get("title") or ""
            ).strip()
            child_goal = str(
                requirement_row["goal"] if direct_task_intake else spec.get("goal") or ""
            ).strip()
            if not title or not child_goal:
                raise ValueError("Every decomposed task requires title and goal")
            targets = spec.get("targets") or []
            acceptance_plan = spec.get("acceptance_plan")
            quality_gates = spec.get("quality_gates")
            if quality_gates is None:
                raise ValueError(
                    "Every decomposed task requires quality_gates to classify the task"
                )
            normalized_gates = self._normalize_quality_gates(quality_gates)
            requires_changes = normalized_gates["code_review"]["required"]
            review_checks = (
                spec["review_checks"]
                if "review_checks" in spec
                else (default_code_review_checks() if requires_changes else [])
            )
            if not str(spec.get("analysis_id") or "").strip():
                raise ValueError("Every decomposed task requires analysis_id")
            if not isinstance(spec.get("location_evidence"), dict) or not spec["location_evidence"]:
                raise ValueError("Every decomposed task requires location_evidence")
            if not isinstance(targets, list):
                raise ValueError("Every decomposed task targets must be an array")
            if any(not isinstance(target, dict) for target in targets):
                raise ValueError("Every decomposed task target must be an object")
            if not isinstance(acceptance_plan, list) or not acceptance_plan:
                raise ValueError("Every decomposed task requires non-empty acceptance_plan")
            if not isinstance(review_checks, list):
                raise ValueError("Every decomposed task review_checks must be an array")
            if requires_changes and not targets:
                raise ValueError(
                    "Code-changing decomposed tasks require non-empty targets"
                )
            if requires_changes and any(not target.get("tasks") for target in targets):
                raise ValueError(
                    "Every code-changing decomposed task target requires tasks"
                )
            modules = list(dict.fromkeys([*requirement_modules, *(spec.get("modules") or [])]))
            scope = list(dict.fromkeys(spec.get("scope") or [child_goal]))
            out_of_scope = list(dict.fromkeys([*requirement_out_of_scope, *(spec.get("out_of_scope") or [])]))
            normalized_plan = [dict(item) for item in acceptance_plan]
            result = self.finalize_task_intake({
                "intake_kind": "task",
                "analysis_id": spec["analysis_id"],
                "title": title,
                "project": str(spec.get("project") or requirement_row["project"] or ""),
                "modules": modules,
                # A decomposed task owns only its declared child outcome.
                # Requirement-wide scope and acceptance stay on the parent;
                # copying them here makes every sibling responsible for the
                # entire requirement and creates duplicate acceptance bugs.
                "goal": child_goal,
                "scope": scope,
                "out_of_scope": out_of_scope,
                "priority": str(spec.get("priority") or requirement_row["priority"] or "P2"),
                "source_thread_id": requirement_row["source_thread_id"],
                "type": str(spec.get("type") or (existing["type"] if existing else "feature")),
                "token_budget": int(spec.get("token_budget") or self.task_token_budget()),
                # Keep every child non-dispatchable until the whole dependency
                # graph has passed validation and is committed below.
                "auto_dispatch": False,
                "location_summary": str(spec.get("location_summary") or "需求拆解子任务定位已确认"),
                "agent_id": str(spec.get("agent_id") or "requirement-decomposer"),
                "location_evidence": spec["location_evidence"],
                "targets": targets,
                "review_checks": review_checks,
                "quality_gates": normalized_gates,
                "acceptance_plan": normalized_plan,
                "dependency_analysis": {"decision": "independent"},
                "visual_references": requirement_visual_references,
            }, existing_task_id=existing_task_id)
            task_id = str(result.get("task_id") or "")
            if not task_id or result.get("task_status") != "ready":
                raise ValueError("Decomposed task did not pass normal ready task intake")
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE tasks SET requirement_id=?, requirement_task_key=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (requirement_id, key, task_id),
                )
            created[key] = task_id

        with self.db.transaction() as connection:
            current_run = connection.execute(
                """SELECT status FROM requirement_decomposition_runs
                   WHERE id=? AND requirement_id=?""",
                (run_id, requirement_id),
            ).fetchone()
            if not current_run or current_run["status"] != "running":
                raise ValueError("Requirement decomposition run is no longer active")
            for position, spec in enumerate(specs, 1):
                source_id = created[str(spec.get("key") or position).strip()]
                dependencies = spec.get("depends_on") or []
                if not isinstance(dependencies, list):
                    raise ValueError("depends_on must be an array of task keys")
                for dependency in dependencies:
                    dependency_id = created.get(str(dependency).strip())
                    if not dependency_id:
                        raise ValueError(f"Unknown decomposed task dependency: {dependency}")
                    insert_relation(
                        connection, source_id, dependency_id, "depends_on",
                        f"需求 {requirement_id} 拆解依赖",
                    )
            for key, task_id in created.items():
                connection.execute(
                    "UPDATE tasks SET auto_dispatch=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (int(desired_auto_dispatch[key]), task_id),
                )
            connection.execute(
                """UPDATE requirement_decomposition_runs SET status='completed',
                   completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (run_id,),
            )
            connection.execute(
                """UPDATE requirements SET status='decomposed', decomposition_plan=?,
                   last_decomposition_error='', decomposed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (json.dumps(specs, ensure_ascii=False), requirement_id),
            )
            self._event(connection, "requirement", requirement_id, "decomposed", {"run_id": run_id, "task_ids": list(created.values())})
        return self.get_requirement(requirement_id)

    def fail_requirement_decomposition(
        self, requirement_id: str, run_id: str, error: str,
    ) -> dict[str, Any]:
        with self.db.transaction() as connection:
            requirement = connection.execute(
                "SELECT * FROM requirements WHERE id=?", (requirement_id,),
            ).fetchone()
            run = connection.execute(
                """SELECT * FROM requirement_decomposition_runs
                   WHERE id=? AND requirement_id=?""",
                (run_id, requirement_id),
            ).fetchone()
            if not requirement or not run:
                raise ValueError(
                    "A decomposition run belonging to the requirement is required"
                )
            if run["status"] != "running" or requirement["status"] != "decomposing":
                raise ValueError("An active requirement decomposition run is required")
            reason = str(error or "需求拆解失败").strip()
            exhausted = (
                int(run["attempt"] or 0)
                >= REQUIREMENT_DECOMPOSITION_ATTEMPT_LIMIT
            )
            connection.execute(
                """UPDATE requirement_decomposition_runs SET status='failed', error=?,
                   completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (reason, run_id),
            )
            connection.execute(
                """UPDATE requirements SET status=?, auto_dispatch=?,
                   last_decomposition_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    "failed" if exhausted else "ready",
                    0 if exhausted else int(bool(requirement["auto_dispatch"])),
                    reason,
                    requirement_id,
                ),
            )
            self._event(
                connection, "requirement", requirement_id, "decomposition_failed",
                {
                    "run_id": run_id,
                    "error": reason,
                    "attempt": int(run["attempt"] or 0),
                    "retry_exhausted": exhausted,
                },
            )
        return self.get_requirement(requirement_id)

    def _claim_requirement_decomposition(
        self, worker_id: str, project: str | None, lease_seconds: int,
    ) -> dict[str, Any] | None:
        with self.db.transaction() as connection:
            if connection.execute("SELECT 1 FROM mobile_messages WHERE status IN ('starting','running','uncertain') LIMIT 1").fetchone():
                return None
            connection.execute(
                """UPDATE requirement_decomposition_runs SET status='failed',
                   error='decomposition lease expired', completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE status='running' AND lease_expires_at<=CURRENT_TIMESTAMP"""
            )
            connection.execute(
                """UPDATE requirements SET status='ready',
                   last_decomposition_error='decomposition lease expired',
                   updated_at=CURRENT_TIMESTAMP
                   WHERE status='decomposing' AND NOT EXISTS(
                     SELECT 1 FROM requirement_decomposition_runs r
                     WHERE r.requirement_id=requirements.id AND r.status='running')"""
            )
            requirement_filters = ["status='ready'", "auto_dispatch=1"]
            requirement_values: list[Any] = []
            if project:
                requirement_filters.append("project=?")
                requirement_values.append(self._normalize_project(project))
            requirement_row = connection.execute(
                f"""SELECT * FROM requirements WHERE {' AND '.join(requirement_filters)}
                    ORDER BY CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1
                             WHEN 'P2' THEN 2 ELSE 3 END, created_at, id LIMIT 1""",
                requirement_values,
            ).fetchone()
            if requirement_row:
                attempt = int(requirement_row["decomposition_attempts"] or 0) + 1
                run_id = self.db.next_id(connection, "RDRUN")
                lease_token = uuid.uuid4().hex
                modifier = f"+{lease_seconds} seconds"
                connection.execute(
                    """INSERT INTO requirement_decomposition_runs(
                           id, requirement_id, attempt, status, claimed_by,
                           lease_token, lease_expires_at
                       ) VALUES(?, ?, ?, 'running', ?, ?, datetime('now', ?))""",
                    (run_id, requirement_row["id"], attempt, worker_id, lease_token, modifier),
                )
                connection.execute(
                    """UPDATE requirements SET status='decomposing',
                       decomposition_attempts=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (attempt, requirement_row["id"]),
                )
                requirement = self._decode_requirement(requirement_row)
                requirement["status"] = "decomposing"
                requirement["decomposition_attempts"] = attempt
                return {
                    "kind": "requirement_decomposition",
                    "requirement": requirement,
                    "run": {"id": run_id, "run_type": "requirement_decomposition", "status": "running"},
                    "lease_token": lease_token,
                    "dispatch_prompt": (
                        "将该需求拆解为可独立执行且具有明确依赖关系的 ready 任务；"
                        "完成后调用 submit_requirement_decomposition。"
                    ),
                }
        return None
