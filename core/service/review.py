from __future__ import annotations

import json
import posixpath
import subprocess
import time
import uuid
from collections import Counter
from typing import Any

from ..runtime import project_runtime_environment


class TaskReviewMixin:
    """Delivery validation, automated checks, review decisions, and acceptance records."""

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
                    "Use the exact symbols from RUN_CONTEXT_JSON.located_targets "
                    "and retry the same active run."
                )

    def _validate_workspace_delta(
        self, task: dict[str, Any], run: dict[str, Any], changed_locations: list[dict[str, Any]],
        current: dict[str, Any] | None = None, task_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        task_ids = task_ids or [task["id"]]
        baseline = (run.get("context_snapshot") or {}).get("workspace_baseline") or {}
        if not baseline.get("available"):
            return current or {}
        current = current or self._workspace_state(task.get("project"))
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
                self._normalize_project(task.get("project")), "diff", "--name-only", "-z",
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
        workspace_state = self._workspace_state(task.get("project"))
        self._validate_workspace_delta(
            task, run, changed_locations, workspace_state, batch_task_ids
        )
        if not isinstance(acceptance_evidence, list) or not acceptance_evidence or any(not isinstance(item, dict) for item in acceptance_evidence):
            raise ValueError("acceptance_evidence is required")
        criteria = set(
            self._batch_acceptance_items(task["id"])
            if batch and len(batch_tasks) > 1
            else task.get("acceptance_criteria", [])
        )
        covered = {item.get("criterion") for item in acceptance_evidence}
        missing_criteria = criteria - covered
        if missing_criteria:
            raise ValueError(f"Missing acceptance evidence for: {', '.join(sorted(missing_criteria))}")
        invalid_evidence = [
            item.get("criterion") for item in acceptance_evidence
            if item.get("criterion") not in criteria or not str(item.get("evidence") or "").strip()
        ]
        if invalid_evidence:
            raise ValueError("Every acceptance criterion requires non-empty evidence and an exact criterion match")
        baseline = (run.get("context_snapshot") or {}).get("workspace_baseline") or {}
        artifact_snapshot = {
            "context_version": int(task.get("context_version") or 1),
            "workspace": workspace_state,
            "diff": self._workspace_diff(
                task.get("project"), str(baseline.get("revision") or ""),
                [str(item["file"]) for item in changed_locations],
            ),
        }
        if token_used:
            self.record_run_token_usage(run_id, token_used)
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
                """UPDATE task_runs SET status='waiting_review', delivery_summary=?, verification_result=?, changed_locations=?, acceptance_evidence=?, artifact_snapshot=?, token_used=MAX(token_used, ?),
                   updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='running'""",
                (delivery_summary.strip(), verification_result.strip(), json.dumps(changed_locations, ensure_ascii=False),
                 json.dumps(acceptance_evidence, ensure_ascii=False), json.dumps(artifact_snapshot, ensure_ascii=False),
                 token_used, run_id),
            )
            if run_cursor.rowcount != 1:
                raise ValueError("Execution run changed concurrently")
            next_stage = "code_review" if int(task.get("workflow_version") or 1) >= 2 else "review"
            task_cursor = connection.execute(
                f"""UPDATE tasks SET status='{next_stage}', delivery_summary=?, verification_result=?,
                   token_used=(SELECT COALESCE(SUM(r.token_used), 0) FROM task_runs r WHERE r.task_id=tasks.id),
                   effective_token_used=(SELECT COALESCE(SUM(r.effective_token_used), 0) FROM task_runs r WHERE r.task_id=tasks.id),
                   active_run_id=NULL, assigned_to=NULL,
                   primary_run_id=?,
                   review_interrupt_count=0, review_retry_after=NULL, updated_at=CURRENT_TIMESTAMP
                   WHERE id=? AND active_run_id=? AND status IN ('investigating','implementing','rework')""",
                (delivery_summary.strip(), verification_result.strip(), run_id, task["id"], run_id),
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
            self._event(connection, "run", run_id, "delivery_submitted", {"task_id": task["id"]})
            connection.execute(
                "UPDATE task_conversations SET status='waiting_review', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_run_conversations SET status='waiting_review', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            self._queue_obsidian_sync(connection, "task", task["id"])
        updated = self.get_task(task["id"])
        self.flush_integration_outbox()
        return {"task": updated, "run": self.get_run(run_id), "review_dispatch_required": True}

    def prepare_review_location(self, task_id: str) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "review":
            raise ValueError("Task must be in review")
        runs = [run for run in self.list_runs(task_id) if run["id"] == task.get("primary_run_id") and run.get("acceptance_evidence")]
        if not runs:
            raise ValueError("No submitted delivery is available for review")
        latest = runs[-1]
        payload = {
            "project": task["project"], "title": task["title"], "goal": task["goal"],
            "modules": task["modules"] + [item.get("file", "") for item in latest["changed_locations"]],
        }
        prepared = self.prepare_location_analysis(payload, "review", task_id, latest["id"])
        prepared["changed_locations"] = latest["changed_locations"]
        prepared["acceptance_evidence"] = latest["acceptance_evidence"]
        prepared["acceptance_criteria"] = task["acceptance_criteria"]
        return prepared

    def prepare_review_run(self, task_id: str, review_location_analysis_id: str, reviewer_id: str = "codex-reviewer", lease_seconds: int = 1800) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "review":
            raise ValueError("Task must be in review")
        analysis = self.get_location_analysis(review_location_analysis_id)
        if analysis["stage"] != "review" or analysis["task_id"] != task_id or analysis["status"] != "completed":
            raise ValueError("A completed review location analysis for this task is required")
        deliveries = [run for run in self.list_runs(task_id) if run["id"] == task.get("primary_run_id") and run.get("acceptance_evidence")]
        if not deliveries:
            raise ValueError("No submitted delivery is available for review")
        latest_delivery = deliveries[-1]
        if analysis.get("delivery_run_id") != latest_delivery["id"] or (
            analysis.get("delivery_attempt") is not None and analysis["delivery_attempt"] != latest_delivery["attempt"]
        ):
            raise ValueError("Review location analysis belongs to an older delivery")
        review_context = {
            "task_id": task_id,
            "review_location_analysis_id": analysis["id"],
            "delivery_run_id": latest_delivery["id"],
            "targets": analysis["targets"],
            "location_evidence": analysis["location_evidence"],
            "obsidian_evidence": analysis["obsidian_evidence"],
            "acceptance_plan": analysis["acceptance_plan"],
            "changed_locations": latest_delivery["changed_locations"],
            "acceptance_evidence": latest_delivery["acceptance_evidence"],
            "verification_result": latest_delivery["verification_result"],
        }
        with self.db.transaction() as connection:
            existing = connection.execute(
                """SELECT id FROM task_runs WHERE id=? AND task_id=? AND run_type='review'
                   AND delivery_run_id=? AND status IN ('awaiting_thread','running')""",
                (task.get("active_run_id"), task_id, latest_delivery["id"]),
            ).fetchone()
            if existing:
                run_id = existing["id"]
                if task.get("active_run_id") != run_id:
                    raise ValueError("Task active review run does not match the prepared run")
            else:
                delivery = connection.execute(
                    """SELECT id FROM task_runs WHERE id=? AND task_id=?
                       AND run_type IN ('execution','rework') AND status='waiting_review'""",
                    (task.get("primary_run_id"), task_id),
                ).fetchone()
                if not delivery:
                    raise ValueError("Task has no current delivery awaiting review")
                attempt = connection.execute(
                    "SELECT COALESCE(MAX(attempt),0)+1 AS value FROM task_runs WHERE task_id=?",
                    (task_id,),
                ).fetchone()["value"]
                run_id = self.db.next_id(connection, "RUN")
                connection.execute(
                    """INSERT INTO task_runs(
                           id, task_id, parent_run_id, delivery_run_id, run_type, attempt,
                           status, claimed_by, lease_token, lease_expires_at
                       ) VALUES(?, ?, ?, ?, 'review', ?, 'awaiting_thread', ?, ?, datetime('now', ?))""",
                    (
                        run_id, task_id, delivery["id"], delivery["id"], attempt, reviewer_id,
                        uuid.uuid4().hex, f"+{max(300, min(lease_seconds, 7200))} seconds",
                    ),
                )
                connection.execute(
                    "UPDATE tasks SET active_run_id=?, assigned_to=?, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='review'",
                    (run_id, reviewer_id, task_id),
                )
            connection.execute(
                "UPDATE task_runs SET context_snapshot=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (json.dumps(review_context, ensure_ascii=False), run_id),
            )
        return {
            "task": self.get_task(task_id),
            "run": self.get_run(run_id),
            "dispatch_prompt": (
                f"独立验收 Codex Taskboard 任务 {task_id}（验收运行 {run_id}）。"
                "先读取该运行的 context_snapshot；只检查其中的 targets、changed_locations、直接依赖和 acceptance_plan。"
                "依据逐条 acceptance_evidence 与已保存的 CodeGraph、GitNexus 或源码匹配影响证据验证，不重新扫描整个项目，也不修改实现；"
                "先调用 run_acceptance_checks 执行自动化检查，再调用 review_task；失败时列出原因和已通过项。"
                "passed_items 与 failed_criteria 必须使用原始验收标准的精确文本并完整覆盖全部标准；"
                "通过时 passed_items 必须包含全部原始验收标准，范围外发现不得用于打回。"
            ),
        }

    def run_acceptance_checks(
        self, task_id: str, run_id: str, force: bool = False,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        run = self.get_run(run_id)
        if task["status"] not in {"review", "acceptance"} or task.get("active_run_id") != run_id:
            raise ValueError("Acceptance checks require the active review run")
        if run["task_id"] != task_id or run["run_type"] not in {"review", "acceptance"} or run["status"] != "running":
            raise ValueError("Acceptance checks require a running review run")
        context = run.get("context_snapshot") or {}
        delivery_run_id = str(context.get("delivery_run_id") or run.get("delivery_run_id") or "")
        if not delivery_run_id or delivery_run_id != task.get("primary_run_id"):
            raise ValueError("Acceptance checks are not bound to the current delivery")
        plans = [
            item for item in (context.get("acceptance") or context.get("acceptance_plan") or [])
            if str(item.get("check_type") or ("automated" if item.get("command") else "static_review")) == "automated"
        ]
        workspace = self._workspace_state(task.get("project"))
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
                               status, exit_code, output, duration_ms, workspace_fingerprint, cache_hit
                           ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 1)""",
                        (
                            task_id, run_id, delivery_run_id, criterion, command,
                            cached_item["status"], cached_item["exit_code"], cached_item["output"],
                            workspace_fingerprint,
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
            started = time.monotonic()
            exit_code: int | None = None
            try:
                completed = subprocess.run(
                    command,
                    cwd=task["project"],
                    env=project_runtime_environment(task["project"]),
                    shell=True,
                    executable="/bin/zsh",
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    check=False,
                )
                exit_code = completed.returncode
                status = "passed" if completed.returncode == 0 else "failed"
                output = "\n".join(part for part in (completed.stdout, completed.stderr) if part).strip()
            except subprocess.TimeoutExpired as exc:
                status = "timeout"
                output = "\n".join(
                    str(part) for part in (exc.stdout, exc.stderr, f"Timed out after {timeout_seconds}s") if part
                )
            except OSError as exc:
                status = "error"
                output = str(exc)
            duration_ms = max(0, int((time.monotonic() - started) * 1000))
            with self.db.transaction() as connection:
                cursor = connection.execute(
                    """INSERT INTO acceptance_check_runs(
                           task_id, review_run_id, delivery_run_id, criterion, command,
                           status, exit_code, output, duration_ms, workspace_fingerprint, cache_hit
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                    (
                        task_id, run_id, delivery_run_id, criterion, command,
                        status, exit_code, output[-12000:], duration_ms, workspace_fingerprint,
                    ),
                )
                check_id = cursor.lastrowid
                self._event(connection, "run", run_id, "acceptance_check_completed", {
                    "check_id": check_id, "criterion": plan.get("criterion"), "status": status,
                    "exit_code": exit_code, "duration_ms": duration_ms,
                })
            results.append(self.get_acceptance_check(check_id))
        return {
            "task_id": task_id,
            "run_id": run_id,
            "delivery_run_id": delivery_run_id,
            "workspace_fingerprint": workspace_fingerprint,
            "cache_hits": sum(int(item.get("cache_hit") or 0) for item in results),
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

    def review_task(
        self, task_id: str, verdict: str, reasons: list[str] | None = None,
        passed_items: list[str] | None = None, run_id: str | None = None,
        failed_criteria: list[str] | None = None,
    ) -> dict[str, Any]:
        task = self.get_task(task_id)
        if task["status"] != "review":
            raise ValueError("Task must be in review before it can be reviewed")
        if verdict not in {"pass", "fail"}:
            raise ValueError("verdict must be pass or fail")
        reasons = reasons or []
        passed_items = passed_items or []
        failed_criteria = failed_criteria or []
        if any(not isinstance(item, str) or not item.strip() for item in [*reasons, *passed_items, *failed_criteria]):
            raise ValueError("Review reasons and criterion results must be non-empty strings")
        if verdict == "fail" and not reasons:
            raise ValueError("Review failure requires reasons")
        criteria_list = [str(item) for item in task.get("acceptance_criteria", [])]
        criteria = set(criteria_list)
        if verdict == "fail":
            if not failed_criteria:
                raise ValueError("Review failure requires failed_criteria")
            unknown = set(failed_criteria) - criteria
            if unknown:
                raise ValueError(f"Review cannot fail outside confirmed acceptance criteria: {', '.join(sorted(unknown))}")
        elif failed_criteria:
            raise ValueError("Passing review cannot include failed_criteria")
        unknown_passed = set(passed_items) - criteria
        if unknown_passed:
            raise ValueError(f"Review passed_items are outside confirmed acceptance criteria: {', '.join(sorted(unknown_passed))}")
        if set(passed_items) & set(failed_criteria):
            raise ValueError("A review criterion cannot be both passed and failed")
        result_counter = Counter(passed_items) + Counter(failed_criteria)
        if result_counter != Counter(criteria_list):
            raise ValueError("Review results must exactly cover every confirmed acceptance criterion")
        if not run_id:
            raise ValueError("run_id from a located independent review is required")
        run = self.get_run(run_id)
        if run["task_id"] != task_id or run["run_type"] != "review" or run["status"] != "running":
            raise ValueError("Invalid or inactive review run")
        if task.get("active_run_id") != run_id:
            raise ValueError("Review run is no longer the task's active run")
        review_context = run.get("context_snapshot") or {}
        analysis_id = str(review_context.get("review_location_analysis_id") or "")
        delivery_run_id = str(review_context.get("delivery_run_id") or "")
        if not analysis_id or not delivery_run_id or not review_context.get("targets") or not review_context.get("acceptance_plan"):
            raise ValueError("Review location analysis must be completed and prepared before review_task")
        analysis = self.get_location_analysis(analysis_id)
        if (
            analysis["stage"] != "review" or analysis["task_id"] != task_id
            or analysis["status"] != "completed" or analysis.get("delivery_run_id") != delivery_run_id
        ):
            raise ValueError("Review location analysis is missing, stale or belongs to another delivery")
        delivery_run = self.get_run(delivery_run_id)
        if (
            delivery_run["task_id"] != task_id
            or delivery_run["id"] != task.get("primary_run_id")
            or delivery_run["run_type"] not in {"execution", "rework"}
            or delivery_run["status"] != "waiting_review"
            or str(run.get("delivery_run_id") or "") != delivery_run_id
        ):
            raise ValueError("Reviewed delivery is no longer current")
        if Counter(str(item.get("criterion") or "") for item in review_context["acceptance_plan"]) != Counter(criteria_list):
            raise ValueError("Prepared review plan does not cover the confirmed acceptance criteria")
        if verdict == "pass":
            self._assert_required_acceptance_checks_passed(run_id, review_context["acceptance_plan"])
        with self.db.transaction() as connection:
            previous_review = connection.execute(
                "SELECT failed_criteria, reasons FROM reviews WHERE task_id=? AND verdict='fail' ORDER BY round DESC LIMIT 1",
                (task_id,),
            ).fetchone()
            round_number = connection.execute(
                "SELECT COALESCE(MAX(round), 0) + 1 AS next_round FROM reviews WHERE task_id = ?", (task_id,)
            ).fetchone()["next_round"]
            connection.execute(
                "INSERT INTO reviews(task_id, round, verdict, reasons, passed_items, failed_criteria) VALUES(?, ?, ?, ?, ?, ?)",
                (task_id, round_number, verdict, json.dumps(reasons, ensure_ascii=False),
                 json.dumps(passed_items, ensure_ascii=False), json.dumps(failed_criteria, ensure_ascii=False)),
            )
            connection.execute(
                "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (run_id,),
            )
            if verdict == "fail":
                normalized_reasons = {str(item).strip() for item in reasons if str(item).strip()}
                repeated_failure = bool(previous_review) and (
                    set(json.loads(previous_review["failed_criteria"] or "[]")) == set(failed_criteria)
                    and {str(item).strip() for item in json.loads(previous_review["reasons"] or "[]") if str(item).strip()} == normalized_reasons
                )
                connection.execute(
                    """UPDATE tasks SET status='rework', active_run_id=NULL, assigned_to=NULL,
                       review_failed_at=CURRENT_TIMESTAMP, last_review_reasons=?, last_failed_criteria=?,
                       review_rework_count=review_rework_count+1, auto_dispatch=?,
                       review_interrupt_count=0, review_retry_after=NULL, updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (
                        json.dumps(reasons, ensure_ascii=False), json.dumps(failed_criteria, ensure_ascii=False),
                        0 if repeated_failure else 1, task_id,
                    ),
                )
                connection.execute(
                    "UPDATE task_runs SET status='review_failed', completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                    (delivery_run_id,),
                )
                connection.execute(
                    "UPDATE task_conversations SET status='review_failed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (delivery_run_id,),
                )
                connection.execute(
                    "UPDATE task_run_conversations SET status='review_failed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (delivery_run_id,),
                )
            else:
                connection.execute(
                    """INSERT INTO acceptance_results(task_id, run_id, delivery_run_id, round, verdict,
                       reasons, passed_criteria, failed_criteria, failure_locations)
                       VALUES(?, ?, ?, (SELECT COALESCE(MAX(round),0)+1 FROM acceptance_results WHERE task_id=?), 'pass', ?, ?, '[]', '[]')""",
                    (task_id, run_id, delivery_run_id, task_id, json.dumps(reasons, ensure_ascii=False), json.dumps(passed_items, ensure_ascii=False)),
                )
                connection.execute(
                    """UPDATE tasks SET status='done', active_run_id=NULL, assigned_to=NULL,
                       auto_dispatch=1, retry_required=0, retry_run_type=NULL,
                       review_interrupt_count=0, review_retry_after=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (task_id,),
                )
                connection.execute(
                    "UPDATE task_runs SET status='completed', completed_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP WHERE id=? AND status='waiting_review'",
                    (delivery_run_id,),
                )
                connection.execute(
                    "UPDATE task_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (delivery_run_id,),
                )
                connection.execute(
                    "UPDATE task_run_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                    (delivery_run_id,),
                )
            connection.execute(
                "UPDATE task_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            connection.execute(
                "UPDATE task_run_conversations SET status='completed', updated_at=CURRENT_TIMESTAMP WHERE run_id=?",
                (run_id,),
            )
            self._event(connection, "task", task_id, "reviewed", {"round": round_number, "verdict": verdict, "reasons": reasons})
            self._queue_obsidian_sync(connection, "task", task_id)
        updated = self.get_task(task_id)
        if verdict == "pass":
            self._create_experience(updated)
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
