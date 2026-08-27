from __future__ import annotations

import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskboard.project_guard import ProjectWorkspaceGuard
from .domain import (
    ACTIVE_RUN_STATUSES,
    GENERIC_MATCH_TERMS,
    JSON_FIELDS,
    LOCATION_REPORT_MAX_AGE_SECONDS,
    RELATION_TYPES,
    _decode_row,
    _search_tokens,
    specific_modules,
)


class TaskPlanningMixin:
    """Task creation, dependency analysis, target locks, and bounded location gates."""

    def _validate_implementation_contract(
        self, contract: dict[str, Any], located_targets: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Validate and normalize the current object-form implementation contract."""
        if not isinstance(contract, dict):
            raise ValueError("implementation_contract must be an object")
        raw_targets = contract.get("targets")
        raw_steps = contract.get("ordered_steps")
        if not isinstance(raw_targets, list) or not raw_targets:
            raise ValueError("implementation_contract.targets is required")
        if not isinstance(raw_steps, list) or not raw_steps:
            raise ValueError("implementation_contract.ordered_steps must be non-empty")

        targets: list[dict[str, Any]] = []
        for item in raw_targets:
            if not isinstance(item, dict):
                raise ValueError("Every implementation target must be an object")
            target = dict(item)
            file_value = self._normalize_target_file(target.get("file"))
            symbols = target.get("symbols")
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
                raise ValueError("Every implementation target requires a symbols array")
            normalized_symbols = list(dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()))
            if not normalized_symbols:
                raise ValueError("Every v2 code target requires a non-empty symbols array")
            target["file"] = file_value
            target["symbols"] = normalized_symbols
            targets.append(target)

        target_keys = {
            (item["file"], tuple(sorted(item["symbols"]))) for item in targets
        }
        if located_targets is not None:
            located_keys = {
                (
                    self._normalize_target_file(item.get("file")),
                    tuple(sorted(
                        str(symbol).strip() for symbol in (item.get("symbols") or [])
                        if str(symbol).strip()
                    )),
                )
                for item in located_targets if isinstance(item, dict)
            }
            if not located_keys or target_keys != located_keys:
                raise ValueError("implementation_contract.targets must exactly match location analysis targets")

        steps: list[dict[str, Any]] = []
        for item in raw_steps:
            if not isinstance(item, dict):
                raise ValueError("Every implementation step must be an object")
            step = dict(item)
            file_value = self._normalize_target_file(step.get("file"))
            symbol_value = str(step.get("symbol") or "").strip()
            action = str(step.get("action") or "").strip()
            if not file_value or not symbol_value or not action:
                raise ValueError("Every implementation step requires file, symbol and action")
            if not any(file_value == target_file and symbol_value in symbols for target_file, symbols in target_keys):
                raise ValueError("Implementation step target is outside locked targets")
            step["file"] = file_value
            step["symbol"] = symbol_value
            step["action"] = action
            steps.append(step)

        canonical = dict(contract)
        canonical["targets"] = targets
        canonical["ordered_steps"] = steps
        return canonical

    def _validate_connected_location_evidence(
        self, project: str, evidence: dict[str, Any],
    ) -> None:
        """Accept bounded CodeGraph, GitNexus, or direct-source location evidence."""
        tool = str(evidence.get("tool") or "")
        if tool == "codegraph_explore":
            pass
        elif tool == "codegraph_cli_explore":
            command = evidence.get("command")
            if (
                not isinstance(command, list)
                or len(command) < 4
                or Path(str(command[0])).name != "codegraph"
                or command[1] != "explore"
            ):
                raise ValueError(
                    "connected CodeGraph CLI evidence must include the executed codegraph explore argv"
                )
            if type(evidence.get("exit_code")) is not int or evidence.get("exit_code") != 0:
                raise ValueError("connected CodeGraph CLI evidence must include exit_code=0")
            path_flag = "--path" if "--path" in command else ("-p" if "-p" in command else "")
            if not path_flag:
                raise ValueError("connected CodeGraph CLI evidence must include the project path")
            path_index = command.index(path_flag)
            cli_project = str(command[path_index + 1]) if path_index + 1 < len(command) else ""
            if (
                not Path(cli_project).expanduser().is_absolute()
                or self._normalize_project(cli_project) != project
            ):
                raise ValueError("connected CodeGraph CLI evidence project path does not match")
            if not str(evidence.get("query") or "").strip():
                raise ValueError("connected CodeGraph CLI evidence must include the bounded query")
        elif tool in {"gitnexus_query", "gitnexus_context"}:
            if not str(evidence.get("query") or "").strip():
                raise ValueError("connected GitNexus evidence must include the bounded query")
            evidence_project = str(evidence.get("project_path") or "")
            if (
                not Path(evidence_project).expanduser().is_absolute()
                or self._normalize_project(evidence_project) != project
            ):
                raise ValueError("connected GitNexus evidence project path does not match")
        elif tool == "gitnexus_cli_query":
            command = evidence.get("command")
            if not isinstance(command, list) or len(command) < 3:
                raise ValueError("connected GitNexus CLI evidence must include the executed query argv")
            executable = Path(str(command[0])).name
            direct_cli = executable == "gitnexus" and command[1] in {"query", "context"}
            repo_script = executable in {"node", "bun"} and any(
                Path(str(item)).name == "run.cjs" for item in command[1:]
            ) and any(str(item) in {"query", "context"} for item in command[1:])
            if not direct_cli and not repo_script:
                raise ValueError("connected GitNexus CLI evidence must include a gitnexus query or context command")
            if type(evidence.get("exit_code")) is not int or evidence.get("exit_code") != 0:
                raise ValueError("connected GitNexus CLI evidence must include exit_code=0")
            if not str(evidence.get("query") or "").strip():
                raise ValueError("connected GitNexus CLI evidence must include the bounded query")
            evidence_project = str(evidence.get("project_path") or "")
            if (
                not Path(evidence_project).expanduser().is_absolute()
                or self._normalize_project(evidence_project) != project
            ):
                raise ValueError("connected GitNexus CLI evidence project path does not match")
        elif tool == "source_match":
            evidence_project = str(evidence.get("project_path") or "")
            if (
                not Path(evidence_project).expanduser().is_absolute()
                or self._normalize_project(evidence_project) != project
            ):
                raise ValueError("direct source-match evidence project path does not match")
            if not str(evidence.get("query") or "").strip():
                raise ValueError("direct source-match evidence must include the bounded query")
            commands = evidence.get("commands")
            if not isinstance(commands, list) or not commands:
                raise ValueError("direct source-match evidence must include the read-only search commands")
        else:
            raise ValueError(
                "connected location evidence must come from CodeGraph, GitNexus, or source_match"
            )
        files = evidence.get("files")
        if (
            not isinstance(files, list)
            or not files
            or any(not isinstance(item, str) or not item.strip() for item in files)
        ):
            raise ValueError("connected location evidence must include non-empty files")

    def create_task(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        if not title:
            raise ValueError("title is required")
        task_type = str(payload.get("type") or "feature").strip().lower()
        task_id_prefix = "BUG" if task_type == "bug" else "TASK"
        list_values = {field: self._string_list(payload, field) for field in JSON_FIELDS}
        priority = str(payload.get("priority", "P2"))
        if priority not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("priority must be P0, P1, P2 or P3")
        token_budget = int(
            payload["token_budget"] if "token_budget" in payload else self.task_token_budget()
        )
        if token_budget <= 0:
            raise ValueError("token_budget must be positive")
        workflow_version = int(payload.get("workflow_version", 2))
        if payload.get("status", "draft") == "ready" and not payload.get("location_analysis_id"):
            self._assert_ready_payload(payload, list_values)
        project = self._require_project_directory(payload.get("project")) if str(payload.get("project") or "").strip() else ""
        with self.db.transaction() as connection:
            location_analysis = self._consume_location_analysis(
                connection, payload.get("location_analysis_id"), project,
                allowed_stages=("creation", "change"),
            )
            planned_criteria = [str(item.get("criterion") or "").strip() for item in location_analysis.get("acceptance_plan", [])]
            confirmed_criteria = [str(item).strip() for item in list_values["acceptance_criteria"]]
            if Counter(planned_criteria) != Counter(confirmed_criteria):
                raise ValueError("Location acceptance plan must exactly match the confirmed acceptance criteria")
            if payload.get("status", "draft") == "ready":
                self._assert_ready_payload(payload, list_values)
            dependency_analysis = payload.get("dependency_analysis")
            implementation_contract = payload.get("implementation_contract")
            review_contract = payload.get("review_contract")
            if workflow_version >= 2 and (not isinstance(dependency_analysis, dict) or not str(dependency_analysis.get("decision") or "").strip()):
                raise ValueError("dependency_analysis.decision is required")
            decision = str(dependency_analysis.get("decision") or "").strip()
            if workflow_version >= 2 and decision not in {"independent", "depends_on", "continues_from"}:
                raise ValueError("dependency_analysis.decision must be independent, depends_on or continues_from")
            declared_relations = payload.get("relations", []) or []
            if not isinstance(declared_relations, list):
                raise ValueError("relations must be an array")
            scheduling_relations = [item for item in declared_relations if isinstance(item, dict) and str(item.get("relation_type") or item.get("type") or "") in {"depends_on", "continues_from"}]
            related_task_id = str(dependency_analysis.get("related_task_id") or dependency_analysis.get("target_task_id") or "").strip()
            if workflow_version >= 2 and decision == "independent" and scheduling_relations:
                raise ValueError("independent dependency analysis cannot include scheduling relations")
            if workflow_version >= 2 and decision in {"depends_on", "continues_from"}:
                if not related_task_id:
                    raise ValueError("A dependent task requires dependency_analysis.related_task_id")
                if len(scheduling_relations) != 1 or str(scheduling_relations[0].get("relation_type") or scheduling_relations[0].get("type") or "") != decision:
                    raise ValueError("dependency analysis and task relation must match exactly")
                relation_source = str(scheduling_relations[0].get("source_task_id") or "").strip()
                if relation_source and relation_source != "<new>" and relation_source != str(payload.get("task_id") or ""):
                    # New task relations may omit source (the service fills it);
                    # an explicit source cannot point at another task.
                    raise ValueError("dependency relation source must be the new task")
                relation_target = str(scheduling_relations[0].get("target_task_id") or scheduling_relations[0].get("task_id") or "").strip()
                if relation_target != related_task_id:
                    raise ValueError("dependency relation target does not match dependency analysis")
            if workflow_version >= 2:
                implementation_contract = self._validate_implementation_contract(
                    implementation_contract, location_analysis.get("targets") or [],
                )
                if not isinstance(implementation_contract, dict) or not implementation_contract.get("targets"):
                    raise ValueError("implementation_contract.targets is required")
                if not isinstance(review_contract, dict):
                    raise ValueError("review_contract must be an object")
                targets = implementation_contract.get("targets")
                if not isinstance(targets, list) or not targets:
                    raise ValueError("v2 implementation_contract.targets must be non-empty")
                target_keys = set()
                for target in targets:
                    if not isinstance(target, dict) or not str(target.get("file") or "").strip():
                        raise ValueError("Every implementation target requires a file")
                    symbols = target.get("symbols")
                    if not isinstance(symbols, list) or not symbols or any(not isinstance(symbol, str) or not symbol.strip() for symbol in symbols):
                        raise ValueError("Every v2 code target requires a non-empty symbols array")
                    target_keys.add((self._normalize_target_file(target["file"]), tuple(sorted(symbol.strip() for symbol in symbols))))
                located_keys = {
                    (self._normalize_target_file(item.get("file")), tuple(sorted(str(symbol).strip() for symbol in (item.get("symbols") or []) if str(symbol).strip())))
                    for item in (location_analysis.get("targets") or []) if isinstance(item, dict)
                }
                contract_keys = {(self._normalize_target_file(item.get("file")), tuple(sorted(str(symbol).strip() for symbol in (item.get("symbols") or []) if str(symbol).strip()))) for item in targets}
                if contract_keys != located_keys:
                    raise ValueError("implementation_contract.targets must exactly match location analysis targets")
                steps = implementation_contract.get("ordered_steps")
                if not isinstance(steps, list) or not steps:
                    raise ValueError("v2 implementation_contract.ordered_steps must be non-empty")
                for step in steps:
                    if not isinstance(step, dict) or not str(step.get("file") or "").strip() or not str(step.get("symbol") or "").strip() or not str(step.get("action") or "").strip():
                        raise ValueError("Every implementation step requires file, symbol and action")
                    step_key = (self._normalize_target_file(step["file"]), str(step["symbol"]).strip())
                    if not any(step_key[0] == file and step_key[1] in symbols for file, symbols in target_keys):
                        raise ValueError("Implementation step target is outside locked targets")
                checks = review_contract.get("checks")
                if not isinstance(checks, list) or not checks or any(not isinstance(item, str) or not item.strip() for item in checks):
                    raise ValueError("v2 review_contract.checks must be a non-empty array")
                separate_acceptance = review_contract.get("separate_acceptance_session", False)
                if not isinstance(separate_acceptance, bool):
                    raise ValueError("review_contract.separate_acceptance_session must be boolean")
            task_id = self.db.next_id(connection, task_id_prefix)
            connection.execute(
                """INSERT INTO tasks(
                    id, requirement_id, title, type, project, modules, status,
                    priority, goal, scope, out_of_scope, acceptance_criteria,
                    source_thread_id, token_budget, location_context, acceptance_plan,
                    dependency_analysis, implementation_contract, review_contract,
                    parent_acceptance_task_id, workflow_version
                ) VALUES(?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    task_id, title, task_type, project,
                    json.dumps(list_values["modules"], ensure_ascii=False), payload.get("status", "draft"),
                    priority, payload.get("goal", ""),
                    json.dumps(list_values["scope"], ensure_ascii=False),
                    json.dumps(list_values["out_of_scope"], ensure_ascii=False),
                    json.dumps(list_values["acceptance_criteria"], ensure_ascii=False),
                    payload.get("source_thread_id"), token_budget,
                    json.dumps(location_analysis, ensure_ascii=False),
                    json.dumps(location_analysis.get("acceptance_plan", []), ensure_ascii=False),
                    json.dumps(dependency_analysis, ensure_ascii=False),
                    json.dumps(implementation_contract, ensure_ascii=False),
                    json.dumps(review_contract, ensure_ascii=False),
                    payload.get("parent_acceptance_task_id"), workflow_version,
                ),
            )
            if payload.get("source_thread_id"):
                connection.execute(
                    "INSERT OR IGNORE INTO task_conversations(task_id, role, thread_id, title) VALUES(?, 'source', ?, ?)",
                    (task_id, payload["source_thread_id"], f"{task_id} 需求确认"),
                )
            self._store_task_targets(connection, task_id, location_analysis.get("targets", []))
            for relation in payload.get("relations", []) or []:
                if not isinstance(relation, dict):
                    raise ValueError("relations must contain objects")
                source = str(relation.get("source_task_id") or task_id)
                if source == "<new>":
                    source = task_id
                target = str(relation.get("target_task_id") or "")
                relation_type = str(relation.get("relation_type") or relation.get("type") or "")
                if source == task_id and not target:
                    target = str(relation.get("task_id") or "")
                if source != task_id and target == task_id:
                    pass
                if not target or relation_type not in RELATION_TYPES:
                    raise ValueError("Each relation requires target_task_id and valid relation_type")
                if source == target:
                    raise ValueError("A task cannot relate to itself")
                exists = connection.execute("SELECT 1 FROM tasks WHERE id=?", (target if source == task_id else source,)).fetchone()
                if not exists:
                    raise ValueError(f"Related task not found: {target if source == task_id else source}")
                self._insert_relation_in_connection(connection, source, target, relation_type, str(relation.get("description") or ""))
            conflicts = self._target_conflicts(connection, task_id)
            batch_id = self._try_join_open_batch(connection, task_id)
            self._event(connection, "task", task_id, "created", payload)
            if conflicts:
                self._event(connection, "task", task_id, "target_conflict_detected", {"conflicts": conflicts})
            if batch_id:
                self._event(
                    connection,
                    "task",
                    task_id,
                    "auto_batched",
                    {"batch_id": batch_id},
                )
            self._queue_obsidian_sync(connection, "task", task_id)
        task = self.get_task(task_id)
        self.flush_integration_outbox()
        return task

    @staticmethod
    def _insert_relation_in_connection(connection: Any, source: str, target: str, relation_type: str, description: str = "") -> None:
        if relation_type in {"depends_on", "blocks", "continues_from"}:
            dependent, prerequisite = ((source, target) if relation_type != "blocks" else (target, source))
            rows = connection.execute(
                "SELECT source_task_id, target_task_id, relation_type FROM task_relations WHERE relation_type IN ('depends_on','blocks','continues_from')"
            ).fetchall()
            adjacency: dict[str, set[str]] = {}
            for row in rows:
                left, right = ((row["source_task_id"], row["target_task_id"])
                               if row["relation_type"] != "blocks" else (row["target_task_id"], row["source_task_id"]))
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
            (source, target, relation_type, description),
        )

    @staticmethod
    def _store_task_targets(connection: Any, task_id: str, targets: list[dict[str, Any]]) -> None:
        for target in targets:
            raw_file = str(target.get("file") or "").strip()
            if not raw_file:
                continue
            file = ProjectWorkspaceGuard.normalize_target_file(raw_file)
            symbols = sorted({str(symbol).strip() for symbol in target.get("symbols", []) if str(symbol).strip()}) or [""]
            connection.executemany(
                "INSERT OR IGNORE INTO task_targets(task_id, file, symbol) VALUES(?, ?, ?)",
                [(task_id, file, symbol) for symbol in symbols],
            )

    @staticmethod
    def _target_conflicts(connection: Any, task_id: str, locking_only: bool = False) -> list[dict[str, Any]]:
        status_filter = (
            "AND other.status IN ('claimed','investigating','implementing','waiting_confirmation','review','failed','blocked')"
            if locking_only else "AND other.status NOT IN ('done','cancelled')"
        )
        rows = connection.execute(
            f"""SELECT DISTINCT other.id AS task_id, other.title, other.status,
                               mine.file, mine.symbol
                FROM task_targets mine
                JOIN tasks current ON current.id=mine.task_id
                JOIN task_targets theirs
                  ON theirs.task_id != mine.task_id
                 AND theirs.file=mine.file
                 AND (mine.symbol='' OR theirs.symbol='' OR theirs.symbol=mine.symbol)
                JOIN tasks other ON other.id=theirs.task_id AND other.project=current.project
                WHERE mine.task_id=? {status_filter}
                ORDER BY other.id, mine.file, mine.symbol""",
            (task_id,),
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            conflict = grouped.setdefault(row["task_id"], {
                "task_id": row["task_id"], "title": row["title"], "status": row["status"], "targets": [],
            })
            target = {"file": row["file"], "symbol": row["symbol"]}
            if target not in conflict["targets"]:
                conflict["targets"].append(target)
        return list(grouped.values())

    def target_conflicts(self, task_id: str, locking_only: bool = False) -> list[dict[str, Any]]:
        connection = self.db.connect()
        try:
            return self._target_conflicts(connection, task_id, locking_only)
        finally:
            connection.close()

    @staticmethod
    def _project_blockers(connection: Any, task_id: str) -> list[dict[str, Any]]:
        rows = connection.execute(
            """SELECT other.id AS task_id, other.title, other.status,
                      other.paused_from_status, other.retry_required,
                      other.last_failure_reason,
                      EXISTS(SELECT 1 FROM task_runs active
                             WHERE active.task_id=other.id
                               AND active.status IN ('awaiting_thread','running')) AS active_run
               FROM tasks current JOIN tasks other
                 ON other.project=current.project AND other.id != current.id
               WHERE current.id=? AND other.status NOT IN ('done','cancelled') AND (
                 other.status IN ('failed','blocked','waiting_confirmation')
                 OR (other.status='paused' AND other.paused_from_status IN ('claimed','investigating','implementing','review','code_review','acceptance','acceptance_blocked','rework'))
                 OR other.retry_required=1
                 OR EXISTS(SELECT 1 FROM task_runs active
                           WHERE active.task_id=other.id
                             AND active.status IN ('awaiting_thread','running'))
               )
               ORDER BY other.updated_at, other.id""",
            (task_id,),
        ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            if item["active_run"]:
                blocker_type = "active_project_run"
            elif item["status"] == "failed" or item["retry_required"]:
                blocker_type = "retry_pending"
            else:
                blocker_type = item["status"]
            item["blocker_type"] = blocker_type
            result.append(item)
        return result

    def report_location_status(
        self,
        project: str,
        available: bool,
        state: str,
        summary: str,
        evidence: dict[str, Any],
        agent_id: str = "",
    ) -> dict[str, Any]:
        project = self._normalize_project(project)
        if not project:
            raise ValueError("project is required")
        if state not in {"connected", "stale", "error"}:
            raise ValueError("state must be connected, stale or error")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("evidence is required")
        if available != (state in {"connected", "stale"}):
            raise ValueError("available must match state")
        evidence = dict(evidence)
        if state == "connected":
            self._validate_connected_location_evidence(project, evidence)
        workspace = self._workspace_state(project)
        if workspace.get("available"):
            evidence["repository_revision"] = workspace["revision"]
            evidence["repository_workspace_fingerprint"] = workspace["fingerprint"]
        with self.db.transaction() as connection:
            connection.execute(
                """INSERT INTO location_reports(project, available, state, summary, evidence, agent_id)
                   VALUES(?, ?, ?, ?, ?, ?)
                   ON CONFLICT(project) DO UPDATE SET
                     available=excluded.available, state=excluded.state,
                     summary=excluded.summary, evidence=excluded.evidence,
                     agent_id=excluded.agent_id, checked_at=CURRENT_TIMESTAMP,
                     updated_at=CURRENT_TIMESTAMP""",
                (project, int(available), state, str(summary or "")[:2000], json.dumps(evidence, ensure_ascii=False), str(agent_id or "")),
            )
            self._event(connection, "integration", project, "location_status_reported", {"available": available, "state": state, "agent_id": agent_id})
        return self.location_status(project)

    def location_status(self, project: str | None) -> dict[str, Any]:
        project = self._normalize_project(project)
        if not project:
            return {"available": False, "reason": "project_path_missing", "mode": "agent_reported"}
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM location_reports WHERE project=?", (project,)).fetchone()
        if not row:
            return {
                "available": False,
                "reason": "agent_report_missing",
                "mode": "agent_reported",
                "project_path": project,
                "instruction": "Probe CodeGraph, then GitNexus, then bounded direct source matching; report the selected evidence with report_location_status.",
            }
        report = dict(row)
        checked_at = datetime.fromisoformat(str(report["checked_at"]).replace("Z", "+00:00"))
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        age_seconds = max(0, int((datetime.now(timezone.utc) - checked_at).total_seconds()))
        evidence = json.loads(report["evidence"] or "{}")
        reported_revision = str(evidence.get("repository_revision") or "")
        current_workspace = self._workspace_state(project)
        current_revision = str(current_workspace.get("revision") or "")
        reported_fingerprint = str(evidence.get("repository_workspace_fingerprint") or "")
        current_fingerprint = str(current_workspace.get("fingerprint") or "")
        revision_matches = not reported_revision or not current_revision or reported_revision == current_revision
        workspace_matches = not reported_fingerprint or not current_fingerprint or reported_fingerprint == current_fingerprint
        fresh = age_seconds <= LOCATION_REPORT_MAX_AGE_SECONDS and revision_matches and workspace_matches
        if not revision_matches:
            stale_reason = "repository_revision_changed"
        elif not workspace_matches:
            stale_reason = "repository_workspace_changed"
        elif age_seconds > LOCATION_REPORT_MAX_AGE_SECONDS:
            stale_reason = "agent_report_expired"
        else:
            stale_reason = "agent_reported_error"
        return {
            "available": bool(report["available"]) and fresh,
            "state": report["state"] if fresh else "stale",
            "reason": "" if report["available"] and fresh else stale_reason,
            "mode": "agent_reported",
            "source": "codex-agent",
            "project_path": project,
            "summary": report["summary"],
            "output": report["summary"],
            "evidence": evidence,
            "reported_revision": reported_revision,
            "current_revision": current_revision,
            "reported_workspace_fingerprint": reported_fingerprint,
            "current_workspace_fingerprint": current_fingerprint,
            "agent_id": report["agent_id"],
            "checked_at": report["checked_at"],
            "age_seconds": age_seconds,
            "max_age_seconds": LOCATION_REPORT_MAX_AGE_SECONDS,
        }

    def prepare_location_analysis(
        self, payload: dict[str, Any], stage: str = "creation", task_id: str | None = None,
        delivery_run_id: str | None = None,
    ) -> dict[str, Any]:
        if stage not in {"creation", "change", "review"}:
            raise ValueError("stage must be creation, change or review")
        project = self._require_project_directory(payload.get("project"))
        if task_id:
            task = self.get_task(task_id)
            if task.get("project") != project:
                raise ValueError("project does not match task")
        modules = payload.get("modules") or []
        if not isinstance(modules, list) or any(not isinstance(item, str) for item in modules):
            raise ValueError("modules must be an array of strings")
        if stage == "review" and not delivery_run_id:
            raise ValueError("Review location analysis must be bound to a delivery run")
        delivery_attempt = self.get_run(delivery_run_id)["attempt"] if delivery_run_id else None
        query = " ".join(str(value) for value in [payload.get("title", ""), payload.get("goal", ""), *modules] if value).strip()
        if not query:
            raise ValueError("title, goal or modules are required for location analysis")
        dependency_candidates = self.obsidian.search_task_dependencies(
            str(payload.get("title") or ""), str(payload.get("goal") or ""), modules,
        )
        obsidian = {
            "status": self.obsidian.status(),
            "dependency_candidates": dependency_candidates,
        }
        plan_task = {"title": payload.get("title", ""), "goal": payload.get("goal", ""), "modules": payload.get("modules", [])}
        location_plan = {"status": self.location_status(project), "query_plan": self.location.query_plan(plan_task)}
        with self.db.transaction() as connection:
            analysis_id = self.db.next_id(connection, "LOC")
            connection.execute(
                """INSERT INTO location_analyses(id, stage, task_id, delivery_run_id, delivery_attempt, project, query,
                   obsidian_evidence, location_plan, dependency_analysis, implementation_contract, review_contract)
                   VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (analysis_id, stage, task_id, delivery_run_id, delivery_attempt, project, query,
                 json.dumps(obsidian, ensure_ascii=False), json.dumps(location_plan, ensure_ascii=False),
                 json.dumps(payload.get("dependency_analysis") or {}, ensure_ascii=False),
                 json.dumps(payload.get("implementation_contract") or {}, ensure_ascii=False),
                 json.dumps(payload.get("review_contract") or {}, ensure_ascii=False)),
            )
        instruction = (
            "After one bounded location route, call finalize_task_intake once with unique targets, "
            "ordered steps, review checks and acceptance items."
            if stage == "creation" else
            "Use the first usable bounded location route in order: CodeGraph, GitNexus, then direct "
            "source matching. Report that evidence, then complete the location analysis."
        )
        return {"analysis_id": analysis_id, "stage": stage, "project": project,
                "obsidian": {"status": obsidian["status"], "candidate_count": len(dependency_candidates)},
                "location": location_plan,
                "instruction": instruction}

    def _classify_task_dependencies(
        self, payload: dict[str, Any], candidates: list[dict[str, Any]],
    ) -> dict[str, Any]:
        project = self._normalize_project(payload.get("project"))
        title = str(payload.get("title") or "").strip(); goal = str(payload.get("goal") or "").strip()
        modules = payload.get("modules") or []; symbols = payload.get("located_symbols") or []
        if not title or not goal or not project: raise ValueError("title, goal and project are required")
        valid = []
        ignored = []
        requested_modules = specific_modules(modules)
        requested_terms = (
            _search_tokens(" ".join((title, goal, *symbols))) - GENERIC_MATCH_TERMS
        )
        with self.db.connection() as connection:
            for item in candidates:
                if not item.get("task_id"): continue
                row = connection.execute("SELECT * FROM tasks WHERE id=?", (item["task_id"],)).fetchone()
                if not row:
                    continue
                task = _decode_row(row)
                item.update({
                    "title": task["title"],
                    "status": task["status"],
                    "project": task["project"],
                    "codex_thread_id": task["codex_thread_id"],
                })
                if task["project"] != project:
                    item["ignored_reason"] = "different_project"
                    ignored.append(item)
                    continue
                if task["status"] in {"done", "cancelled"}:
                    item["ignored_reason"] = "terminal_task"
                    ignored.append(item)
                    continue
                module_overlap = requested_modules & specific_modules(task.get("modules", []))
                task_terms = (
                    _search_tokens(
                        " ".join((task.get("title", ""), task.get("goal", "")))
                    )
                    - GENERIC_MATCH_TERMS
                )
                term_overlap = requested_terms & task_terms
                if not module_overlap and len(term_overlap) < 2:
                    item["ignored_reason"] = "weak_match"
                    ignored.append(item)
                    continue
                item["module_overlap"] = sorted(module_overlap)
                item["term_overlap"] = sorted(term_overlap)[:8]
                valid.append(item)
        return {
            "decision": "independent" if not valid else "requires_confirmation",
            "candidates": valid,
            "ignored_candidates": ignored,
            "obsidian": {"status": self.obsidian.status(), "evidence": candidates},
            "instruction": (
                "无强匹配活动任务时直接使用 independent；"
                "否则确认 independent、depends_on 或 continues_from 后原子创建关系。"
            ),
        }

    def analyze_task_dependencies(self, payload: dict[str, Any]) -> dict[str, Any]:
        title = str(payload.get("title") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        modules = payload.get("modules") or []
        symbols = payload.get("located_symbols") or []
        candidates = self.obsidian.search_task_dependencies(title, goal, modules, symbols)
        return self._classify_task_dependencies(payload, candidates)

    def finalize_task_intake(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Create a ready task from one non-duplicated, model-authored intake bundle."""
        analysis_id = str(payload.get("analysis_id") or "").strip()
        if not analysis_id:
            raise ValueError("analysis_id is required")
        analysis = self.get_location_analysis(analysis_id)
        if analysis.get("stage") != "creation" or analysis.get("status") != "prepared":
            raise ValueError("A prepared creation location analysis is required")
        project = self._require_project_directory(payload.get("project"))
        if self._normalize_project(analysis.get("project")) != project:
            raise ValueError("Location analysis project does not match task project")
        if not str(payload.get("title") or "").strip():
            raise ValueError("title is required")
        if not str(payload.get("goal") or "").strip():
            raise ValueError("goal is required")
        for field in ("modules", "scope", "out_of_scope"):
            value = payload.get(field, [])
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                raise ValueError(f"{field} must be an array of strings")

        evidence = payload.get("location_evidence")
        targets = payload.get("targets")
        ordered_steps = payload.get("ordered_steps")
        acceptance_plan = payload.get("acceptance_plan")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("location_evidence is required")
        if not isinstance(targets, list) or not targets:
            raise ValueError("targets must be a non-empty array")
        if not isinstance(ordered_steps, list) or not ordered_steps:
            raise ValueError("ordered_steps must be a non-empty array")
        if not isinstance(acceptance_plan, list) or not acceptance_plan:
            raise ValueError("acceptance_plan must be a non-empty array")
        review_checks = self._string_list(payload, "review_checks")
        if not review_checks:
            raise ValueError("review_checks must be a non-empty array")

        self.report_location_status(
            project, True, "connected",
            str(payload.get("location_summary") or "单次 intake 定位包已确认")[:2000],
            evidence, str(payload.get("agent_id") or ""),
        )

        located_symbols = list(dict.fromkeys(
            str(symbol).strip()
            for target in targets if isinstance(target, dict)
            for symbol in (target.get("symbols") or [])
            if str(symbol).strip()
        ))
        dependency_analysis = payload.get("dependency_analysis")
        if dependency_analysis is None:
            cached_candidates = (
                analysis.get("obsidian_evidence", {}).get("dependency_candidates", [])
                if isinstance(analysis.get("obsidian_evidence"), dict) else []
            )
            dependency_result = self._classify_task_dependencies({
                "title": payload.get("title"), "goal": payload.get("goal"),
                "project": project, "modules": payload.get("modules") or [],
                "located_symbols": located_symbols,
            }, cached_candidates)
            if dependency_result.get("decision") == "requires_confirmation":
                return {
                    "status": "requires_confirmation",
                    "analysis_id": analysis_id,
                    "dependency_analysis": dependency_result,
                }
            dependency_analysis = {"decision": "independent"}
        if not isinstance(dependency_analysis, dict):
            raise ValueError("dependency_analysis must be an object")

        decision = str(dependency_analysis.get("decision") or "").strip()
        if decision not in {"independent", "depends_on", "continues_from"}:
            raise ValueError("dependency_analysis.decision must be independent, depends_on or continues_from")
        raw_relations = payload.get("relations") or []
        if not isinstance(raw_relations, list) or any(not isinstance(item, dict) for item in raw_relations):
            raise ValueError("relations must be an array of objects")
        relations = list(raw_relations)
        if decision in {"depends_on", "continues_from"}:
            target_task_id = str(
                dependency_analysis.get("target_task_id")
                or dependency_analysis.get("related_task_id") or ""
            ).strip()
            if not target_task_id:
                raise ValueError("A dependent task requires dependency_analysis.target_task_id")
            if not any(
                isinstance(item, dict)
                and str(item.get("relation_type") or item.get("type") or "") in {"depends_on", "continues_from"}
                for item in relations
            ):
                relations.append({"target_task_id": target_task_id, "relation_type": decision})

        implementation_contract = {"targets": targets, "ordered_steps": ordered_steps}
        review_contract = {
            "checks": review_checks,
            "separate_acceptance_session": bool(payload.get("separate_acceptance_session", False)),
        }
        completed = self.complete_location_analysis(
            analysis_id, evidence, targets, acceptance_plan, dependency_analysis,
            implementation_contract, review_contract,
        )
        task_payload = {
            key: payload[key]
            for key in (
                "title", "project", "modules", "goal", "scope", "out_of_scope",
                "priority", "source_thread_id", "type", "token_budget", "auto_dispatch",
            )
            if key in payload
        }
        task_payload.update({
            "acceptance_criteria": [
                str(item.get("criterion") or "").strip()
                for item in acceptance_plan if isinstance(item, dict)
            ],
            "location_analysis_id": completed["id"],
            "dependency_analysis": dependency_analysis,
            "implementation_contract": implementation_contract,
            "review_contract": review_contract,
            "relations": relations,
            "workflow_version": 2,
        })
        task_payload["status"] = "ready"
        task = self.create_task(task_payload)
        return {
            "status": "created", "analysis_id": analysis_id,
            "task_id": task["id"], "task_status": task["status"],
        }

    def complete_location_analysis(
        self, analysis_id: str, location_evidence: dict[str, Any], targets: list[dict[str, Any]],
        acceptance_plan: list[dict[str, Any]], dependency_analysis: dict[str, Any] | None = None,
        implementation_contract: dict[str, Any] | None = None, review_contract: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        analysis = self.get_location_analysis(analysis_id)
        location_status = self.location_status(analysis["project"])
        if not location_status.get("available") or location_status.get("state") != "connected":
            raise ValueError("A connected location evidence report from CodeGraph, GitNexus, or source matching is required")
        if not location_evidence:
            raise ValueError("location_evidence is required")
        if not isinstance(targets, list) or not targets or any(not isinstance(item, dict) for item in targets):
            raise ValueError("At least one located target is required")
        target_map: dict[str, set[str]] = {}
        for target in targets:
            file = self._normalize_target_file(target.get("file"))
            symbols = target.get("symbols", [])
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
                raise ValueError("Target symbols must be an array of strings")
            target["file"] = file
            target["symbols"] = [symbol.strip() for symbol in symbols if symbol.strip()]
            if not target["symbols"] and Path(file).suffix.lower() not in {".json", ".yaml", ".yml", ".toml", ".ini", ".sql", ".md", ".txt", ".css", ".scss", ".html"}:
                raise ValueError(f"Code target requires a component or method symbol: {file}")
            target.setdefault("reason", "")
            target_map.setdefault(file, set()).update(
                str(symbol).strip() for symbol in target["symbols"] if str(symbol).strip()
            )
        if not isinstance(acceptance_plan, list) or not acceptance_plan or any(not isinstance(item, dict) for item in acceptance_plan):
            raise ValueError("acceptance_plan is required")
        for item in acceptance_plan:
            file = self._normalize_target_file(item.get("file"))
            symbol = str(item.get("symbol") or "").strip()
            if not item.get("criterion") or not file or not item.get("method") or not item.get("expected"):
                raise ValueError("Every acceptance plan item requires criterion, file, method and expected")
            check_type = str(item.get("check_type") or ("automated" if item.get("command") else "static_review"))
            if check_type not in {"automated", "static_review", "manual_runtime"}:
                raise ValueError("Acceptance check_type must be automated, static_review or manual_runtime")
            if check_type == "automated" and not str(item.get("command") or "").strip():
                raise ValueError("Automated acceptance checks require command")
            item["check_type"] = check_type
            item["required"] = bool(item.get("required", True))
            item["timeout_seconds"] = max(1, min(int(item.get("timeout_seconds", 300)), 1800))
            if file not in target_map:
                raise ValueError(f"Acceptance plan points outside located targets: {file}")
            if target_map[file] and (not symbol or symbol not in target_map[file]):
                raise ValueError(f"Acceptance plan symbol is outside located targets: {file}#{symbol or '<missing>'}")
            item["file"] = file
        if implementation_contract is not None:
            implementation_contract = self._validate_implementation_contract(
                implementation_contract, targets,
            )
        if analysis["stage"] == "review":
            task = self.get_task(analysis["task_id"])
            if not analysis.get("delivery_run_id"):
                raise ValueError("Review location analysis is not bound to a delivery run")
            delivery = self.get_run(analysis["delivery_run_id"])
            if delivery["task_id"] != task["id"] or delivery["id"] != task.get("primary_run_id") or (
                delivery["status"] != "waiting_review"
                and not (delivery["run_type"] == "review" and delivery["status"] in ACTIVE_RUN_STATUSES)
            ) or (analysis.get("delivery_attempt") is not None and analysis["delivery_attempt"] != delivery["attempt"]):
                raise ValueError("Review location analysis delivery is no longer current")
            delivered_targets: dict[str, set[str]] = {}
            for location in delivery.get("changed_locations", []):
                delivered_targets.setdefault(self._normalize_target_file(location.get("file")), set()).update(
                    str(symbol).strip() for symbol in location.get("symbols", []) if str(symbol).strip()
                )
            evidence_files = {
                self._normalize_target_file(file)
                for file in location_evidence.get("files", [])
                if isinstance(file, str) and file.strip()
            }
            for file, symbols in target_map.items():
                if file not in delivered_targets and file not in evidence_files:
                    raise ValueError(f"Review target is outside delivered changes and location evidence: {file}")
                if file in delivered_targets:
                    delivered_symbols = delivered_targets[file]
                    if delivered_symbols and (not symbols or not symbols.issubset(delivered_symbols)):
                        raise ValueError(f"Review symbols are outside the delivered changes: {file}")
            if Counter(str(item.get("criterion") or "").strip() for item in acceptance_plan) != Counter(
                str(item).strip() for item in task.get("acceptance_criteria", [])
            ):
                raise ValueError("Review acceptance plan must exactly match the confirmed acceptance criteria")
            original_required_automated = {
                (str(item.get("criterion") or "").strip(), str(item.get("command") or "").strip())
                for item in task.get("acceptance_plan", [])
                if bool(item.get("required", True))
                and str(item.get("check_type") or ("automated" if item.get("command") else "static_review")) == "automated"
            }
            review_required_automated = {
                (str(item.get("criterion") or "").strip(), str(item.get("command") or "").strip())
                for item in acceptance_plan
                if bool(item.get("required", True)) and item.get("check_type") == "automated"
            }
            if not original_required_automated.issubset(review_required_automated):
                raise ValueError("Review acceptance plan cannot remove or downgrade required automated checks")
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE location_analyses SET location_evidence=?, targets=?,
                   dependency_analysis=?, implementation_contract=?, review_contract=?,
                   status='completed', completed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='prepared'""",
                (json.dumps(location_evidence, ensure_ascii=False), json.dumps(targets, ensure_ascii=False),
                 json.dumps(dependency_analysis or analysis.get("dependency_analysis") or {"decision": "independent"}, ensure_ascii=False),
                 json.dumps(implementation_contract or analysis.get("implementation_contract") or {"targets": targets}, ensure_ascii=False),
                 json.dumps(review_contract or analysis.get("review_contract") or {}, ensure_ascii=False), analysis_id),
            )
            if cursor.rowcount != 1:
                raise ValueError("Location analysis is missing or already completed")
            connection.execute("UPDATE location_analyses SET acceptance_plan=? WHERE id=?", (json.dumps(acceptance_plan, ensure_ascii=False), analysis_id))
        return self.get_location_analysis(analysis_id)

    def get_location_analysis(self, analysis_id: str) -> dict[str, Any]:
        with self.db.connection() as connection:
            row = connection.execute("SELECT * FROM location_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            raise KeyError(f"Location analysis not found: {analysis_id}")
        item = dict(row)
        for field in ("obsidian_evidence", "location_plan", "location_evidence", "targets", "acceptance_plan",
                      "dependency_analysis", "implementation_contract", "review_contract"):
            item[field] = json.loads(item[field] or ("[]" if field in {"targets", "acceptance_plan"} else "{}"))
        return item

    def _consume_location_analysis(
        self, connection: Any, analysis_id: str | None, project: str | None,
        allowed_stages: tuple[str, ...] = ("creation",), task_id: str | None = None,
    ) -> dict[str, Any]:
        if not analysis_id:
            raise ValueError("location_analysis_id is required before a task can enter the queue")
        row = connection.execute("SELECT * FROM location_analyses WHERE id=?", (analysis_id,)).fetchone()
        if not row:
            raise KeyError(f"Location analysis not found: {analysis_id}")
        analysis = dict(row)
        for field in ("obsidian_evidence", "location_plan", "location_evidence", "targets", "acceptance_plan",
                      "dependency_analysis", "implementation_contract", "review_contract"):
            analysis[field] = json.loads(analysis[field] or ("[]" if field in {"targets", "acceptance_plan"} else "{}"))
        if analysis["stage"] not in allowed_stages or analysis["status"] != "completed" or analysis["consumed_at"]:
            expected = " or ".join(allowed_stages)
            raise ValueError(f"A completed, unused {expected} location analysis is required")
        if task_id is not None and analysis.get("task_id") != task_id:
            raise ValueError("Location analysis task does not match the revision target")
        if self._normalize_project(analysis["project"]) != self._normalize_project(project):
            raise ValueError("Location analysis project does not match task project")
        cursor = connection.execute("UPDATE location_analyses SET consumed_at=CURRENT_TIMESTAMP WHERE id=? AND consumed_at IS NULL", (analysis_id,))
        if cursor.rowcount != 1:
            raise ValueError("Location analysis was consumed concurrently")
        return analysis

    @staticmethod
    def _assert_ready_payload(payload: dict[str, Any], list_values: dict[str, Any]) -> None:
        missing = []
        if not str(payload.get("goal") or "").strip():
            missing.append("goal")
        if not list_values.get("acceptance_criteria"):
            missing.append("acceptance_criteria")
        if not str(payload.get("project") or "").strip():
            missing.append("project")
        if missing:
            raise ValueError(f"Task is not ready; missing: {', '.join(missing)}")
