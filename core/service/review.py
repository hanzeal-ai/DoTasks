from __future__ import annotations

import json
import posixpath
import subprocess
import time
from typing import Any

from ..runtime import project_runtime_environment
from ..self_healing import (
    SELF_HEAL_ATTEMPT_LIMIT,
    classify_recoverable_failure,
    normalize_self_heal_locations,
)
from .domain import REVIEW_REWORK_LIMIT


class TaskReviewMixin:
    """Delivery validation and code-quality review decisions."""

    @staticmethod
    def _run_project_command(
        command: str, project: str, timeout_seconds: int,
    ) -> tuple[str, int | None, str, int]:
        started = time.monotonic()
        exit_code: int | None = None
        try:
            completed = subprocess.run(
                command,
                cwd=project,
                env=project_runtime_environment(project),
                shell=True,
                executable="/bin/zsh",
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                check=False,
            )
            exit_code = completed.returncode
            status = "passed" if completed.returncode == 0 else "failed"
            output = "\n".join(
                part for part in (completed.stdout, completed.stderr) if part
            ).strip()
        except subprocess.TimeoutExpired as exc:
            status = "timeout"
            output = "\n".join(
                str(part)
                for part in (
                    exc.stdout,
                    exc.stderr,
                    f"Timed out after {timeout_seconds}s",
                )
                if part
            )
        except OSError as exc:
            status = "error"
            output = str(exc)
        duration_ms = max(0, int((time.monotonic() - started) * 1000))
        return status, exit_code, output, duration_ms

    def _validate_changed_locations(
        self,
        task_id: str,
        changed_locations: list[dict[str, Any]],
        task_ids: list[str] | None = None,
    ) -> None:
        task_ids = task_ids or [task_id]
        placeholders = ",".join("?" for _ in task_ids)
        with self.db.connection() as connection:
            rows = connection.execute(
                f"SELECT file, symbol FROM task_targets WHERE task_id IN ({placeholders})",
                task_ids,
            ).fetchall()
        locked: dict[str, set[str]] = {}
        for row in rows:
            locked.setdefault(posixpath.normpath(row["file"]), set()).add(row["symbol"])
        for location in changed_locations:
            if not isinstance(location, dict):
                raise ValueError("Every changed location must be an object")
            file = self._normalize_target_file(location.get("file"))
            if not file or file not in locked:
                raise ValueError(f"Changed location is outside the task target lock: {file or '<missing>'}")
            allowed_symbols = locked[file]
            submitted_symbols = {str(value).strip() for value in location.get("symbols", []) if str(value).strip()}
            if "" not in allowed_symbols and (not submitted_symbols or not submitted_symbols.issubset(allowed_symbols)):
                raise ValueError(
                    f"Changed symbols are outside the task target lock: {file}; "
                    f"submitted={json.dumps(sorted(submitted_symbols), ensure_ascii=False)}; "
                    f"allowed={json.dumps(sorted(allowed_symbols), ensure_ascii=False)}. "
                    "Use the exact symbols from RUN_CONTEXT_JSON.targets "
                    "and retry the same active run."
                )

    def _validate_workspace_delta(
        self, task: dict[str, Any], run: dict[str, Any], changed_locations: list[dict[str, Any]],
        current: dict[str, Any] | None = None, task_ids: list[str] | None = None,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        task_ids = task_ids or [task["id"]]
        baseline = (run.get("context_snapshot") or {}).get("workspace_baseline") or {}
        if not baseline.get("available"):
            return current or {}
        workspace = workspace_path or task.get("project")
        current = current or self._workspace_state(workspace)
        if not current.get("available"):
            raise ValueError("Cannot verify the actual Git workspace changes")
        before = baseline.get("files") or {}
        after = current.get("files") or {}
        actual = {
            path for path in set(before) | set(after)
            if before.get(path) != after.get(path)
        }
        baseline_revision = str(baseline.get("revision") or "")
        current_revision = str(current.get("revision") or "")
        if baseline_revision and current_revision and baseline_revision != current_revision:
            committed = self._git(
                self._normalize_project(workspace), "diff", "--name-only", "-z",
                baseline_revision, current_revision, "--",
            )
            if committed.returncode != 0:
                raise ValueError("Cannot verify committed changes since the execution baseline")
            actual.update(
                posixpath.normpath(path)
                for path in committed.stdout.split("\0") if path.strip()
            )
        submitted = {
            posixpath.normpath(str(item.get("file") or "").strip())
            for item in changed_locations if str(item.get("file") or "").strip()
        }
        with self.db.connection() as connection:
            placeholders = ",".join("?" for _ in task_ids)
            locked = {
                posixpath.normpath(row["file"])
                for row in connection.execute(
                    f"SELECT file FROM task_targets WHERE task_id IN ({placeholders})",
                    task_ids,
                ).fetchall()
            }
        outside = actual - locked
        if outside:
            raise ValueError(f"Actual Git changes are outside the task target lock: {', '.join(sorted(outside))}")
        unreported = actual - submitted
        if unreported:
            raise ValueError(f"Actual Git changes were not reported: {', '.join(sorted(unreported))}")
        unsupported = submitted - actual
        if unsupported:
            raise ValueError(f"Reported changed locations have no Git workspace delta: {', '.join(sorted(unsupported))}")
        return current

    def submit_delivery(
        self,
        run_id: str,
        delivery_summary: str,
        verification_result: str,
        changed_locations: list[dict[str, Any]],
        acceptance_evidence: list[dict[str, Any]],
        token_used: int = 0,
        *,
        batch_revision: int | None = None,
        workspace_path: str | None = None,
    ) -> dict[str, Any]:
        run = self.get_run(run_id)
        task = self.get_task(run["task_id"])
        batch = self._batch_for_run(run_id)
        batch_tasks = self._batch_member_tasks(batch["id"]) if batch else [task]
        batch_task_ids = [item["id"] for item in batch_tasks]
        if batch and len(batch_tasks) > 1:
            current_revision = int(batch["revision"])
            if batch_revision is None or int(batch_revision) != current_revision:
                return {
                    "task": task,
                    "run": run,
                    "batch": self.execution_batch(task["id"]),
                    "continue_development": True,
                    "review_dispatch_required": False,
                }
        if run["run_type"] not in {"execution", "rework", "bugfix"} or run["status"] != "running":
            raise ValueError("Execution run must be active before delivery")
        if task["status"] not in {"investigating", "implementing", "rework"}:
            raise ValueError(f"Task cannot be delivered from {task['status']}")
        if task.get("active_run_id") != run_id:
            raise ValueError("Execution run is no longer the task's active run")
        token_used = int(token_used)
        if token_used < 0:
            raise ValueError("token_used must be non-negative")
        if not delivery_summary.strip() or not verification_result.strip():
            raise ValueError("delivery_summary and verification_result are required")
        projectless = (
            not str(task.get("project") or "").strip()
            and str(run.get("execution_environment") or "") == "projectless"
        )
        requires_code_review = self._quality_gate_required(task, "code_review")
        requires_changes = requires_code_review
        if not isinstance(changed_locations, list):
            raise ValueError("changed_locations is required")
        if requires_changes and not changed_locations:
            raise ValueError("Code-changing deliveries require changed_locations")
        if not requires_changes and changed_locations:
            raise ValueError("Read-only deliveries cannot report changed_locations")
        for location in changed_locations:
            if not location.get("file"):
                raise ValueError("Every changed location requires a file")
            location["file"] = self._normalize_target_file(location["file"])
            symbols = location.get("symbols", [])
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
                raise ValueError("Changed location symbols must be an array of strings")
            location["symbols"] = [symbol.strip() for symbol in symbols if symbol.strip()]
            location.setdefault("summary", "")
        if not projectless:
            self._validate_changed_locations(
                task["id"], changed_locations, batch_task_ids
            )
        execution_environment = str(run.get("execution_environment") or "local")
        execution_workspace = str(workspace_path or task.get("project") or "")
        if execution_environment == "worktree":
            execution_workspace = self._validate_execution_workspace(
                str(task.get("project") or ""), execution_workspace,
                require_worktree=True,
            )
        elif workspace_path:
            execution_workspace = self._validate_execution_workspace(
                str(task.get("project") or ""), execution_workspace,
                require_worktree=False,
            )
        workspace_state = (
            {
                "available": False,
                "reason": "projectless_task",
                "project": "",
                "files": {},
            }
            if projectless
            else self._workspace_state(execution_workspace)
        )
        if not projectless:
            self._validate_workspace_delta(
                task, run, changed_locations, workspace_state, batch_task_ids,
                execution_workspace,
            )
        if not isinstance(acceptance_evidence, list) or not acceptance_evidence or any(not isinstance(item, dict) for item in acceptance_evidence):
            raise ValueError("acceptance_evidence is required")
        criteria = set(
            self._batch_acceptance_items(task["id"])
            if batch and len(batch_tasks) > 1
            else task.get("acceptance_criteria", [])
        )
        covered = [item.get("criterion") for item in acceptance_evidence]
        if len(covered) != len(set(covered)):
            raise ValueError("Acceptance evidence criteria must not contain duplicates")
        covered_set = set(covered)
        missing_criteria = criteria - covered_set
        if missing_criteria:
            raise ValueError(f"Missing acceptance evidence for: {', '.join(sorted(missing_criteria))}")
        allowed_statuses = {"passed", "failed", "blocked", "pending"}
        invalid_evidence = [
            item.get("criterion") for item in acceptance_evidence
            if item.get("criterion") not in criteria
            or not str(item.get("evidence") or "").strip()
            or (
                item.get("status") not in (None, "")
                and str(item.get("status")).strip().lower() not in allowed_statuses
            )
        ]
        if invalid_evidence:
            raise ValueError("Every acceptance criterion requires an exact match, status, and non-empty evidence")
        if batch and len(batch_tasks) > 1:
            plan_by_criterion = {
                f"[{member['id']}] {str(plan.get('criterion') or '').strip()}": plan
                for member in batch_tasks
                for plan in member.get("acceptance_plan") or []
                if isinstance(plan, dict)
                and str(plan.get("criterion") or "").strip()
            }
        else:
            plan_by_criterion = {
                str(item.get("criterion") or ""): item
                for item in task.get("acceptance_plan") or []
                if isinstance(item, dict)
            }
        normalized_evidence: list[dict[str, Any]] = []
        incomplete_automated: list[str] = []
        incomplete_without_review: list[str] = []
        for item in acceptance_evidence:
            normalized = dict(item)
            criterion = str(normalized.get("criterion") or "")
            plan = plan_by_criterion.get(criterion, {})
            check_type = str(
                plan.get("check_type")
                or ("automated" if plan.get("command") else "static_review")
            )
            status = str(
                normalized.get("status")
                or ("passed" if check_type == "automated" else "pending")
            ).strip().lower()
            normalized["status"] = status
            if not requires_code_review and status != "passed":
                incomplete_without_review.append(criterion)
            if (
                check_type == "automated"
                and bool(plan.get("required", True))
                and status != "passed"
            ):
                incomplete_automated.append(criterion)
            normalized_evidence.append(normalized)
        if incomplete_automated:
            raise ValueError(
                "Required automated verification must pass before delivery: "
                + ", ".join(incomplete_automated)
            )
        if incomplete_without_review:
            raise ValueError(
                "Every acceptance criterion must pass during development when Code Review is skipped: "
                + ", ".join(incomplete_without_review)
            )
        acceptance_evidence = normalized_evidence
        baseline = (run.get("context_snapshot") or {}).get("workspace_baseline") or {}
        artifact_path = artifact_sha256 = ""
        base_revision = str(
            run.get("base_revision")
            or ((run.get("context_snapshot") or {}).get("workspace_baseline") or {}).get("revision")
            or ""
        )
        if execution_environment == "worktree" and changed_locations:
            artifact_path, artifact_sha256 = self._capture_delivery_patch(
                run_id,
                execution_workspace,
                base_revision,
                [str(item["file"]) for item in changed_locations],
            )
        artifact_snapshot = {
            "context_version": int(task.get("context_version") or 1),
            "workspace": workspace_state,
            "diff": self._workspace_diff(
                execution_workspace, str(baseline.get("revision") or ""),
                [str(item["file"]) for item in changed_locations],
            ),
            "execution_workspace": execution_workspace,
            "artifact_path": artifact_path,
            "artifact_sha256": artifact_sha256,
            "base_ref": str(run.get("base_ref") or ""),
        }
        if requires_code_review:
            next_stage = "code_review"
        else:
            next_stage = "done"
        delivery_run_status = "waiting_review" if next_stage == "code_review" else "completed"
        conversation_status = "waiting_review" if delivery_run_status == "waiting_review" else "completed"
        if token_used:
            self.record_run_token_usage(run_id, token_used)
        integration_result: dict[str, Any] = {}
        if (
            execution_environment == "worktree"
            and not requires_code_review
            and changed_locations
        ):
            integration_result = self._integrate_delivery_artifact(
                task,
                run_id,
                {
                    **run,
                    "workspace_path": execution_workspace,
                    "base_revision": base_revision,
                    "output_revision": str(workspace_state.get("revision") or ""),
                    "artifact_path": artifact_path,
                    "artifact_sha256": artifact_sha256,
                },
            )
        elif execution_environment == "worktree" and not changed_locations:
            integration_result = {
                "integration_status": "not_required",
                "workspace_sync_status": "not_required",
            }
        with self.db.transaction() as connection:
            if batch:
                sealed = connection.execute(
                    """UPDATE execution_batches
                       SET admission_open=0, state='review', delivery_run_id=?,
                           sealed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
                       WHERE id=? AND revision=? AND state='development'""",
                    (run_id, batch["id"], int(batch["revision"])),
                )
                if sealed.rowcount != 1:
                    return {
                        "task": self.get_task(task["id"]),
                        "run": self.get_run(run_id),
                        "batch": self.execution_batch(task["id"]),
                        "continue_development": True,
                        "review_dispatch_required": False,
                    }
            run_cursor = connection.execute(
                """UPDATE task_runs SET status=?, delivery_summary=?, verification_result=?, changed_locations=?, acceptance_evidence=?, artifact_snapshot=?, token_used=MAX(token_used, ?),
                   workspace_path=?, base_revision=?, output_revision=?, artifact_path=?, artifact_sha256=?,
                   integration_status=?, integration_error='', integration_revision=?,
                   workspace_sync_status=?, workspace_sync_error=?,
                   completed_at=CASE WHEN ?='completed' THEN CURRENT_TIMESTAMP ELSE completed_at END,
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'""",
                (delivery_run_status, delivery_summary.strip(), verification_result.strip(), json.dumps(changed_locations, ensure_ascii=False),
                 json.dumps(acceptance_evidence, ensure_ascii=False), json.dumps(artifact_snapshot, ensure_ascii=False),
                 token_used, execution_workspace, base_revision,
                 str(workspace_state.get("revision") or ""), artifact_path, artifact_sha256,
                 str(integration_result.get("integration_status") or (
                     "pending" if execution_environment == "worktree" else "local"
                 )),
                 str(integration_result.get("integration_revision") or ""),
                 str(integration_result.get("workspace_sync_status") or (
                     "pending" if execution_environment == "worktree" else "local"
                 )),
                 str(integration_result.get("workspace_sync_error") or ""),
                 delivery_run_status, run_id),
            )
            if run_cursor.rowcount != 1:
                raise ValueError("Execution run changed concurrently")
            task_cursor = connection.execute(
                """UPDATE tasks SET status=?, delivery_summary=?, verification_result=?,
                   token_used=(SELECT COALESCE(SUM(r.token_used), 0) FROM task_runs r WHERE r.task_id=tasks.id),
                   effective_token_used=(SELECT COALESCE(SUM(r.effective_token_used), 0) FROM task_runs r WHERE r.task_id=tasks.id),
                   active_run_id=NULL, assigned_to=NULL,
                   primary_run_id=?,
                   execution_recovery_count=0, last_recovery_reason='',
                   review_interrupt_count=0, review_retry_after=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND active_run_id=? AND status IN ('investigating','implementing','rework')""",
                (next_stage, delivery_summary.strip(), verification_result.strip(), run_id, task["id"], run_id),
            )
            if task_cursor.rowcount != 1:
                raise ValueError("Task changed concurrently during delivery")
            if batch and len(batch_tasks) > 1:
                self._create_batch_member_deliveries(
                    connection,
                    batch,
                    task,
                    run,
                    delivery_summary.strip(),
                    verification_result.strip(),
                    changed_locations,
                    acceptance_evidence,
                    artifact_snapshot,
                )
            self._event(
                connection,
                "run",
                run_id,
                "delivery_submitted",
                {
                    "task_id": task["id"],
                    "next_stage": next_stage,
                    "requires_code_review": requires_code_review,
                },
            )
            connection.execute(
                "UPDATE task_conversations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (conversation_status, run_id),
            )
            connection.execute(
                "UPDATE task_run_conversations SET status=?, updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (conversation_status, run_id),
            )
            self._queue_obsidian_sync(connection, "task", task["id"])
        updated = self.get_task(task["id"])
        if updated["status"] == "done":
            self._create_experience(updated)
        self.flush_integration_outbox()
        return {
            "task": updated,
            "run": self.get_run(run_id),
            "next_stage": next_stage,
            "review_dispatch_required": next_stage == "code_review",
        }

    def review_code(
        self,
        task_id: str,
        run_id: str,
        verdict: str,
        reasons: list[str] | None = None,
        passed_items: list[str] | None = None,
        failed_criteria: list[str] | None = None,
        failure_category: str | None = None,
        failure_locations: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        batch = self._batch_for_run(run_id)
        if (
            task.get("active_run_id") != run_id
            or task["status"] != "code_review"
            or run["run_type"] != "code_review"
            or run["status"] != "running"
        ):
            raise ValueError("A running code_review run is required")
        verdict = str(verdict).lower().strip()
        if verdict not in {"pass", "fail"}:
            raise ValueError("verdict must be pass or fail")
        reasons = [str(x).strip() for x in (reasons or []) if str(x).strip()]
        def review_result_label(item: Any) -> str:
            if isinstance(item, dict):
                return str(
                    item.get("id")
                    or item.get("criterion")
                    or item.get("description")
                    or ""
                ).strip()
            return str(item or "").strip()

        passed_items = [
            review_result_label(item)
            for item in (passed_items or [])
            if review_result_label(item)
        ]
        failed_criteria = [
            review_result_label(item)
            for item in (failed_criteria or [])
            if review_result_label(item)
        ]
        contract = task.get("review_contract") or {}
        review_items = (
            self._batch_review_items(task_id)
            if batch and int(batch.get("appended_count") or 0) > 0
            else []
        )
        if not review_items:
            for item in contract.get("checks", []) or []:
                if isinstance(item, dict):
                    review_items.append(
                        str(
                            item.get("id")
                            or item.get("criterion")
                            or item.get("description")
                            or ""
                        ).strip()
                    )
                else:
                    review_items.append(str(item).strip())
            if not review_items:
                review_items = [
                    str(item).strip() for item in (contract.get("rules", []) or [])
                ]
            review_items = [item for item in review_items if item]
        criteria = set(review_items)
        if len(passed_items) != len(set(passed_items)) or len(failed_criteria) != len(
            set(failed_criteria)
        ):
            raise ValueError("Review result items must not contain duplicates")
        if set(passed_items) & set(failed_criteria):
            raise ValueError("Review passed_items and failed_criteria must not overlap")
        if set(passed_items) | set(failed_criteria) != criteria:
            raise ValueError(
                "Review result must exactly cover every confirmed criterion; expected: "
                + json.dumps(review_items, ensure_ascii=False)
            )
        if verdict == "pass":
            if failed_criteria:
                raise ValueError("A passing code review cannot contain failed_criteria")
            if set(passed_items) != criteria:
                raise ValueError(
                    "A passing code review must cover every confirmed criterion"
                )
        elif not reasons or not failed_criteria:
            raise ValueError(
                "A failed code review requires reasons and failed_criteria"
            )
        if verdict == "pass":
            delivery_run_id = str(run.get("delivery_run_id") or "")
            try:
                self._integrate_delivery_artifact(task, delivery_run_id)
            except Exception as exc:
                with self.db.transaction() as connection:
                    connection.execute(
                        """UPDATE task_runs SET integration_status='failed',
                           integration_error=?, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                        (str(exc)[:2000], delivery_run_id),
                    )
                    self._event(
                        connection,
                        "run",
                        delivery_run_id,
                        "delivery_integration_failed",
                        {"task_id": task_id, "error": str(exc)[:2000]},
                    )
                raise
        detected_category = "implementation"
        self_heal = self._self_heal_plan(task, "implementation", [])
        if verdict == "fail":
            detected_category = str(failure_category or "").strip().lower()
            if detected_category not in {"project", "environment", "implementation"}:
                detected_category = classify_recoverable_failure(
                    *reasons, *failed_criteria
                )
            if detected_category in {"project", "environment"} and not normalize_self_heal_locations(
                failure_locations
            ):
                raise ValueError(
                    "Project and environment failures require at least one exact failure_location file"
                )
            self_heal = self._self_heal_plan(
                task,
                detected_category,
                reasons,
                failure_locations,
            )
        completed_batch_task_ids: list[str] = []
        with self.db.transaction() as connection:
            round_no = connection.execute(
                "SELECT COALESCE(MAX(round),0)+1 value FROM reviews WHERE task_id=?",
                (task_id,),
            ).fetchone()["value"]
            connection.execute(
                "INSERT INTO reviews(task_id,round,verdict,reasons,passed_items,failed_criteria) VALUES(?,?,?,?,?,?)",
                (
                    task_id,
                    round_no,
                    verdict,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(passed_items, ensure_ascii=False),
                    json.dumps(failed_criteria, ensure_ascii=False),
                ),
            )
            connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=?",
                (run_id,),
            )
            connection.execute(
                """UPDATE task_conversations SET status='completed',
                   updated_at=CURRENT_TIMESTAMP WHERE run_id=?""",
                (run_id,),
            )
            connection.execute(
                """UPDATE task_run_conversations SET status='completed',
                   updated_at=CURRENT_TIMESTAMP WHERE run_id=?""",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_runs SET status='review_failed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                (run.get("delivery_run_id"),),
            ) if verdict == "fail" else connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                (run.get("delivery_run_id"),),
            )
            review_rework_attempt = (
                int(task.get("review_rework_count") or 0) + 1
                if verdict == "fail" else 0
            )
            implementation_rework_allowed = (
                self_heal["category"] != "implementation"
                or review_rework_attempt <= REVIEW_REWORK_LIMIT
            )
            next_status = (
                (
                    "rework"
                    if (
                        self_heal["category"] == "implementation"
                        and implementation_rework_allowed
                    )
                    or self_heal["scheduled"]
                    else "waiting_confirmation"
                )
                if verdict == "fail"
                else "done"
            )
            connection.execute(
                """UPDATE tasks SET status=?, active_run_id=NULL, assigned_to=NULL,
                   last_review_reasons=?, last_failed_criteria=?,
                   review_rework_count=review_rework_count+?, implementation_contract=?,
                   auto_dispatch=? WHERE id=?""",
                (
                    next_status,
                    json.dumps(reasons, ensure_ascii=False),
                    json.dumps(failed_criteria, ensure_ascii=False),
                    int(verdict == "fail"),
                    json.dumps(self_heal["contract"], ensure_ascii=False),
                    int(next_status != "waiting_confirmation"),
                    task_id,
                ),
            )
            if verdict == "fail" and self_heal["category"] in {
                "project", "environment",
            }:
                self._persist_self_heal_targets(
                    connection, task_id, self_heal["locations"]
                )
                self._event(
                    connection,
                    "task",
                    task_id,
                    "self_heal_scheduled" if self_heal["scheduled"] else "self_heal_exhausted",
                    {
                        "stage": "code_review",
                        "failure_category": self_heal["category"],
                        "attempt": self_heal["attempt"],
                        "attempt_limit": SELF_HEAL_ATTEMPT_LIMIT,
                        "locations": self_heal["locations"],
                    },
                )
            if batch:
                completed_batch_task_ids = self._propagate_batch_review(
                    connection,
                    batch,
                    task_id,
                    verdict,
                    reasons,
                    passed_items,
                    failed_criteria,
                    next_status,
                )
            self._event(
                connection,
                "task",
                task_id,
                "code_reviewed",
                {
                    "verdict": verdict,
                    "reasons": reasons,
                    "next_stage": next_status,
                    "failure_category": detected_category if verdict == "fail" else "",
                    "review_rework_attempt": review_rework_attempt,
                    "review_rework_limit": REVIEW_REWORK_LIMIT,
                    "review_rework_exhausted": bool(
                        verdict == "fail"
                        and self_heal["category"] == "implementation"
                        and not implementation_rework_allowed
                    ),
                    "self_heal_scheduled": bool(
                        verdict == "fail" and self_heal["scheduled"]
                    ),
                },
            )
            self._queue_obsidian_sync(connection, "task", task_id)
        updated = self.get_task(task_id)
        if updated["status"] == "done":
            self._create_experience(updated)
        for completed_task_id in completed_batch_task_ids:
            self._create_experience(self.get_task(completed_task_id))
        self.flush_integration_outbox()
        return updated

    def list_reviews(self, task_id: str) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute("SELECT * FROM reviews WHERE task_id=? ORDER BY round", (task_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["reasons"] = json.loads(item["reasons"] or "[]")
            item["passed_items"] = json.loads(item["passed_items"] or "[]")
            item["failed_criteria"] = json.loads(item.get("failed_criteria") or "[]")
            result.append(item)
        return result

    def task_delivery(self, task_id: str) -> dict[str, str]:
        task = self.get_task(task_id)
        return {"summary": task.get("delivery_summary", ""), "verification": task.get("verification_result", "")}
