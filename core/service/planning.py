from __future__ import annotations

import json
import hashlib
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from taskboard.project_guard import ProjectWorkspaceGuard
from ..workflow import default_code_review_checks
from .domain import (
    GENERIC_MATCH_TERMS,
    JSON_FIELDS,
    LOCATION_REPORT_MAX_AGE_SECONDS,
    RELATION_TYPES,
    decode_row,
    search_tokens,
    specific_modules,
)


class TaskPlanningMixin:
    """Task creation, dependency analysis, target locks, and bounded location gates."""

    @staticmethod
    def _normalize_dependency_analysis(value: Any) -> dict[str, Any]:
        if not isinstance(value, dict):
            raise ValueError("dependency_analysis must be an object")
        unknown = set(value) - {
            "decision", "depends_tasks", "conflicts_tasks", "history_tasks",
            "history_edges", "continues_from_task_id", "relation_evidence",
        }
        if unknown:
            raise ValueError(
                f"Unknown dependency_analysis fields: {', '.join(sorted(unknown))}"
            )

        def task_ids(field: str) -> list[str]:
            raw = value.get(field) or []
            if not isinstance(raw, list):
                raise ValueError(f"dependency_analysis.{field} must be an array")
            result = list(dict.fromkeys(str(item).strip() for item in raw if str(item).strip()))
            if any(not item.startswith(("TASK-", "BUG-")) for item in result):
                raise ValueError(f"dependency_analysis.{field} contains an invalid task id")
            return result

        depends = task_ids("depends_tasks")
        conflicts = task_ids("conflicts_tasks")
        history = task_ids("history_tasks")
        decision = str(value.get("decision") or "").strip()
        continues_from = str(value.get("continues_from_task_id") or "").strip()
        raw_relation_evidence = value.get("relation_evidence") or []
        if not isinstance(raw_relation_evidence, list) or any(
            not isinstance(item, dict) for item in raw_relation_evidence
        ):
            raise ValueError("dependency_analysis.relation_evidence must be an array of objects")
        relation_evidence: list[dict[str, Any]] = []
        allowed_relation_kinds = {
            "depends_on": {"artifact_dependency"},
            "continues_from": {"thread_continuation"},
            "conflicts_with": {"target_overlap"},
        }
        for item in raw_relation_evidence:
            unknown = set(item) - {
                "task_id", "relation_type", "confidence", "kind", "reason",
                "source", "files", "symbols",
            }
            if unknown:
                raise ValueError(
                    f"Unknown relation evidence fields: {', '.join(sorted(unknown))}"
                )
            task_id = str(item.get("task_id") or "").strip()
            relation_type = str(item.get("relation_type") or "").strip()
            confidence = str(item.get("confidence") or "").strip().lower()
            kind = str(item.get("kind") or "").strip()
            reason = str(item.get("reason") or "").strip()
            source = str(item.get("source") or "").strip()
            files = list(dict.fromkeys(
                str(path).strip() for path in (item.get("files") or [])
                if str(path).strip()
            ))
            symbols = list(dict.fromkeys(
                str(symbol).strip() for symbol in (item.get("symbols") or [])
                if str(symbol).strip()
            ))
            if not task_id.startswith(("TASK-", "BUG-")):
                raise ValueError("Relation evidence task_id is invalid")
            if relation_type not in allowed_relation_kinds:
                raise ValueError("Relation evidence relation_type is invalid")
            if confidence not in {"high", "medium", "low"}:
                raise ValueError("Relation evidence confidence is invalid")
            if kind not in allowed_relation_kinds[relation_type]:
                raise ValueError("Relation evidence kind does not prove its relation type")
            if not reason or not source or not (files or symbols):
                raise ValueError(
                    "Relation evidence requires reason, source and exact files or symbols"
                )
            normalized_evidence = {
                "task_id": task_id,
                "relation_type": relation_type,
                "confidence": confidence,
                "kind": kind,
                "reason": reason,
                "source": source,
                "files": files,
                "symbols": symbols,
            }
            if normalized_evidence not in relation_evidence:
                relation_evidence.append(normalized_evidence)
            if confidence == "high":
                if relation_type == "depends_on" and task_id not in depends:
                    depends.append(task_id)
                elif relation_type == "conflicts_with" and task_id not in conflicts:
                    conflicts.append(task_id)
                elif relation_type == "continues_from":
                    if continues_from and continues_from != task_id:
                        raise ValueError("Only one high-confidence continuation is allowed")
                    continues_from = task_id
        if continues_from:
            decision = "continues_from"
        elif depends:
            decision = "depends_on"
        if decision not in {"independent", "depends_on", "continues_from"}:
            raise ValueError(
                "dependency_analysis.decision must be independent, depends_on or continues_from"
            )
        if decision == "independent" and (depends or continues_from):
            raise ValueError("independent dependency analysis cannot include scheduling dependencies")
        if decision == "depends_on" and not depends:
            raise ValueError("depends_on dependency analysis requires depends_tasks")
        if decision == "continues_from" and not continues_from:
            raise ValueError("continues_from dependency analysis requires continues_from_task_id")
        if continues_from and not continues_from.startswith(("TASK-", "BUG-")):
            raise ValueError("dependency_analysis.continues_from_task_id is invalid")
        if set(depends) & set(conflicts):
            raise ValueError("A task cannot both depend on and conflict with the same task")

        raw_edges = value.get("history_edges") or []
        if not isinstance(raw_edges, list) or any(not isinstance(item, dict) for item in raw_edges):
            raise ValueError("dependency_analysis.history_edges must be an array of objects")
        history_edges: list[dict[str, str]] = []
        for item in raw_edges:
            unknown = set(item) - {"from", "to", "type"}
            if unknown:
                raise ValueError(
                    f"Unknown history edge fields: {', '.join(sorted(unknown))}"
                )
            source = str(item.get("from") or "").strip()
            target = str(item.get("to") or "").strip()
            relation_type = str(item.get("type") or "").strip()
            if not source or not target or relation_type not in RELATION_TYPES:
                raise ValueError("Every history edge requires from, to and a valid relation type")
            edge = {"from": source, "to": target, "type": relation_type}
            if edge not in history_edges:
                history_edges.append(edge)

        normalized = {
            "decision": decision,
            "depends_tasks": depends,
            "conflicts_tasks": conflicts,
            "history_tasks": history,
            "history_edges": history_edges,
            "relation_evidence": relation_evidence,
        }
        if continues_from:
            normalized["continues_from_task_id"] = continues_from
        return normalized

    def _validate_high_confidence_relation_evidence(
        self,
        connection: Any,
        project: str,
        location_analysis: dict[str, Any],
        evidence: dict[str, Any],
    ) -> None:
        related_task_id = evidence["task_id"]
        related = connection.execute(
            "SELECT project, codex_thread_id FROM tasks WHERE id=?",
            (related_task_id,),
        ).fetchone()
        if not related:
            raise ValueError(f"Related task not found: {related_task_id}")
        if self._normalize_project(related["project"]) != project:
            raise ValueError("High-confidence relation evidence cannot cross projects")

        source = evidence["source"]
        location_evidence = location_analysis.get("location_evidence") or {}
        connected_tool = str(location_evidence.get("tool") or "").strip()
        obsidian_evidence = json.dumps(
            location_analysis.get("obsidian_evidence") or {}, ensure_ascii=False,
        )
        if source != connected_tool and not (
            source == "obsidian" and related_task_id in obsidian_evidence
        ):
            raise ValueError(
                "High-confidence relation evidence must come from the connected "
                "location evidence or a matching Obsidian candidate"
            )

        current_targets = location_analysis.get("targets") or []
        current_files = {
            self._normalize_target_file(item.get("file"))
            for item in current_targets if isinstance(item, dict) and item.get("file")
        }
        current_symbols = {
            str(symbol).strip()
            for item in current_targets if isinstance(item, dict)
            for symbol in (item.get("symbols") or []) if str(symbol).strip()
        }
        connected_files = {
            self._normalize_target_file(path)
            for path in (location_evidence.get("files") or []) if str(path).strip()
        }
        connected_symbols = {
            str(symbol).strip()
            for symbol in (location_evidence.get("symbols") or []) if str(symbol).strip()
        }
        evidence_files = {
            self._normalize_target_file(path)
            for path in evidence.get("files") or []
        }
        evidence_symbols = set(evidence.get("symbols") or [])
        if source != "obsidian" and not (
            evidence_files & (current_files | connected_files)
            or evidence_symbols & (current_symbols | connected_symbols)
        ):
            raise ValueError(
                "Relation evidence does not match the connected location result"
            )

        related_targets = connection.execute(
            "SELECT file, symbol FROM task_targets WHERE task_id=?",
            (related_task_id,),
        ).fetchall()
        related_files = {str(item["file"] or "") for item in related_targets}
        related_symbols = {
            str(item["symbol"] or "") for item in related_targets if item["symbol"]
        }
        relation_type = evidence["relation_type"]
        if relation_type == "conflicts_with":
            overlap = current_files & related_files
            if not overlap or not evidence_files & overlap:
                raise ValueError(
                    "High-confidence target overlap must match both tasks' locked files"
                )
        elif not (
            evidence_files & related_files or evidence_symbols & related_symbols
        ):
            raise ValueError(
                "High-confidence relation evidence does not match the related task targets"
            )
        if relation_type == "continues_from" and not str(
            related["codex_thread_id"] or ""
        ).strip():
            raise ValueError(
                "Thread continuation requires a related task with a native thread"
            )

    @staticmethod
    def _normalize_quality_gates(gates: Any) -> dict[str, dict[str, Any]]:
        if not isinstance(gates, dict):
            raise ValueError("quality_gates must be an object")
        unknown = set(gates) - {"code_review"}
        if unknown:
            raise ValueError(f"Unknown quality gate fields: {', '.join(sorted(unknown))}")
        normalized: dict[str, dict[str, Any]] = {}
        for gate in ("code_review",):
            value = gates.get(gate)
            if not isinstance(value, dict):
                raise ValueError(f"quality_gates.{gate} must be an object")
            unknown = set(value) - {"required", "reason"}
            if unknown:
                raise ValueError(
                    f"Unknown quality_gates.{gate} fields: {', '.join(sorted(unknown))}"
                )
            required = value.get("required")
            reason = str(value.get("reason") or "").strip()
            if not isinstance(required, bool):
                raise ValueError(f"quality_gates.{gate}.required must be boolean")
            if not reason:
                raise ValueError(f"quality_gates.{gate}.reason is required")
            normalized[gate] = {"required": required, "reason": reason}
        return normalized

    @staticmethod
    def _default_quality_gates_for_targets(
        targets: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        requires_code_review = any(
            str(target.get("mode") or "modify").strip().lower() != "inspect"
            for target in targets
            if isinstance(target, dict)
        )
        return {
            "code_review": {
                "required": requires_code_review,
                "reason": (
                    "Repository changes require code review"
                    if requires_code_review
                    else "Read-only tasks do not change repository files"
                ),
            },
        }

    @staticmethod
    def _quality_gate_required(task: dict[str, Any], gate: str) -> bool:
        gates = (task.get("review_contract") or {}).get("quality_gates")
        if not isinstance(gates, dict) or not isinstance(gates.get(gate), dict):
            raise ValueError(f"Task review contract is missing quality_gates.{gate}")
        return bool(gates[gate]["required"])

    def _store_managed_artifacts(
        self, namespace: str, references: list[str],
    ) -> list[str]:
        if not isinstance(references, list):
            raise ValueError("artifact references must be an array")
        safe_namespace = "/".join(
            part for part in str(namespace).split("/") if part and part not in {".", ".."}
        )
        destination = self.data_home / "artifacts" / safe_namespace
        destination.mkdir(parents=True, exist_ok=True)
        stored: list[str] = []
        for reference in references:
            value = str(reference or "").strip()
            if not value:
                raise ValueError("artifact reference must not be empty")
            if value.startswith("artifact://"):
                relative = Path(value.removeprefix("artifact://"))
                resolved = (self.data_home / "artifacts" / relative).resolve()
                artifact_root = (self.data_home / "artifacts").resolve()
                if artifact_root not in resolved.parents or not resolved.is_file():
                    raise ValueError(f"Managed artifact does not exist: {value}")
                stored.append(value)
                continue
            source = Path(value).expanduser().resolve()
            if not source.is_file():
                raise ValueError(f"Artifact file does not exist: {source}")
            digest = hashlib.sha256(source.read_bytes()).hexdigest()
            suffix = source.suffix.lower()
            target = destination / f"{digest[:16]}{suffix}"
            if not target.exists():
                shutil.copy2(source, target)
            relative = target.relative_to(self.data_home / "artifacts")
            stored.append(f"artifact://{relative.as_posix()}")
        return list(dict.fromkeys(stored))

    @staticmethod
    def _normalize_review_contract(contract: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(contract, dict):
            raise ValueError("review_contract must be an object")
        unknown = set(contract) - {"checks", "quality_gates"}
        if unknown:
            raise ValueError(f"Unknown review_contract fields: {', '.join(sorted(unknown))}")
        quality_gates = TaskPlanningMixin._normalize_quality_gates(
            contract.get("quality_gates")
        )
        raw_checks = contract.get("checks") if "checks" in contract else None
        if raw_checks is None or (
            raw_checks == [] and quality_gates["code_review"]["required"]
        ):
            raw_checks = (
                default_code_review_checks()
                if quality_gates["code_review"]["required"]
                else []
            )
        if not isinstance(raw_checks, list):
            raise ValueError("review_contract.checks must be an array")
        checks: list[dict[str, str]] = []
        seen: set[str] = set()
        for raw in raw_checks:
            if isinstance(raw, dict):
                unknown = set(raw) - {"id", "description", "kind"}
                if unknown:
                    raise ValueError(
                        f"Unknown code review check fields: {', '.join(sorted(unknown))}"
                    )
                check_id = str(
                    raw.get("id") or ""
                ).strip()
                description = str(raw.get("description") or "").strip()
                kind = str(raw.get("kind") or "").strip()
            else:
                raise ValueError("Every code review check must be an object")
            if not check_id or not description:
                raise ValueError("Every code review check requires id and description")
            if kind not in {"code", "static"}:
                raise ValueError("Code review check kind must be code or static")
            if check_id in seen:
                raise ValueError("Code review check ids must be unique")
            seen.add(check_id)
            checks.append({"id": check_id, "description": description, "kind": kind})
        return {
            "checks": checks,
            "quality_gates": quality_gates,
        }

    def _validate_implementation_contract(
        self,
        contract: dict[str, Any],
        analysis_targets: list[dict[str, Any]] | None = None,
        *,
        allow_empty: bool = False,
    ) -> dict[str, Any]:
        """Normalize one precise file-oriented execution plan."""
        if not isinstance(contract, dict):
            raise ValueError("implementation_contract must be an object")
        unknown = set(contract) - {"targets", "visual_references"}
        if unknown:
            raise ValueError(
                f"Unknown implementation_contract fields: {', '.join(sorted(unknown))}"
            )
        raw_targets = contract.get("targets", [])
        if not isinstance(raw_targets, list):
            raise ValueError("implementation_contract.targets must be an array")
        if not raw_targets and not allow_empty:
            raise ValueError("implementation_contract.targets is required")
        targets: list[dict[str, Any]] = []
        target_by_file: dict[str, dict[str, Any]] = {}
        for item in raw_targets:
            if not isinstance(item, dict):
                raise ValueError("Every implementation target must be an object")
            unknown = set(item) - {"file", "mode", "symbols", "reason", "tasks"}
            if unknown:
                raise ValueError(
                    f"Unknown implementation target fields: {', '.join(sorted(unknown))}"
                )
            file_value = self._normalize_target_file(item.get("file"))
            mode = str(item.get("mode") or "modify").strip().lower()
            if mode not in {"modify", "create", "delete", "config", "inspect"}:
                raise ValueError(
                    "Implementation target mode must be modify, create, delete, config or inspect"
                )
            symbols = item.get("symbols") or []
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
                raise ValueError("Every implementation target requires a symbols array")
            normalized_symbols = list(dict.fromkeys(symbol.strip() for symbol in symbols if symbol.strip()))
            symbol_optional = Path(file_value).suffix.lower() in {
                ".json", ".yaml", ".yml", ".toml", ".ini", ".sql", ".md",
                ".txt", ".css", ".scss", ".html",
            }
            if mode in {"modify", "delete"} and not normalized_symbols and not symbol_optional:
                raise ValueError("Modify and delete targets require at least one exact symbol")
            raw_tasks = item.get("tasks") or []
            if not isinstance(raw_tasks, list) or any(not isinstance(task, dict) for task in raw_tasks):
                raise ValueError("implementation_contract.targets.tasks must be an array of objects")
            normalized_tasks: list[dict[str, Any]] = []
            for raw_task in raw_tasks:
                unknown = set(raw_task) - {"symbol", "action", "expected"}
                if unknown:
                    raise ValueError(
                        f"Unknown implementation task fields: {', '.join(sorted(unknown))}"
                    )
                action = str(raw_task.get("action") or "").strip()
                symbol = str(raw_task.get("symbol") or "").strip()
                if not action:
                    raise ValueError("Every implementation target task requires an action")
                if symbol and symbol not in normalized_symbols:
                    raise ValueError("Implementation target task symbol is outside locked symbols")
                normalized = {"action": action}
                if symbol:
                    normalized["symbol"] = symbol
                expected = str(raw_task.get("expected") or "").strip()
                if expected:
                    normalized["expected"] = expected
                normalized_tasks.append(normalized)
            target = {
                "file": file_value,
                "mode": mode,
                "symbols": normalized_symbols,
                "tasks": normalized_tasks,
            }
            reason = str(item.get("reason") or "").strip()
            if reason:
                target["reason"] = reason
            if file_value in target_by_file:
                raise ValueError("Implementation targets must contain each file exactly once")
            target_by_file[file_value] = target
            targets.append(target)

        target_keys = {
            (item["file"], item["mode"], tuple(sorted(item["symbols"]))) for item in targets
        }
        if analysis_targets is not None:
            located_keys = {
                (
                    self._normalize_target_file(item.get("file")),
                    str(item.get("mode") or "modify").strip().lower(),
                    tuple(sorted(
                        str(symbol).strip() for symbol in (item.get("symbols") or [])
                        if str(symbol).strip()
                    )),
                )
                for item in analysis_targets if isinstance(item, dict)
            }
            if target_keys != located_keys:
                raise ValueError("implementation_contract.targets must exactly match location analysis targets")

        if not allow_empty and any(not target["tasks"] for target in targets):
            raise ValueError("Every implementation target requires at least one task")

        visual_references = contract.get("visual_references") or []
        if not isinstance(visual_references, list) or any(
            not isinstance(item, dict)
            or not str(item.get("artifact_id") or "").strip()
            or not str(item.get("path") or "").strip()
            for item in visual_references
        ):
            raise ValueError(
                "implementation_contract.visual_references must contain managed artifact_id and path"
            )
        return {"targets": targets, "visual_references": visual_references}

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
                    "connected CodeGraph CLI evidence.command must be the executed argv array beginning with codegraph, explore"
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

    def create_task(
        self, payload: dict[str, Any], existing_task_id: str | None = None,
    ) -> dict[str, Any]:
        unknown = set(payload) - {
            "title", "type", "project", "modules", "status", "priority", "goal",
            "scope", "out_of_scope", "acceptance_criteria", "source_thread_id",
            "token_budget", "location_analysis_id", "dependency_analysis",
            "implementation_contract", "review_contract", "relations", "auto_dispatch",
        }
        if unknown:
            raise ValueError(f"Unknown task fields: {', '.join(sorted(unknown))}")
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
            dependency_analysis = payload.get("dependency_analysis") or {
                "decision": "independent"
            }
            analysis_targets = location_analysis.get("targets") or []
            review_contract = dict(payload.get("review_contract") or {})
            review_contract.setdefault(
                "quality_gates",
                self._default_quality_gates_for_targets(analysis_targets),
            )
            review_contract = self._normalize_review_contract(review_contract)
            requires_changes = review_contract["quality_gates"]["code_review"][
                "required"
            ]
            implementation_contract = payload.get(
                "implementation_contract"
            ) or {"targets": analysis_targets}
            dependency_analysis = self._normalize_dependency_analysis(
                dependency_analysis
            )
            evidence_by_relation = {
                (item["task_id"], item["relation_type"]): item
                for item in dependency_analysis.get("relation_evidence") or []
                if item.get("confidence") == "high"
            }
            for evidence_item in evidence_by_relation.values():
                self._validate_high_confidence_relation_evidence(
                    connection, project, location_analysis, evidence_item,
                )
            declared_relations = payload.get("relations", []) or []
            if not isinstance(declared_relations, list):
                raise ValueError("relations must be an array")
            declared_relations = [dict(item) for item in declared_relations if isinstance(item, dict)]
            required_relations = [
                (task_id, "depends_on")
                for task_id in dependency_analysis["depends_tasks"]
            ]
            if dependency_analysis.get("continues_from_task_id"):
                required_relations.append((
                    dependency_analysis["continues_from_task_id"], "continues_from",
                ))
            required_relations.extend(
                (task_id, "conflicts_with")
                for task_id in dependency_analysis["conflicts_tasks"]
            )
            for target_task_id, relation_type in required_relations:
                if not any(
                    str(item.get("target_task_id") or "").strip()
                    == target_task_id
                    and str(item.get("relation_type") or "").strip()
                    == relation_type
                    for item in declared_relations
                ):
                    declared_relations.append({
                        "target_task_id": target_task_id,
                        "relation_type": relation_type,
                        **(
                            {"description": evidence_by_relation[(target_task_id, relation_type)]["reason"]}
                            if (target_task_id, relation_type) in evidence_by_relation
                            else {}
                        ),
                    })
            declared_scheduling = {
                (
                    str(item.get("target_task_id") or "").strip(),
                    str(item.get("relation_type") or "").strip(),
                )
                for item in declared_relations
                if str(item.get("relation_type") or "").strip()
                in {"depends_on", "continues_from"}
            }
            expected_scheduling = set(required_relations) - {
                (task_id, "conflicts_with")
                for task_id in dependency_analysis["conflicts_tasks"]
            }
            if declared_scheduling != expected_scheduling:
                raise ValueError("dependency analysis and scheduling relations must match exactly")
            implementation_contract = self._validate_implementation_contract(
                implementation_contract,
                analysis_targets,
                allow_empty=not requires_changes,
            )
            targets = implementation_contract.get("targets")
            if requires_changes and not targets:
                raise ValueError(
                    "Code-changing tasks require non-empty implementation targets"
                )
            task_values = (
                title, task_type, project,
                json.dumps(list_values["modules"], ensure_ascii=False),
                payload.get("status", "draft"), priority, payload.get("goal", ""),
                json.dumps(list_values["scope"], ensure_ascii=False),
                json.dumps(list_values["out_of_scope"], ensure_ascii=False),
                json.dumps(list_values["acceptance_criteria"], ensure_ascii=False),
                payload.get("source_thread_id"), token_budget,
                json.dumps(location_analysis, ensure_ascii=False),
                json.dumps(location_analysis.get("acceptance_plan", []), ensure_ascii=False),
                json.dumps(dependency_analysis, ensure_ascii=False),
                json.dumps(implementation_contract, ensure_ascii=False),
                json.dumps(review_contract, ensure_ascii=False),
                int(bool(payload.get("auto_dispatch", True))),
            )
            if existing_task_id:
                existing = connection.execute(
                    "SELECT status FROM tasks WHERE id=?", (existing_task_id,),
                ).fetchone()
                if not existing or existing["status"] != "draft":
                    raise ValueError("A queued draft task is required for intake completion")
                task_id = existing_task_id
                connection.execute(
                    """UPDATE tasks SET
                         title=?, type=?, project=?, modules=?, status=?, priority=?,
                         goal=?, scope=?, out_of_scope=?, acceptance_criteria=?,
                         source_thread_id=COALESCE(?, source_thread_id), token_budget=?,
                         location_context=?, acceptance_plan=?, dependency_analysis=?,
                         implementation_contract=?, review_contract=?, auto_dispatch=?,
                         last_failure_reason='', updated_at=CURRENT_TIMESTAMP
                       WHERE id=?""",
                    (*task_values, task_id),
                )
            else:
                task_id = self.db.next_id(connection, task_id_prefix)
                connection.execute(
                    """INSERT INTO tasks(
                        id, requirement_id, title, type, project, modules, status,
                        priority, goal, scope, out_of_scope, acceptance_criteria,
                        source_thread_id, token_budget, location_context, acceptance_plan,
                        dependency_analysis, implementation_contract, review_contract,
                        auto_dispatch
                    ) VALUES(?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (task_id, *task_values),
                )
            if payload.get("source_thread_id"):
                connection.execute(
                    "INSERT OR IGNORE INTO task_conversations(task_id, role, thread_id, title) VALUES(?, 'source', ?, ?)",
                    (task_id, payload["source_thread_id"], f"{task_id} 需求确认"),
                )
            self._store_task_targets(connection, task_id, location_analysis.get("targets", []))
            for relation in declared_relations:
                if not isinstance(relation, dict):
                    raise ValueError("relations must contain objects")
                unknown = set(relation) - {"target_task_id", "relation_type", "description"}
                if unknown:
                    raise ValueError(
                        f"Unknown task relation fields: {', '.join(sorted(unknown))}"
                    )
                source = task_id
                target = str(relation.get("target_task_id") or "")
                relation_type = str(relation.get("relation_type") or "")
                if not target or relation_type not in RELATION_TYPES:
                    raise ValueError("Each relation requires target_task_id and valid relation_type")
                if source == target:
                    raise ValueError("A task cannot relate to itself")
                exists = connection.execute("SELECT 1 FROM tasks WHERE id=?", (target,)).fetchone()
                if not exists:
                    raise ValueError(f"Related task not found: {target}")
                self._insert_relation_in_connection(connection, source, target, relation_type, str(relation.get("description") or ""))
                self._queue_obsidian_sync(
                    connection, "task", target,
                )
            batch_id = self._try_join_open_batch(connection, task_id)
            batch_members = {
                row["task_id"] for row in connection.execute(
                    "SELECT task_id FROM execution_batch_tasks WHERE batch_id=?",
                    (batch_id,),
                ).fetchall()
            } if batch_id else set()
            conflicts = self._target_conflicts(connection, task_id)
            scheduling_targets = set(dependency_analysis["depends_tasks"])
            if dependency_analysis.get("continues_from_task_id"):
                scheduling_targets.add(dependency_analysis["continues_from_task_id"])
            for conflict in conflicts:
                conflict_task_id = str(conflict.get("task_id") or "").strip()
                if (
                    not conflict_task_id
                    or conflict_task_id in scheduling_targets
                    or conflict_task_id in batch_members
                ):
                    continue
                self._insert_relation_in_connection(
                    connection, task_id, conflict_task_id, "conflicts_with",
                    "DoTasks detected overlapping locked file/symbol targets",
                )
                self._queue_obsidian_sync(connection, "task", conflict_task_id)
                if conflict_task_id not in dependency_analysis["conflicts_tasks"]:
                    dependency_analysis["conflicts_tasks"].append(conflict_task_id)
            connection.execute(
                "UPDATE tasks SET dependency_analysis=? WHERE id=?",
                (json.dumps(dependency_analysis, ensure_ascii=False), task_id),
            )
            event_payload = {
                **payload,
                "dependency_analysis": dependency_analysis,
                "implementation_contract": implementation_contract,
                "review_contract": review_contract,
                "relations": declared_relations,
            }
            self._event(connection, "task", task_id, "created", event_payload)
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
            "AND other.status IN ('claimed','investigating','implementing','waiting_confirmation','code_review','failed','blocked')"
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
        if stage not in {"creation", "change"}:
            raise ValueError("stage must be creation or change")
        project = self._require_project_directory(payload.get("project"))
        if task_id:
            task = self.get_task(task_id)
            if task.get("project") != project:
                raise ValueError("project does not match task")
        modules = payload.get("modules") or []
        if not isinstance(modules, list) or any(not isinstance(item, str) for item in modules):
            raise ValueError("modules must be an array of strings")
        delivery_attempt = self.get_run(delivery_run_id)["attempt"] if delivery_run_id else None
        query = " ".join(str(value) for value in [payload.get("title", ""), payload.get("goal", ""), *modules] if value).strip()
        if not query:
            raise ValueError("title, goal or modules are required for location analysis")
        obsidian = {
            "status": self.obsidian.status(),
            # Dependency retrieval is deferred until finalization, when exact
            # files, symbols and actions are available. This keeps intake to one
            # project-scoped graph query instead of an early broad search.
            "dependency_candidates": [],
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
            "After one bounded location route, call finalize_task_intake once with location evidence, "
            "an explicit code-review decision, and acceptance items. Code-changing tasks also require "
            "exact targets and target tasks; read-only task fields that do not apply may be empty."
            if stage == "creation" else
            "Use the first usable bounded location route in order: CodeGraph, GitNexus, then direct "
            "source matching. Report that evidence, then complete the location analysis."
        )
        return {"analysis_id": analysis_id, "stage": stage, "project": project,
                "obsidian": {"status": obsidian["status"], "candidate_count": 0},
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
        history = []
        requested_modules = specific_modules(modules)
        requested_terms = (
            search_tokens(" ".join((title, goal, *symbols))) - GENERIC_MATCH_TERMS
        )
        with self.db.connection() as connection:
            for item in candidates:
                if not item.get("task_id"): continue
                row = connection.execute("SELECT * FROM tasks WHERE id=?", (item["task_id"],)).fetchone()
                if not row:
                    if self._normalize_project(item.get("project")) == project:
                        history.append(item)
                    continue
                task = decode_row(row)
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
                    item["history_reason"] = "terminal_task"
                    history.append(item)
                    continue
                module_overlap = requested_modules & specific_modules(task.get("modules", []))
                task_terms = (
                    search_tokens(
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
        history_ids = {
            str(item.get("task_id") or "").strip()
            for item in history if str(item.get("task_id") or "").strip()
        }
        history_edges: list[dict[str, str]] = []
        for item in history:
            for edge in item.get("history_edges") or []:
                if not isinstance(edge, dict):
                    continue
                source = str(edge.get("from") or "").strip()
                target = str(edge.get("to") or "").strip()
                relation_type = str(edge.get("type") or "").strip()
                normalized = {"from": source, "to": target, "type": relation_type}
                if (
                    source in history_ids and target in history_ids
                    and relation_type in RELATION_TYPES and normalized not in history_edges
                ):
                    history_edges.append(normalized)
        adjacency: dict[str, set[str]] = {task_id: set() for task_id in history_ids}
        indegree = {task_id: 0 for task_id in history_ids}
        for edge in history_edges:
            if edge["to"] not in adjacency[edge["from"]]:
                adjacency[edge["from"]].add(edge["to"])
                indegree[edge["to"]] += 1
        pending = sorted(task_id for task_id, degree in indegree.items() if degree == 0)
        history_path: list[str] = []
        while pending:
            current = pending.pop(0)
            history_path.append(current)
            for target in sorted(adjacency[current]):
                indegree[target] -= 1
                if indegree[target] == 0:
                    pending.append(target)
                    pending.sort()
        history_path.extend(sorted(history_ids - set(history_path)))
        return {
            "decision": "independent" if not valid else "requires_confirmation",
            "candidates": valid,
            "ignored_candidates": ignored,
            "history_candidates": history,
            "history_tasks": history_path,
            "history_edges": history_edges,
            "obsidian": {"status": self.obsidian.status(), "evidence": candidates},
            "instruction": (
                "无强匹配活动任务时直接使用 independent；"
                "否则确认 independent、depends_on 或 continues_from 后原子创建关系。"
            ),
        }

    @staticmethod
    def _controller_kickoff_contract(auto_dispatch: bool) -> dict[str, Any]:
        return {
            "auto_dispatch": bool(auto_dispatch),
            "controller_kickoff_required": bool(auto_dispatch),
            "controller_kickoff": (
                {
                    "mode": "kickoff",
                    "tool": "claim_schedule_cycle",
                    "arguments": {
                        "worker_id": "codex-native-controller",
                        "force": True,
                        "lease_seconds": 7200,
                    },
                }
                if auto_dispatch else None
            ),
        }

    def enqueue_task_intake(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Place a page-created task in the queue while its location is prepared."""
        title = str(payload.get("title") or "").strip()
        goal = str(payload.get("goal") or "").strip()
        if not title:
            raise ValueError("title is required")
        if not goal:
            raise ValueError("goal is required")
        priority = str(payload.get("priority") or "P2").strip()
        if priority not in {"P0", "P1", "P2", "P3"}:
            raise ValueError("priority must be P0, P1, P2 or P3")
        task_type = str(payload.get("type") or "feature").strip().lower()
        if task_type not in {"feature", "bug"}:
            raise ValueError("type must be feature or bug")
        raw_project = str(payload.get("project") or "").strip()
        project = self._require_project_directory(raw_project) if raw_project else None
        auto_dispatch = bool(payload.get("auto_dispatch", True))
        modules = self._string_list(payload, "modules")
        scope = self._string_list(payload, "scope") or [goal]
        out_of_scope = self._string_list(payload, "out_of_scope")
        visual_references = self._manage_visual_references(
            "web-task", payload.get("visual_references")
        )
        if project is None:
            acceptance_criteria = [goal]
            acceptance_plan = [
                {
                    "criterion": goal,
                    "method": "Codex response",
                    "expected": goal,
                    "required": True,
                    "check_type": "static_review",
                }
            ]
            dependency_analysis = self._normalize_dependency_analysis(
                {"decision": "independent"}
            )
            review_contract = {
                "checks": [],
                "quality_gates": {
                    "code_review": {
                        "required": False,
                        "reason": "Projectless tasks do not modify a repository",
                    },
                },
            }
            with self.db.transaction() as connection:
                task_id = self.db.next_id(
                    connection, "BUG" if task_type == "bug" else "TASK"
                )
                connection.execute(
                    """INSERT INTO tasks(
                           id, title, type, project, modules, status, priority,
                           goal, scope, out_of_scope, acceptance_criteria,
                           location_context, acceptance_plan, dependency_analysis,
                           implementation_contract, review_contract, auto_dispatch
                       ) VALUES(?, ?, ?, NULL, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        task_id, title, task_type,
                        json.dumps(modules, ensure_ascii=False), priority, goal,
                        json.dumps(scope, ensure_ascii=False),
                        json.dumps(out_of_scope, ensure_ascii=False),
                        json.dumps(acceptance_criteria, ensure_ascii=False),
                        json.dumps(
                            {"mode": "projectless", "targets": []},
                            ensure_ascii=False,
                        ),
                        json.dumps(acceptance_plan, ensure_ascii=False),
                        json.dumps(dependency_analysis, ensure_ascii=False),
                        json.dumps({
                            "targets": [],
                            "visual_references": visual_references,
                        }, ensure_ascii=False),
                        json.dumps(review_contract, ensure_ascii=False),
                        int(auto_dispatch),
                    ),
                )
                self._event(
                    connection, "task", task_id, "intake_queued",
                    {"projectless": True, "auto_dispatch": auto_dispatch},
                )
            return {
                "status": "queued",
                "intake_kind": "task",
                "task_id": task_id,
                "task_status": "ready",
                "requirement_id": None,
                "projectless": True,
                **self._controller_kickoff_contract(auto_dispatch),
            }

        auto_dispatch = auto_dispatch and bool(project)
        with self.db.transaction() as connection:
            requirement_id = self.db.next_id(connection, "REQ")
            task_id = self.db.next_id(
                connection, "BUG" if task_type == "bug" else "TASK"
            )
            connection.execute(
                """INSERT INTO requirements(
                       id, title, original_content, description, source_type,
                       project, status, priority, goal, modules, scope,
                       out_of_scope, acceptance_criteria, auto_dispatch,
                       decomposition_plan, visual_references
                   ) VALUES(?, ?, ?, ?, 'web_task', ?, 'ready', ?, ?, ?, ?, ?,
                            '[]', ?, ?, ?)""",
                (
                    requirement_id, title, goal, goal, project, priority, goal,
                    json.dumps(modules, ensure_ascii=False),
                    json.dumps(scope, ensure_ascii=False),
                    json.dumps(out_of_scope, ensure_ascii=False),
                    int(auto_dispatch),
                    json.dumps([{"key": "direct", "title": title, "goal": goal}], ensure_ascii=False),
                    json.dumps(visual_references, ensure_ascii=False),
                ),
            )
            connection.execute(
                """INSERT INTO tasks(
                       id, requirement_id, requirement_task_key, title, type,
                       project, modules, status, priority, goal, scope,
                       out_of_scope, acceptance_criteria, auto_dispatch
                   ) VALUES(?, ?, 'direct', ?, ?, ?, ?, 'draft', ?, ?, ?, ?,
                            '[]', 0)""",
                (
                    task_id, requirement_id, title, task_type, project,
                    json.dumps(modules, ensure_ascii=False), priority, goal,
                    json.dumps(scope, ensure_ascii=False),
                    json.dumps(out_of_scope, ensure_ascii=False),
                ),
            )
            self._event(
                connection, "task", task_id, "intake_queued",
                {"requirement_id": requirement_id, "auto_dispatch": auto_dispatch},
            )
            self._event(
                connection, "requirement", requirement_id, "created",
                {"intake_kind": "task", "task_id": task_id},
            )
        return {
            "status": "queued", "intake_kind": "task", "task_id": task_id,
            "task_status": "draft", "requirement_id": requirement_id,
            **self._controller_kickoff_contract(auto_dispatch),
        }

    def finalize_task_intake(
        self, payload: dict[str, Any], existing_task_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist a requirement or create a ready independently executable task."""
        intake_kind = str(payload.get("intake_kind") or "").strip().lower()
        if intake_kind not in {"requirement", "task"}:
            raise ValueError("intake_kind must be requirement or task")
        if intake_kind == "requirement":
            title = str(payload.get("title") or "").strip()
            goal = str(payload.get("goal") or "").strip()
            if not title:
                raise ValueError("title is required")
            if not goal:
                raise ValueError("goal is required")
            raw_project = str(payload.get("project") or "").strip()
            project = self._require_project_directory(raw_project) if raw_project else None
            auto_dispatch = bool(payload.get("auto_dispatch", True)) and bool(project)
            lists = {
                field: self._string_list(payload, field)
                for field in (
                    "modules", "scope", "out_of_scope", "acceptance_criteria"
                )
            }
            priority = str(payload.get("priority") or "P2").strip()
            if priority not in {"P0", "P1", "P2", "P3"}:
                raise ValueError("priority must be P0, P1, P2 or P3")
            plan = payload.get("decomposition_tasks") or []
            if not isinstance(plan, list) or any(
                not isinstance(item, dict) for item in plan
            ):
                raise ValueError("decomposition_tasks must be an array of objects")
            original_content = str(
                payload.get("original_content") or payload.get("description") or goal
            ).strip()
            visual_references = self._manage_visual_references(
                "requirement", payload.get("visual_references")
            )
            with self.db.transaction() as connection:
                requirement_id = self.db.next_id(connection, "REQ")
                connection.execute(
                    """INSERT INTO requirements(
                           id, title, original_content, description, source_type,
                           source_reference, project, status, priority, goal,
                           modules, scope, out_of_scope, acceptance_criteria,
                           source_thread_id, auto_dispatch, decomposition_plan,
                           visual_references
                       ) VALUES(?, ?, ?, ?, ?, ?, ?, 'ready', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        requirement_id, title, original_content,
                        str(payload.get("description") or goal),
                        str(payload.get("source_type") or "conversation"),
                        payload.get("source_reference"), project, priority, goal,
                        json.dumps(lists["modules"], ensure_ascii=False),
                        json.dumps(lists["scope"], ensure_ascii=False),
                        json.dumps(lists["out_of_scope"], ensure_ascii=False),
                        json.dumps(lists["acceptance_criteria"], ensure_ascii=False),
                        payload.get("source_thread_id"),
                        int(auto_dispatch),
                        json.dumps(plan, ensure_ascii=False),
                        json.dumps(visual_references, ensure_ascii=False),
                    ),
                )
                self._event(
                    connection, "requirement", requirement_id, "created",
                    {"intake_kind": "requirement", "title": title},
                )
            return {
                "status": "created", "intake_kind": "requirement",
                "requirement_id": requirement_id,
                "requirement_status": "ready",
                **self._controller_kickoff_contract(
                    auto_dispatch
                ),
            }
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
        targets = payload.get("targets", [])
        acceptance_plan = payload.get("acceptance_plan")
        if not isinstance(evidence, dict) or not evidence:
            raise ValueError("location_evidence is required")
        if not isinstance(targets, list):
            raise ValueError("targets must be an array")
        if any(not isinstance(target, dict) for target in targets):
            raise ValueError("Every target must be an object")
        if not isinstance(acceptance_plan, list) or not acceptance_plan:
            raise ValueError("acceptance_plan must be a non-empty array")
        quality_gates = payload.get("quality_gates")
        if quality_gates is None:
            raise ValueError("quality_gates is required to classify the task")
        normalized_gates = self._normalize_quality_gates(quality_gates)
        requires_changes = normalized_gates["code_review"]["required"]
        if requires_changes and not targets:
            raise ValueError("Code-changing tasks require non-empty targets")
        if requires_changes and any(not target.get("tasks") for target in targets):
            raise ValueError("Every code-changing target requires non-empty tasks")
        review_checks = (
            payload["review_checks"]
            if "review_checks" in payload
            else (default_code_review_checks() if requires_changes else [])
        )
        if not isinstance(review_checks, list):
            raise ValueError("review_checks must be an array")

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
        located_files = [
            str(target.get("file") or "").strip()
            for target in targets if isinstance(target, dict)
            and str(target.get("file") or "").strip()
        ]
        actions: list[str] = []
        for target in targets:
            if not isinstance(target, dict):
                continue
            actions.extend(
                str(item.get("action") or "").strip()
                for item in target.get("tasks") or [] if isinstance(item, dict)
                and str(item.get("action") or "").strip()
            )
        candidates = self.obsidian.search_task_dependencies(
            str(payload.get("title") or ""), str(payload.get("goal") or ""),
            payload.get("modules") or [], located_symbols,
            project=project, located_files=located_files,
            actions=list(dict.fromkeys(actions)),
        )
        dependency_result = self._classify_task_dependencies({
            "title": payload.get("title"), "goal": payload.get("goal"),
            "project": project, "modules": payload.get("modules") or [],
            "located_symbols": located_symbols,
        }, candidates)
        if dependency_analysis is None:
            if dependency_result.get("decision") == "requires_confirmation":
                return {
                    "status": "requires_confirmation",
                    "analysis_id": analysis_id,
                    "dependency_analysis": dependency_result,
                }
            dependency_analysis = {
                "decision": "independent",
                "history_tasks": dependency_result.get("history_tasks") or [],
                "history_edges": dependency_result.get("history_edges") or [],
            }
        elif isinstance(dependency_analysis, dict):
            dependency_analysis = dict(dependency_analysis)
            dependency_analysis.setdefault(
                "history_tasks", dependency_result.get("history_tasks") or [],
            )
            dependency_analysis.setdefault(
                "history_edges", dependency_result.get("history_edges") or [],
            )
        dependency_analysis = self._normalize_dependency_analysis(dependency_analysis)
        raw_relations = payload.get("relations") or []
        if not isinstance(raw_relations, list) or any(not isinstance(item, dict) for item in raw_relations):
            raise ValueError("relations must be an array of objects")
        relations = list(raw_relations)

        implementation_contract = {
            "targets": targets,
            "visual_references": self._manage_visual_references(
                analysis_id, payload.get("visual_references")
            ),
        }
        review_contract = self._normalize_review_contract({
            "checks": review_checks,
            "quality_gates": normalized_gates,
        })
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
        })
        task_payload["status"] = "ready"
        task = self.create_task(task_payload, existing_task_id=existing_task_id)
        return {
            "status": "created", "intake_kind": "task", "analysis_id": analysis_id,
            "task_id": task["id"], "task_status": task["status"],
            **self._controller_kickoff_contract(bool(task.get("auto_dispatch"))),
        }

    def complete_location_analysis(
        self, analysis_id: str, location_evidence: dict[str, Any], targets: list[dict[str, Any]],
        acceptance_plan: list[dict[str, Any]], dependency_analysis: dict[str, Any] | None,
        implementation_contract: dict[str, Any] | None,
        review_contract: dict[str, Any] | None,
    ) -> dict[str, Any]:
        analysis = self.get_location_analysis(analysis_id)
        location_status = self.location_status(analysis["project"])
        if not location_status.get("available") or location_status.get("state") != "connected":
            raise ValueError("A connected location evidence report from CodeGraph, GitNexus, or source matching is required")
        if not location_evidence:
            raise ValueError("location_evidence is required")
        if not isinstance(targets, list) or any(
            not isinstance(item, dict) for item in targets
        ):
            raise ValueError("targets must be an array of objects")
        if isinstance(implementation_contract, dict):
            raw_implementation_targets = implementation_contract.get("targets", [])
            if isinstance(raw_implementation_targets, list) and any(
                not isinstance(item, dict) for item in raw_implementation_targets
            ):
                raise ValueError("Every implementation target must be an object")
        if not isinstance(review_contract, dict) or review_contract.get(
            "quality_gates"
        ) is None:
            raise ValueError(
                "review_contract.quality_gates is required to classify the task"
            )
        review_contract = dict(review_contract)
        review_contract = self._normalize_review_contract(review_contract)
        requires_changes = review_contract["quality_gates"]["code_review"][
            "required"
        ]
        if requires_changes and not targets:
            raise ValueError("Code-changing tasks require at least one located target")
        target_map: dict[str, set[str]] = {}
        for target in targets:
            file = self._normalize_target_file(target.get("file"))
            mode = str(target.get("mode") or "modify").strip().lower()
            if mode not in {"modify", "create", "delete", "config", "inspect"}:
                raise ValueError(
                    "Target mode must be modify, create, delete, config or inspect"
                )
            symbols = target.get("symbols", [])
            if not isinstance(symbols, list) or any(not isinstance(symbol, str) for symbol in symbols):
                raise ValueError("Target symbols must be an array of strings")
            target["file"] = file
            target["mode"] = mode
            target["symbols"] = [symbol.strip() for symbol in symbols if symbol.strip()]
            if mode in {"modify", "delete"} and not target["symbols"] and Path(file).suffix.lower() not in {".json", ".yaml", ".yml", ".toml", ".ini", ".sql", ".md", ".txt", ".css", ".scss", ".html"}:
                raise ValueError(f"Code target requires a component or method symbol: {file}")
            target.setdefault("reason", "")
            target_map.setdefault(file, set()).update(
                str(symbol).strip() for symbol in target["symbols"] if str(symbol).strip()
            )
        if not isinstance(acceptance_plan, list) or not acceptance_plan or any(not isinstance(item, dict) for item in acceptance_plan):
            raise ValueError("acceptance_plan is required")
        for item in acceptance_plan:
            raw_file = str(item.get("file") or "").strip()
            file = self._normalize_target_file(raw_file) if raw_file else ""
            symbol = str(item.get("symbol") or "").strip()
            if (
                not item.get("criterion")
                or not item.get("method")
                or not item.get("expected")
            ):
                raise ValueError(
                    "Every acceptance plan item requires criterion, method and expected"
                )
            if requires_changes and not file:
                raise ValueError(
                    "Code-changing acceptance plan items require a target file"
                )
            check_type = str(item.get("check_type") or ("automated" if item.get("command") else "static_review"))
            if check_type not in {"automated", "static_review", "manual_runtime"}:
                raise ValueError("Acceptance check_type must be automated, static_review or manual_runtime")
            if check_type == "automated" and not str(item.get("command") or "").strip():
                raise ValueError("Automated acceptance checks require command")
            item["check_type"] = check_type
            item["required"] = bool(item.get("required", True))
            item["timeout_seconds"] = max(1, min(int(item.get("timeout_seconds", 300)), 1800))
            failure_category = str(item.get("failure_category") or "").strip().lower()
            if failure_category and failure_category not in {
                "project", "environment", "implementation",
            }:
                raise ValueError(
                    "Acceptance failure_category must be project, environment or implementation"
                )
            if failure_category:
                item["failure_category"] = failure_category
            if str(item.get("repair_command") or "").strip():
                if failure_category and failure_category != "environment":
                    raise ValueError(
                        "Acceptance repair_command is only allowed for environment failures"
                    )
                item["repair_command"] = str(item["repair_command"]).strip()
                item["repair_timeout_seconds"] = max(
                    1,
                    min(
                        int(item.get("repair_timeout_seconds", item["timeout_seconds"])),
                        1800,
                    ),
                )
            if file and file not in target_map:
                raise ValueError(f"Acceptance plan points outside located targets: {file}")
            if file and target_map[file] and (
                not symbol or symbol not in target_map[file]
            ):
                raise ValueError(f"Acceptance plan symbol is outside located targets: {file}#{symbol or '<missing>'}")
            if file:
                item["file"] = file
            else:
                item.pop("file", None)
        implementation_contract = self._validate_implementation_contract(
            implementation_contract or {"targets": targets},
            targets,
            allow_empty=not requires_changes,
        )
        normalized_dependency = self._normalize_dependency_analysis(
            dependency_analysis or {"decision": "independent"}
        )
        with self.db.transaction() as connection:
            cursor = connection.execute(
                """UPDATE location_analyses SET location_evidence=?, targets=?,
                   dependency_analysis=?, implementation_contract=?, review_contract=?,
                   status='completed', completed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='prepared'""",
                (json.dumps(location_evidence, ensure_ascii=False), json.dumps(targets, ensure_ascii=False),
                 json.dumps(normalized_dependency, ensure_ascii=False),
                 json.dumps(implementation_contract, ensure_ascii=False),
                 json.dumps(review_contract, ensure_ascii=False), analysis_id),
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
        projectless = (
            not str(payload.get("project") or "").strip()
            and (payload.get("location_context") or {}).get("mode") == "projectless"
        )
        if not projectless and not str(payload.get("project") or "").strip():
            missing.append("project")
        if missing:
            raise ValueError(f"Task is not ready; missing: {', '.join(missing)}")
