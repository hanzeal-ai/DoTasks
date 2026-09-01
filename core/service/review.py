from __future__ import annotations

import json
import posixpath
import subprocess
import time
import uuid
from collections import Counter
from typing import Any

from ..runtime import project_runtime_environment
from ..self_healing import (
    SELF_HEAL_ATTEMPT_LIMIT,
    classify_recoverable_failure,
    infer_environment_repair_command,
)


class TaskReviewMixin:
    """Delivery validation, automated checks, review decisions, and acceptance records."""

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
        if not isinstance(changed_locations, list) or not changed_locations:
            raise ValueError("changed_locations is required")
        for location in changed_locations:
            if not location.get("file"):
                raise ValueError("Every changed location requires a file")
            location["file"] = self._normalize_target_file(location["file"])
            symbols = location.get("symbols", [])
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
                raise ValueError("Changed location symbols must be an array of strings")
            location["symbols"] = [symbol.strip() for symbol in symbols if symbol.strip()]
            location.setdefault("summary", "")
        self._validate_changed_locations(task["id"], changed_locations, batch_task_ids)
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
        workspace_state = self._workspace_state(execution_workspace)
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
        requires_code_review = self._quality_gate_required(task, "code_review")
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
        if execution_environment == "worktree":
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
        if execution_environment == "worktree" and not requires_code_review:
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
            "review_dispatch_required": next_stage in {"review", "code_review"},
        }

    def run_acceptance_checks(
        self, task_id: str, run_id: str, force: bool = False,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        if task["status"] not in {"review", "code_review", "acceptance"} or task.get("active_run_id") != run_id:
            raise ValueError("Acceptance checks require the active review run")
        if run["task_id"] != task_id or run["run_type"] not in {"review", "code_review", "acceptance"} or run["status"] != "running":
            raise ValueError("Acceptance checks require a running review run")
        context = run.get("context_snapshot") or {}
        delivery_run_id = str(context.get("delivery_run_id") or run.get("delivery_run_id") or "")
        if not delivery_run_id or delivery_run_id != task.get("primary_run_id"):
            raise ValueError("Acceptance checks are not bound to the current delivery")
        delivery_run = self.get_run(delivery_run_id)
        execution_workspace = str(
            delivery_run.get("workspace_path") or task.get("project") or ""
        )
        plans = [
            item for item in (context.get("acceptance") or context.get("acceptance_plan") or [])
            if str(item.get("check_type") or ("automated" if item.get("command") else "static_review")) == "automated"
        ]
        workspace = self._workspace_state(execution_workspace)
        workspace_fingerprint = str(workspace.get("fingerprint") or "") if workspace.get("available") else ""
        results: list[dict[str, Any]] = []
        for plan in plans:
            command = str(plan.get("command") or "").strip()
            if not command:
                raise ValueError("Automated acceptance checks require command")
            criterion = str(plan.get("criterion") or "")
            cached = None
            if not force and workspace_fingerprint:
                with self.db.connection() as connection:
                    cached = connection.execute(
                        """SELECT * FROM acceptance_check_runs
                           WHERE delivery_run_id=? AND criterion=? AND command=?
                             AND workspace_fingerprint=? AND status='passed'
                           ORDER BY created_at DESC, id DESC LIMIT 1""",
                        (delivery_run_id, criterion, command, workspace_fingerprint),
                    ).fetchone()
            if cached:
                cached_item = dict(cached)
                with self.db.transaction() as connection:
                    cursor = connection.execute(
                        """INSERT INTO acceptance_check_runs(
                               task_id, review_run_id, delivery_run_id, criterion, command,
                               status, exit_code, output, duration_ms, workspace_fingerprint, cache_hit,
                               failure_category, repair_attempted, repair_command, repair_status,
                               repair_output, initial_status, initial_exit_code, initial_output
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (
                            task_id, run_id, delivery_run_id, criterion, command,
                            cached_item["status"], cached_item["exit_code"], cached_item["output"],
                            workspace_fingerprint,
                            cached_item.get("failure_category", ""),
                            0,
                            cached_item.get("repair_command", ""),
                            (
                                "cached_healed"
                                if cached_item.get("repair_status") in {
                                    "healed", "cached_healed",
                                }
                                else ""
                            ),
                            cached_item.get("repair_output", ""),
                            cached_item.get("initial_status", ""),
                            cached_item.get("initial_exit_code"),
                            cached_item.get("initial_output", ""),
                        ),
                    )
                    check_id = cursor.lastrowid
                    self._event(connection, "run", run_id, "acceptance_check_cache_hit", {
                        "check_id": check_id, "source_check_id": cached_item["id"],
                        "criterion": criterion, "workspace_fingerprint": workspace_fingerprint,
                    })
                results.append(self.get_acceptance_check(check_id))
                continue
            timeout_seconds = max(1, min(int(plan.get("timeout_seconds", 300)), 1800))
            status, exit_code, output, duration_ms = self._run_project_command(
                command, execution_workspace, timeout_seconds
            )
            initial_status, initial_exit_code, initial_output = status, exit_code, output
            failure_category = (
                classify_recoverable_failure(output, command)
                if status != "passed" else ""
            )
            configured_category = str(plan.get("failure_category") or "").strip().lower()
            if status != "passed" and configured_category in {
                "project", "environment", "implementation",
            }:
                failure_category = configured_category
            repair_attempted = 0
            repair_command = ""
            repair_status = ""
            repair_output = ""
            if failure_category == "environment":
                repair_command = str(plan.get("repair_command") or "").strip()
                if not repair_command:
                    repair_command = infer_environment_repair_command(
                        execution_workspace, output
                    )
                with self.db.connection() as connection:
                    prior_repairs = connection.execute(
                        """SELECT COUNT(*) value FROM acceptance_check_runs
                           WHERE delivery_run_id=? AND criterion=? AND command=?
                             AND repair_attempted=1""",
                        (delivery_run_id, criterion, command),
                    ).fetchone()["value"]
                if repair_command and int(prior_repairs) < SELF_HEAL_ATTEMPT_LIMIT:
                    repair_attempted = 1
                    repair_timeout = max(
                        1,
                        min(int(plan.get("repair_timeout_seconds", timeout_seconds)), 1800),
                    )
                    (
                        repair_run_status,
                        repair_exit_code,
                        repair_output,
                        repair_duration_ms,
                    ) = self._run_project_command(
                        repair_command, execution_workspace, repair_timeout
                    )
                    duration_ms += repair_duration_ms
                    if repair_run_status == "passed":
                        status, exit_code, output, retry_duration_ms = self._run_project_command(
                            command, execution_workspace, timeout_seconds
                        )
                        duration_ms += retry_duration_ms
                        repair_status = "healed" if status == "passed" else "retry_failed"
                    else:
                        repair_status = "failed"
                        repair_output = (
                            f"exit_code={repair_exit_code}\n{repair_output}"
                        ).strip()
                    refreshed_workspace = self._workspace_state(execution_workspace)
                    if refreshed_workspace.get("available"):
                        workspace_fingerprint = str(
                            refreshed_workspace.get("fingerprint") or ""
                        )
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    """INSERT INTO acceptance_check_runs(
                           task_id, review_run_id, delivery_run_id, criterion, command,
                           status, exit_code, output, duration_ms, workspace_fingerprint, cache_hit,
                           failure_category, repair_attempted, repair_command, repair_status,
                           repair_output, initial_status, initial_exit_code, initial_output
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id, run_id, delivery_run_id, criterion, command,
                        status, exit_code, output[-12000:], duration_ms, workspace_fingerprint,
                        failure_category, repair_attempted, repair_command, repair_status,
                        repair_output[-12000:], initial_status, initial_exit_code,
                        initial_output[-12000:],
                    ),
                )
                check_id = cursor.lastrowid
                self._event(connection, "run", run_id, "acceptance_check_completed", {
                    "check_id": check_id, "criterion": plan.get("criterion"), "status": status,
                    "exit_code": exit_code, "duration_ms": duration_ms,
                    "failure_category": failure_category,
                    "repair_attempted": bool(repair_attempted),
                    "repair_status": repair_status,
                })
            results.append(self.get_acceptance_check(check_id))
        return {
            "task_id": task_id,
            "run_id": run_id,
            "delivery_run_id": delivery_run_id,
            "workspace_fingerprint": workspace_fingerprint,
            "cache_hits": sum(int(item.get("cache_hit") or 0) for item in results),
            "self_heal_attempts": sum(
                int(item.get("repair_attempted") or 0) for item in results
            ),
            "self_healed": sum(
                item.get("repair_status") == "healed" for item in results
            ),
            "checks": results,
            "all_required_passed": all(
                (not bool(plan.get("required", True))) or result["status"] == "passed"
                for plan, result in zip(plans, results)
            ),
        }

    def get_acceptance_check(self, check_id: int) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM acceptance_check_runs WHERE id=?", (check_id,)).fetchone()
        if not row:
            raise KeyError(f"Acceptance check not found: {check_id}")
        return dict(row)

    def list_acceptance_checks(self, task_id: str, review_run_id: str | None = None) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            if review_run_id:
                rows = connection.execute(
                    "SELECT * FROM acceptance_check_runs WHERE task_id=? AND review_run_id=? ORDER BY id",
                    (task_id, review_run_id),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM acceptance_check_runs WHERE task_id=? ORDER BY id", (task_id,),
                ).fetchall()
        return [dict(row) for row in rows]

    def _assert_required_acceptance_checks_passed(self, run_id: str, acceptance_plan: list[dict[str, Any]]) -> None:
        required = [
            item for item in acceptance_plan
            if bool(item.get("required", True))
            and str(item.get("check_type") or ("automated" if item.get("command") else "static_review")) == "automated"
        ]
        if not required:
            return
        with self.db.connection() as connection:
            rows = connection.execute(
                "SELECT * FROM acceptance_check_runs WHERE review_run_id=? ORDER BY id DESC", (run_id,),
            ).fetchall()
        latest: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            item = dict(row)
            latest.setdefault((item["criterion"], item["command"]), item)
        missing = []
        failed = []
        for plan in required:
            key = (str(plan.get("criterion") or ""), str(plan.get("command") or "").strip())
            result = latest.get(key)
            if not result:
                missing.append(key[0])
            elif result["status"] != "passed":
                failed.append(f"{key[0]} ({result['status']})")
        if missing:
            raise ValueError(f"Required acceptance checks were not run: {', '.join(missing)}")
        if failed:
            raise ValueError(f"Required acceptance checks did not pass: {', '.join(failed)}")

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
