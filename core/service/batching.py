from __future__ import annotations

import json
import uuid
from typing import Any

from ..run_context import group_verification_checks
from .domain import decode_row


def _review_check_label(check: Any) -> str:
    if isinstance(check, dict):
        return str(
            check.get("id") or check.get("criterion") or check.get("description") or ""
        ).strip()
    return str(check or "").strip()


def batch_item_label(task_id: str, value: str) -> str:
    return f"[{task_id}] {str(value).strip()}"


class TaskBatchMixin:
    """Group compatible tasks into one development conversation and review gate."""

    def _ensure_execution_batch(
        self,
        connection: Any,
        task: dict[str, Any],
        run_id: str,
        run_type: str,
    ) -> str:
        if not self._quality_gate_required(task, "code_review"):
            # Tasks that skip the only independent review gate complete in development.
            return ""
        existing = connection.execute(
            """SELECT batch.* FROM execution_batches batch
               JOIN execution_batch_tasks member ON member.batch_id=batch.id
               WHERE member.task_id=? AND member.role='owner'
                 AND batch.state NOT IN ('done','cancelled')
               ORDER BY batch.created_at DESC LIMIT 1""",
            (task["id"],),
        ).fetchone()
        if existing:
            batch_id = existing["id"]
            admission_open = int(run_type == "execution")
            connection.execute(
                """UPDATE execution_batches
                   SET owner_run_id=?, state='development', admission_open=?,
                       thread_id='', active_turn_id='', sealed_at=NULL,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (run_id, admission_open, batch_id),
            )
        else:
            batch_id = self.db.next_id(connection, "BATCH")
            setting = connection.execute(
                "SELECT value FROM system_settings WHERE key='max_batch_appended_tasks'"
            ).fetchone()
            try:
                max_appended = max(0, min(int(setting["value"]), 20)) if setting else 3
            except (TypeError, ValueError):
                max_appended = 3
            connection.execute(
                """INSERT INTO execution_batches(
                       id, project, owner_task_id, owner_run_id, state,
                       max_appended_tasks, admission_open
                   ) VALUES(?, ?, ?, ?, 'development', ?, 1)""",
                (batch_id, task.get("project") or "", task["id"], run_id, max_appended),
            )
            connection.execute(
                """INSERT INTO execution_batch_tasks(
                       batch_id, task_id, role, join_order, joined_revision
                   ) VALUES(?, ?, 'owner', 0, 1)""",
                (batch_id, task["id"]),
            )
        connection.execute(
            "INSERT OR IGNORE INTO execution_batch_runs(batch_id, run_id) VALUES(?, ?)",
            (batch_id, run_id),
        )
        return batch_id

    def _try_join_open_batch(self, connection: Any, task_id: str) -> str:
        # Parallel development owns one isolated worktree per task. Appending a
        # second task into the owner's conversation would collapse that isolation.
        if self.parallel_development_enabled():
            return ""
        task = connection.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if (
            not task
            or task["status"] != "ready"
            or not task["project"]
            or not int(task["auto_dispatch"])
        ):
            return ""
        try:
            dependency = json.loads(task["dependency_analysis"] or "{}")
            review = json.loads(task["review_contract"] or "{}")
        except (TypeError, json.JSONDecodeError):
            return ""
        if (
            str(dependency.get("decision") or "") != "independent"
            or bool(dependency.get("conflicts_tasks"))
            or not self._quality_gate_required({"review_contract": review}, "code_review")
        ):
            return ""
        batch = connection.execute(
            """SELECT batch.* FROM execution_batches batch
               JOIN tasks owner ON owner.id=batch.owner_task_id
               LEFT JOIN task_runs active ON active.id=batch.owner_run_id
               WHERE batch.project=? AND batch.admission_open=1
                 AND batch.state IN ('development','regrouping')
                 AND batch.appended_count < batch.max_appended_tasks
                 AND (
                   (batch.state='development' AND active.status IN ('awaiting_thread','running'))
                   OR (batch.state='regrouping' AND owner.status='rework')
                 )
               ORDER BY batch.created_at LIMIT 1""",
            (task["project"],),
        ).fetchone()
        if not batch:
            return ""
        revision = int(batch["revision"]) + 1
        join_order = int(batch["appended_count"]) + 1
        connection.execute(
            """INSERT INTO execution_batch_tasks(
                   batch_id, task_id, role, join_order, joined_revision
               ) VALUES(?, ?, 'appended', ?, ?)""",
            (batch["id"], task_id, join_order, revision),
        )
        connection.execute(
            """UPDATE execution_batches
               SET revision=?, appended_count=appended_count+1,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (revision, batch["id"]),
        )
        connection.execute(
            """UPDATE tasks SET status='implementing', auto_dispatch=0,
               assigned_to='execution-batch', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (task_id,),
        )
        connection.execute(
            """INSERT INTO batch_steer_events(batch_id, task_id, revision)
               VALUES(?, ?, ?)""",
            (batch["id"], task_id, revision),
        )
        self._event(
            connection,
            "task",
            task_id,
            "execution_batch_joined",
            {"batch_id": batch["id"], "revision": revision, "join_order": join_order},
        )
        self._event(
            connection,
            "batch",
            batch["id"],
            "task_joined",
            {"task_id": task_id, "revision": revision, "join_order": join_order},
        )
        return str(batch["id"])

    def _batch_for_run(self, run_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            row = connection.execute(
                """SELECT batch.* FROM execution_batches batch
                   JOIN execution_batch_runs mapping ON mapping.batch_id=batch.id
                   WHERE mapping.run_id=?""",
                (run_id,),
            ).fetchone()
        return dict(row) if row else None

    def _batch_for_task(self, task_id: str) -> dict[str, Any] | None:
        with self.db.connection() as connection:
            row = connection.execute(
                """SELECT batch.* FROM execution_batches batch
                   JOIN execution_batch_tasks member ON member.batch_id=batch.id
                   WHERE member.task_id=? ORDER BY batch.created_at DESC LIMIT 1""",
                (task_id,),
            ).fetchone()
        return dict(row) if row else None

    def _batch_member_tasks(self, batch_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            return self._batch_member_tasks_in(connection, batch_id)

    @staticmethod
    def _batch_member_tasks_in(connection: Any, batch_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT task.*, member.role AS batch_role,
                      member.join_order, member.joined_revision
               FROM execution_batch_tasks member
               JOIN tasks task ON task.id=member.task_id
               WHERE member.batch_id=? ORDER BY member.join_order""",
            (batch_id,),
        ).fetchall()
        return [decode_row(row) for row in rows]

    @staticmethod
    def _attach_batch_run(
        connection: Any, batch_id: str, run_id: str, state: str
    ) -> None:
        connection.execute(
            "INSERT OR IGNORE INTO execution_batch_runs(batch_id, run_id) VALUES(?, ?)",
            (batch_id, run_id),
        )
        connection.execute(
            """UPDATE execution_batches SET owner_run_id=?, state=?, admission_open=0,
               active_turn_id='', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (run_id, state, batch_id),
        )

    def execution_batch(self, task_id: str) -> dict[str, Any] | None:
        batch = self._batch_for_task(task_id)
        if not batch:
            return None
        batch["tasks"] = [
            {
                "id": task["id"],
                "title": task["title"],
                "status": task["status"],
                "role": task["batch_role"],
                "join_order": task["join_order"],
                "joined_revision": task["joined_revision"],
            }
            for task in self._batch_member_tasks(batch["id"])
        ]
        return batch

    @staticmethod
    def _merge_targets(targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        merged: dict[str, dict[str, Any]] = {}
        for target in targets:
            file = str(target.get("file") or "").strip()
            if not file:
                continue
            current = merged.setdefault(file, {
                "file": file,
                "mode": str(target.get("mode") or "modify"),
                "symbols": [],
                "reasons": [],
                "tasks": [],
            })
            for symbol in target.get("symbols") or []:
                value = str(symbol).strip()
                if value and value not in current["symbols"]:
                    current["symbols"].append(value)
            reason = str(target.get("reason") or "").strip()
            if reason and reason not in current["reasons"]:
                current["reasons"].append(reason)
            for task in target.get("tasks") or []:
                if isinstance(task, dict) and task not in current["tasks"]:
                    current["tasks"].append(dict(task))
        return list(merged.values())

    def _batch_metadata(self, batch: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": batch["id"],
            "revision": int(batch["revision"]),
            "max_appended_tasks": int(batch["max_appended_tasks"]),
            "appended_count": int(batch["appended_count"]),
            "admission_open": bool(batch["admission_open"]),
        }

    def _merge_batch_execution_context(
        self, context: dict[str, Any], batch_id: str
    ) -> dict[str, Any]:
        batch = self._batch_for_task(self._batch_member_tasks(batch_id)[0]["id"])
        if not batch:
            return context
        if int(batch.get("appended_count") or 0) == 0:
            return context
        tasks = self._batch_member_tasks(batch_id)
        targets: list[dict[str, Any]] = []
        plans: list[dict[str, Any]] = []
        task_contexts = []
        for task in tasks:
            task_contexts.append({
                key: task.get(key)
                for key in ("id", "title", "goal", "scope", "out_of_scope")
                if task.get(key) not in (None, "", [], {})
            })
            targets.extend((task.get("location_context") or {}).get("targets") or [])
            for item in task.get("acceptance_plan") or []:
                plans.append({
                    **item,
                    "task_id": task["id"],
                    "criterion": batch_item_label(task["id"], item.get("criterion") or ""),
                })
        return {
            "batch": self._batch_metadata(batch),
            "tasks": task_contexts,
            "targets": self._merge_targets(targets),
            "verify": group_verification_checks(plans),
        }

    def _merge_batch_code_review_context(
        self, context: dict[str, Any], batch_id: str
    ) -> tuple[dict[str, Any], list[str]]:
        batch = self._batch_for_task(self._batch_member_tasks(batch_id)[0]["id"])
        if not batch:
            return context, list(context.get("review_checks") or [])
        tasks = self._batch_member_tasks(batch_id)
        targets: list[dict[str, Any]] = []
        checks: list[str] = []
        acceptance: list[dict[str, Any]] = []
        acceptance_criteria: list[str] = []
        task_contexts = []
        for task in tasks:
            task_contexts.append({
                key: task.get(key)
                for key in ("id", "title", "goal", "scope", "out_of_scope", "acceptance_criteria")
                if task.get(key) not in (None, "", [], {})
            })
            implementation = task.get("implementation_contract") or {}
            targets.extend(implementation.get("targets") or [])
            checks.extend(
                batch_item_label(task["id"], _review_check_label(item))
                for item in (task.get("review_contract") or {}).get("checks") or []
                if _review_check_label(item)
            )
            task_acceptance_labels: list[str] = []
            for item in task.get("acceptance_plan") or []:
                label = batch_item_label(task["id"], item.get("criterion") or "")
                task_acceptance_labels.append(label)
                acceptance_criteria.append(label)
                acceptance.append({**item, "task_id": task["id"], "criterion": label})
            checks.extend(task_acceptance_labels)
        checks = list(dict.fromkeys(checks))
        return ({
            **context,
            "batch": self._batch_metadata(batch),
            "tasks": task_contexts,
            "implementation": {"targets": self._merge_targets(targets)},
            "review_checks": checks,
            "acceptance_criteria": acceptance_criteria,
            "acceptance": acceptance,
        }, checks)

    def _batch_review_items(self, task_id: str) -> list[str]:
        batch = self._batch_for_task(task_id)
        if not batch:
            return []
        return list(dict.fromkeys([
            batch_item_label(task["id"], item)
            for task in self._batch_member_tasks(batch["id"])
            for item in [
                *[
                    _review_check_label(check)
                    for check in (task.get("review_contract") or {}).get("checks") or []
                    if _review_check_label(check)
                ],
                *[str(value).strip() for value in task.get("acceptance_criteria") or [] if str(value).strip()],
            ]
        ]))

    def _batch_acceptance_items(self, task_id: str) -> list[str]:
        batch = self._batch_for_task(task_id)
        if not batch:
            return []
        return [
            batch_item_label(task["id"], criterion)
            for task in self._batch_member_tasks(batch["id"])
            for criterion in task.get("acceptance_criteria") or []
        ]

    def _create_batch_member_deliveries(
        self,
        connection: Any,
        batch: dict[str, Any],
        owner_task: dict[str, Any],
        owner_run: dict[str, Any],
        delivery_summary: str,
        verification_result: str,
        changed_locations: list[dict[str, Any]],
        acceptance_evidence: list[dict[str, Any]],
        artifact_snapshot: dict[str, Any],
    ) -> None:
        for task in self._batch_member_tasks_in(connection, batch["id"]):
            if task["id"] == owner_task["id"]:
                continue
            prefix = f"[{task['id']}] "
            evidence = []
            for item in acceptance_evidence:
                criterion = str(item.get("criterion") or "")
                if criterion.startswith(prefix):
                    evidence.append({**item, "criterion": criterion[len(prefix):]})
            run_type = (
                "rework"
                if owner_run["run_type"] == "rework"
                else ("bugfix" if task.get("type") == "bug" else "execution")
            )
            attempt = connection.execute(
                "SELECT COALESCE(MAX(attempt),0)+1 value FROM task_runs WHERE task_id=?",
                (task["id"],),
            ).fetchone()["value"]
            member_run_id = self.db.next_id(connection, "RUN")
            connection.execute(
                """INSERT INTO task_runs(
                       id, task_id, parent_run_id, run_type, attempt, status,
                       claimed_by, lease_token, lease_expires_at, delivery_summary,
                       verification_result, changed_locations, acceptance_evidence,
                       artifact_snapshot, completed_at
                   ) VALUES(?, ?, ?, ?, ?, 'waiting_review', 'execution-batch', ?,
                            CURRENT_TIMESTAMP, ?, ?, ?, ?, ?, NULL)""",
                (
                    member_run_id,
                    task["id"],
                    task.get("primary_run_id"),
                    run_type,
                    attempt,
                    uuid.uuid4().hex,
                    delivery_summary,
                    verification_result,
                    json.dumps(changed_locations, ensure_ascii=False),
                    json.dumps(evidence, ensure_ascii=False),
                    json.dumps(artifact_snapshot, ensure_ascii=False),
                ),
            )
            connection.execute(
                """UPDATE tasks SET status='code_review', primary_run_id=?,
                   delivery_summary=?, verification_result=?, active_run_id=NULL,
                   assigned_to=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (
                    member_run_id,
                    delivery_summary,
                    verification_result,
                    task["id"],
                ),
            )
            self._event(
                connection,
                "run",
                member_run_id,
                "batch_delivery_submitted",
                {"task_id": task["id"], "batch_id": batch["id"]},
            )

    def _propagate_batch_review(
        self,
        connection: Any,
        batch: dict[str, Any],
        owner_task_id: str,
        verdict: str,
        reasons: list[str],
        passed_items: list[str],
        failed_criteria: list[str],
    ) -> list[str]:
        next_status = "rework" if verdict == "fail" else "done"
        batch_state = "regrouping" if verdict == "fail" else "done"
        completed_task_ids: list[str] = []
        for task in self._batch_member_tasks_in(connection, batch["id"]):
            if task["id"] == owner_task_id:
                continue
            prefix = f"[{task['id']}] "
            task_passed = [item[len(prefix):] for item in passed_items if item.startswith(prefix)]
            task_failed = [item[len(prefix):] for item in failed_criteria if item.startswith(prefix)]
            round_no = connection.execute(
                "SELECT COALESCE(MAX(round),0)+1 value FROM reviews WHERE task_id=?",
                (task["id"],),
            ).fetchone()["value"]
            connection.execute(
                """INSERT INTO reviews(task_id,round,verdict,reasons,passed_items,failed_criteria)
                   VALUES(?,?,?,?,?,?)""",
                (
                    task["id"], round_no, verdict,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(task_passed, ensure_ascii=False),
                    json.dumps(task_failed, ensure_ascii=False),
                ),
            )
            delivery_status = "review_failed" if verdict == "fail" else "completed"
            connection.execute(
                """UPDATE task_runs SET status=?, completed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='waiting_review'""",
                (delivery_status, task.get("primary_run_id")),
            )
            connection.execute(
                """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                   last_review_reasons=?, last_failed_criteria=?,
                   review_rework_count=review_rework_count+?, updated_at=CURRENT_TIMESTAMP
                   WHERE id=?""",
                (
                    next_status,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(task_failed, ensure_ascii=False),
                    int(verdict == "fail"),
                    task["id"],
                ),
            )
            self._event(
                connection, "task", task["id"], "code_reviewed",
                {"verdict": verdict, "reasons": reasons, "batch_id": batch["id"]},
            )
            if verdict == "pass":
                completed_task_ids.append(task["id"])
        connection.execute(
            """UPDATE execution_batches SET state=?, admission_open=?, owner_run_id=NULL,
               active_turn_id='', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
            (batch_state, int(verdict == "fail"), batch["id"]),
        )
        return completed_task_ids

    def bind_batch_turn(
        self, run_id: str, thread_id: str, turn_id: str, prompt_revision: int
    ) -> None:
        batch = self._batch_for_run(run_id)
        if not batch:
            return
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE execution_batches SET thread_id=?, active_turn_id=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (thread_id, turn_id, batch["id"]),
            )
            connection.execute(
                """UPDATE batch_steer_events SET status='sent', updated_at=CURRENT_TIMESTAMP
                   WHERE batch_id=? AND status='pending' AND revision<=?""",
                (batch["id"], int(prompt_revision)),
            )

    def pending_batch_steers(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT event.*, batch.thread_id, batch.active_turn_id
                   FROM batch_steer_events event
                   JOIN execution_batches batch ON batch.id=event.batch_id
                   WHERE event.status='pending' AND batch.state='development'
                     AND batch.thread_id!='' AND batch.active_turn_id!=''
                   ORDER BY event.created_at, event.id"""
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            task = self.get_task(item["task_id"])
            item["input"] = {
                "batch_id": item["batch_id"],
                "batch_revision": int(item["revision"]),
                "task": {
                    key: task.get(key)
                    for key in ("id", "title", "goal")
                    if task.get(key) not in (None, "", [], {})
                },
                "constraints": {
                    key: task.get(key)
                    for key in ("scope", "out_of_scope")
                    if task.get(key) not in (None, "", [], {})
                },
                "targets": (task.get("implementation_contract") or {}).get("targets") or [],
                "verify": group_verification_checks(task.get("acceptance_plan") or []),
            }
            result.append(item)
        return result

    def mark_batch_steer_sent(self, event_id: int) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE batch_steer_events SET status='sent', attempts=attempts+1,
                   last_error='', updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (event_id,),
            )

    def mark_batch_steer_pending(self, event_id: int, error: str) -> None:
        with self.db.transaction() as connection:
            connection.execute(
                """UPDATE batch_steer_events SET attempts=attempts+1, last_error=?,
                   updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                (str(error or "")[:2000], event_id),
            )
