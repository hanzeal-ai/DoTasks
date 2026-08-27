from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from core.run_context import TOOL_PROFILES
from core.service import TaskboardService
from .version import VERSION


SERVICE = TaskboardService()


TOOLS = [
    {
        "name": "list_board",
        "description": "List confirmed delivery tasks, counts, projects and integration status for the taskboard.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "prepare_task_location",
        "description": "Query bounded Obsidian history and prepare an ordered CodeGraph, GitNexus, or direct-source location plan before a task is created.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "project": {
                    "type": "string",
                    "description": "Absolute path to an existing project directory; never pass a basename or relative path.",
                },
                "goal": {"type": "string"},
                "modules": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "project", "goal"],
        },
    },
    {
        "name": "report_location_status",
        "description": "Report the bounded CodeGraph, GitNexus, or direct source-match route selected by the Codex agent. The taskboard service never queries project code itself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "project": {
                    "type": "string",
                    "description": "Absolute project path matching the location analysis.",
                },
                "available": {"type": "boolean"},
                "state": {"type": "string", "enum": ["connected", "stale", "error"]},
                "summary": {"type": "string"},
                "evidence": {"type": "object", "description": "For connected state include a bounded query and non-empty files. Supported tools: codegraph_explore; codegraph_cli_explore with argv and exit_code=0; gitnexus_query or gitnexus_context with project_path; gitnexus_cli_query with project_path, argv, and exit_code=0; or source_match with project_path and read-only search commands."},
                "agent_id": {"type": "string"},
            },
            "required": ["project", "available", "state", "evidence"],
        },
    },
    {
        "name": "complete_location_analysis",
        "description": "Persist bounded location evidence, exact file/symbol targets, implementation ordered_steps, review checks and criterion-level acceptance plan.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string"}, "location_evidence": {"type": "object", "description": "Evidence from the selected CodeGraph, GitNexus, or source_match route."},
                "targets": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "file": {"type": "string", "minLength": 1}, "symbols": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}, "reason": {"type": "string"}
                }, "required": ["file", "symbols"]}},
                "dependency_analysis": {"type": "object", "description": "v2 fixed structure: decision is independent, depends_on, or continues_from; dependent decisions also require target_task_id.", "properties": {"decision": {"type": "string", "enum": ["independent", "depends_on", "continues_from"]}, "target_task_id": {"type": "string"}}, "required": ["decision"]},
                "implementation_contract": {"type": "object", "description": "Targets must match the completed location evidence exactly and ordered_steps must contain code-change actions only; verification belongs in acceptance_plan.", "properties": {
                    "targets": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {"file": {"type": "string", "minLength": 1}, "symbols": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}}, "required": ["file", "symbols"]}},
                    "ordered_steps": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {"file": {"type": "string", "minLength": 1}, "symbol": {"type": "string", "minLength": 1}, "action": {"type": "string", "minLength": 1}}, "required": ["file", "symbol", "action"]}}
                }, "required": ["targets", "ordered_steps"]},
                "review_contract": {"type": "object", "description": "v2 requires code-review checks. Acceptance reuses the review session unless separate_acceptance_session is true.", "properties": {"checks": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}, "separate_acceptance_session": {"type": "boolean", "default": False}}, "required": ["checks"]},
                "acceptance_plan": {"type": "array", "items": {"type": "object", "properties": {
                    "criterion": {"type": "string"}, "file": {"type": "string"}, "symbol": {"type": "string"},
                    "method": {"type": "string"}, "command": {"type": "string"}, "expected": {"type": "string"},
                    "check_type": {"type": "string", "enum": ["automated", "static_review", "manual_runtime"]},
                    "required": {"type": "boolean"}, "timeout_seconds": {"type": "integer"}
                }, "required": ["criterion", "file", "method", "expected"]}},
            },
            "required": ["analysis_id", "location_evidence", "targets", "dependency_analysis", "implementation_contract", "review_contract", "acceptance_plan"],
        },
    },
    {
        "name": "finalize_task_intake",
        "description": "Create a ready v2 task from one non-duplicated intake bundle. The service reuses prepared history, reports location evidence, classifies dependencies, derives acceptance criteria and contracts, then creates the task. Returns requires_confirmation instead of creating when a strong active dependency candidate exists.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string"},
                "title": {"type": "string"},
                "project": {
                    "type": "string",
                    "description": "Absolute path to the existing project directory used by the prepared location analysis.",
                },
                "modules": {"type": "array", "items": {"type": "string"}},
                "goal": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                "source_thread_id": {"type": "string"},
                "type": {"type": "string", "enum": ["feature", "optimization", "refactor", "bug"]},
                "token_budget": {"type": "integer"},
                "auto_dispatch": {"type": "boolean"},
                "location_summary": {"type": "string"},
                "agent_id": {"type": "string"},
                "location_evidence": {"type": "object", "description": "One bounded CodeGraph, GitNexus, or source_match evidence object."},
                "targets": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "file": {"type": "string", "minLength": 1}, "symbols": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}}, "reason": {"type": "string"}
                }, "required": ["file", "symbols"]}},
                "ordered_steps": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "file": {"type": "string", "minLength": 1}, "symbol": {"type": "string", "minLength": 1}, "action": {"type": "string", "minLength": 1}
                }, "required": ["file", "symbol", "action"]}},
                "review_checks": {"type": "array", "minItems": 1, "items": {"type": "string", "minLength": 1}},
                "separate_acceptance_session": {"type": "boolean", "default": False},
                "acceptance_plan": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "criterion": {"type": "string"}, "file": {"type": "string"}, "symbol": {"type": "string"},
                    "method": {"type": "string"}, "command": {"type": "string"}, "expected": {"type": "string"},
                    "check_type": {"type": "string", "enum": ["automated", "static_review", "manual_runtime"]},
                    "required": {"type": "boolean", "default": True}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600}
                }, "required": ["criterion", "file", "symbol", "method", "expected", "check_type"]}},
                "dependency_analysis": {"type": "object", "description": "Omit for automatic classification. Supply an explicit independent, depends_on, or continues_from decision only after resolving a returned strong candidate."},
                "relations": {"type": "array", "items": {"type": "object"}},
            },
            "required": [
                "analysis_id", "title", "project", "goal", "scope", "out_of_scope",
                "location_evidence", "targets", "ordered_steps", "review_checks", "acceptance_plan"
            ],
        },
    },
    {
        "name": "analyze_task_dependencies",
        "description": "Return structured Obsidian and Taskboard evidence for independent, depends_on, or continues_from classification.",
        "inputSchema": {"type": "object", "properties": {"title": {"type": "string"}, "project": {"type": "string"}, "goal": {"type": "string"}, "modules": {"type": "array", "items": {"type": "string"}}, "located_symbols": {"type": "array", "items": {"type": "string"}}}, "required": ["title", "project", "goal"]},
    },
    {
        "name": "detect_task_change",
        "description": "Rank active tasks that may be the target of a new or changed requirement. Never merges silently; an active match requires interactive confirmation and a completed change location analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "title": {"type": "string"}, "project": {"type": "string"},
                "goal": {"type": "string"}, "modules": {"type": "array", "items": {"type": "string"}},
                "source_thread_id": {"type": "string"}, "task_id": {"type": "string"},
            },
            "required": ["title", "project", "goal"],
        },
    },
    {
        "name": "prepare_task_change_location",
        "description": "Prepare a bounded CodeGraph location analysis for a possible revision or a new task, bound to the active candidate task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_task_id": {"type": "string"}, "title": {"type": "string"},
                "project": {"type": "string"}, "goal": {"type": "string"},
                "modules": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["candidate_task_id", "title", "project", "goal"],
        },
    },
    {
        "name": "prepare_task_change_confirmation",
        "description": "Create a pending interactive decision only after the complete revised/new task payload and its bound change location analysis are ready. Do not ask for this decision in plain text.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate_task_id": {"type": "string"}, "source_thread_id": {"type": "string"},
                "request_text": {"type": "string"}, "proposed_task": {"type": "object"},
                "evidence": {"type": "object"},
            },
            "required": ["candidate_task_id", "request_text", "proposed_task"],
        },
    },
    {
        "name": "resolve_task_change_confirmation",
        "description": "Apply an interactive task-change choice. revise preserves the task and development thread, interrupts stale work, increments context_version, and queues rework; create_new creates a separate related task.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "change_request_id": {"type": "string"},
                "decision": {"type": "string", "enum": ["revise", "create_new"]},
            },
            "required": ["change_request_id", "decision"],
        },
    },
    {
        "name": "get_task_change_confirmation",
        "description": "Read a pending or resolved task-change decision and its resulting task.",
        "inputSchema": {
            "type": "object", "properties": {"change_request_id": {"type": "string"}},
            "required": ["change_request_id"],
        },
    },
    {
        "name": "dispatch_next_task",
        "description": "Atomically claim the next dependency-ready task and return a prompt for a new independent Codex execution conversation.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string"},
                "project": {"type": "string"},
                "lease_seconds": {"type": "integer"},
            },
            "required": ["worker_id"],
        },
    },
    {
        "name": "bind_task_conversation",
        "description": "Attach a source, execution, rework, or review Codex conversation to a task/run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"}, "run_id": {"type": "string"},
                "role": {"type": "string", "enum": ["source", "execution", "rework", "bugfix", "review", "code_review", "acceptance"]},
                "thread_id": {"type": "string"}, "title": {"type": "string"},
            },
            "required": ["task_id", "role", "thread_id"],
        },
    },
    {
        "name": "submit_task_delivery",
        "description": "Submit an execution run's compact delivery summary and verification result, then move the task to review. Validation failures leave the run active: correct changed_locations from RUN_CONTEXT_JSON and retry instead of reporting the run blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"}, "delivery_summary": {"type": "string"},
                "verification_result": {"type": "string"},
                "changed_locations": {"type": "array", "items": {"type": "object"}},
                "acceptance_evidence": {"type": "array", "items": {"type": "object"}},
            },
            "required": ["run_id", "delivery_summary", "verification_result", "changed_locations", "acceptance_evidence"],
        },
    },
    {
        "name": "report_run_blocked",
        "description": "Stop an active implementation run only when it needs user confirmation or cannot continue.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
                "status": {"type": "string", "enum": ["waiting_confirmation", "blocked"]},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["task_id", "run_id", "status", "reason"],
        },
    },
    {
        "name": "review_code",
        "description": "Complete the independent code review stage.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "run_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": {"type": "array", "items": {"type": "string"}}, "passed_items": {"type": "array", "items": {"type": "string"}}, "failed_criteria": {"type": "array", "items": {"type": "string"}}}, "required": ["task_id", "run_id", "verdict"]},
    },
    {
        "name": "accept_task",
        "description": "Complete the independent functional acceptance stage; failures create a linked bug for normal tasks.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "run_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": {"type": "array", "items": {"type": "string"}}, "passed_criteria": {"type": "array", "items": {"type": "string"}}, "failed_criteria": {"type": "array", "items": {"type": "string"}}, "failure_locations": {"type": "array", "items": {"type": "object"}}}, "required": ["task_id", "run_id", "verdict", "passed_criteria", "failed_criteria"]},
    },
    {
        "name": "prepare_review_location",
        "description": "Use Obsidian and a bounded CodeGraph plan to re-locate only actual changed files/symbols before creating a review conversation.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "prepare_task_review",
        "description": "Create an independent review run from a completed review-location analysis.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "review_location_analysis_id": {"type": "string"}, "reviewer_id": {"type": "string"}},
            "required": ["task_id", "review_location_analysis_id"],
        },
    },
    {
        "name": "transition_task",
        "description": "Move a task through the validated workflow state machine.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string"},
                "reason": {"type": "string"},
                "codex_thread_id": {"type": "string"},
                "assigned_to": {"type": "string"},
                "token_used": {"type": "integer"},
                "auto_dispatch": {"type": "boolean"},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "run_acceptance_checks",
        "description": "Execute automated checks and persist results; passed checks are reused only for the same delivery and workspace fingerprint unless force=true.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
                "force": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "run_id"],
        },
    },
    {
        "name": "review_task",
        "description": "Accept or reject a task only after the current delivery's review location gate; criterion results must exactly cover the confirmed acceptance criteria.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "verdict": {"type": "string", "enum": ["pass", "fail"]},
                "reasons": {"type": "array", "items": {"type": "string"}},
                "passed_items": {"type": "array", "items": {"type": "string"}, "description": "Exact confirmed acceptance criteria that passed; all criteria are required for a pass verdict."},
                "failed_criteria": {"type": "array", "items": {"type": "string"}, "description": "Exact confirmed acceptance criteria that failed; required for fail verdict."},
                "run_id": {"type": "string"},
            },
            "required": ["task_id", "verdict", "passed_items", "run_id"],
        },
    },
    {
        "name": "relate_tasks",
        "description": "Create a typed relation between two tasks, such as changed_from or depends_on.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "source_task_id": {"type": "string"},
                "target_task_id": {"type": "string"},
                "relation_type": {"type": "string", "enum": ["changed_from", "defect_of", "depends_on", "continues_from", "blocks", "split_from", "child_of", "references", "duplicates", "replaces", "conflicts_with"]},
                "description": {"type": "string"},
            },
            "required": ["source_task_id", "target_task_id", "relation_type"],
        },
    },
    {
        "name": "suggest_task_relations",
        "description": "Recommend direct historical or active-task relations using project, module and wording overlap. Recommendations require user confirmation before persistence.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "limit": {"type": "integer"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_context",
        "description": "Return a cached immutable run snapshot when run_id is provided; otherwise compile a bounded live task context for manual planning.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string", "description": "Lifecycle run ID. Provide it in autonomous execution, review and acceptance sessions."},
                "project_path": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "get_task_details",
        "description": "Return a task with all run, conversation, review and relation records.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "update_conversation_summary",
        "description": "Store a bounded summary for a linked Codex conversation; full chat history is not copied into future contexts.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}, "thread_id": {"type": "string"}, "summary": {"type": "string"}, "status": {"type": "string"}},
            "required": ["task_id", "thread_id", "summary"],
        },
    },
    {
        "name": "open_taskboard",
        "description": "Return the local taskboard URL and startup command.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _report_location(arguments: dict[str, Any]) -> Any:
    return SERVICE.report_location_status(
        arguments["project"], arguments["available"], arguments["state"],
        arguments.get("summary", ""), arguments["evidence"], arguments.get("agent_id", ""),
    )


def _complete_location(arguments: dict[str, Any]) -> Any:
    return SERVICE.complete_location_analysis(
        arguments["analysis_id"], arguments["location_evidence"], arguments["targets"],
        arguments["acceptance_plan"], arguments.get("dependency_analysis"),
        arguments.get("implementation_contract"), arguments.get("review_contract"),
    )


def _transition_task(arguments: dict[str, Any]) -> Any:
    updates = {
        key: arguments[key]
        for key in ("codex_thread_id", "assigned_to", "token_used", "auto_dispatch")
        if key in arguments
    }
    return SERVICE.transition_task(
        arguments["task_id"], arguments["status"], arguments.get("reason", ""), **updates,
    )


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = {
    "list_board": lambda _arguments: SERVICE.board(),
    "prepare_task_location": lambda arguments: SERVICE.prepare_location_analysis(arguments, "creation"),
    "analyze_task_dependencies": SERVICE.analyze_task_dependencies,
    "detect_task_change": SERVICE.detect_task_change,
    "prepare_task_change_location": lambda arguments: SERVICE.prepare_location_analysis(
        arguments, "change", arguments["candidate_task_id"],
    ),
    "prepare_task_change_confirmation": SERVICE.prepare_task_change_confirmation,
    "resolve_task_change_confirmation": lambda arguments: SERVICE.resolve_task_change_confirmation(
        arguments["change_request_id"], arguments["decision"],
    ),
    "get_task_change_confirmation": lambda arguments: SERVICE.get_task_change_request(arguments["change_request_id"]),
    "report_location_status": _report_location,
    "complete_location_analysis": _complete_location,
    "finalize_task_intake": SERVICE.finalize_task_intake,
    "dispatch_next_task": lambda arguments: SERVICE.claim_next_task(arguments["worker_id"], arguments.get("project"), arguments.get("lease_seconds", 1800)),
    "bind_task_conversation": lambda arguments: SERVICE.bind_conversation(arguments["task_id"], arguments["role"], arguments["thread_id"], arguments.get("run_id"), arguments.get("title", "")),
    "submit_task_delivery": lambda arguments: SERVICE.submit_delivery(arguments["run_id"], arguments["delivery_summary"], arguments["verification_result"], arguments["changed_locations"], arguments["acceptance_evidence"]),
    "report_run_blocked": lambda arguments: SERVICE.report_run_blocked(
        arguments["task_id"], arguments["run_id"], arguments["status"], arguments["reason"],
    ),
    "prepare_review_location": lambda arguments: SERVICE.prepare_review_location(arguments["task_id"]),
    "prepare_task_review": lambda arguments: SERVICE.prepare_review_run(arguments["task_id"], arguments["review_location_analysis_id"], arguments.get("reviewer_id", "codex-reviewer")),
    "transition_task": _transition_task,
    "run_acceptance_checks": lambda arguments: SERVICE.run_acceptance_checks(
        arguments["task_id"], arguments["run_id"], bool(arguments.get("force", False)),
    ),
    "review_task": lambda arguments: SERVICE.review_task(arguments["task_id"], arguments["verdict"], arguments.get("reasons"), arguments.get("passed_items"), arguments.get("run_id"), arguments.get("failed_criteria")),
    "review_code": lambda arguments: SERVICE.review_code(arguments["task_id"], arguments["run_id"], arguments["verdict"], arguments.get("reasons"), arguments.get("passed_items"), arguments.get("failed_criteria")),
    "accept_task": lambda arguments: SERVICE.accept_task(arguments["task_id"], arguments["run_id"], arguments["verdict"], arguments.get("reasons"), arguments.get("passed_criteria"), arguments.get("failed_criteria"), arguments.get("failure_locations")),
    "relate_tasks": lambda arguments: SERVICE.add_relation(arguments["source_task_id"], arguments["target_task_id"], arguments["relation_type"], arguments.get("description", "")),
    "suggest_task_relations": lambda arguments: SERVICE.suggest_relations(arguments["task_id"], arguments.get("limit", 8)),
    "get_task_context": lambda arguments: (
        SERVICE.get_run_context(arguments["task_id"], arguments["run_id"], arguments.get("project_path"))
        if arguments.get("run_id")
        else SERVICE.build_context(arguments["task_id"], arguments.get("project_path"))
    ),
    "get_task_details": lambda arguments: SERVICE.task_details(arguments["task_id"]),
    "update_conversation_summary": lambda arguments: SERVICE.update_conversation_summary(arguments["task_id"], arguments["thread_id"], arguments["summary"], arguments.get("status", "completed")),
    "open_taskboard": lambda _arguments: {"url": "http://127.0.0.1:8765", "startup": "./scripts/start"},
}


def _tool_profile() -> str:
    return os.environ.get("CODEX_TASKBOARD_TOOL_PROFILE", "").strip()


def _profile_tools() -> frozenset[str] | None:
    profile = _tool_profile()
    if not profile:
        return None
    if profile not in TOOL_PROFILES:
        raise ValueError(f"Unknown Taskboard tool profile: {profile}")
    return TOOL_PROFILES[profile]


def _visible_tools() -> list[dict[str, Any]]:
    names = _profile_tools()
    if names is None:
        return TOOLS
    return [tool for tool in TOOLS if tool["name"] in names]


def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    names = _profile_tools()
    if names is not None and name not in names:
        raise ValueError(f"Tool is unavailable in {_tool_profile()} profile: {name}")
    handler = TOOL_HANDLERS.get(name)
    if handler is None:
        raise ValueError(f"Unknown tool: {name}")
    return handler(arguments)


def _task_ack(result: Any) -> dict[str, Any]:
    task = result.get("task") if isinstance(result, dict) and isinstance(result.get("task"), dict) else result
    if not isinstance(task, dict):
        return {"ok": True}
    return {
        "ok": True,
        "task_id": task.get("id"),
        "status": task.get("status"),
        "active_run_id": task.get("active_run_id"),
        "parent_acceptance_task_id": task.get("parent_acceptance_task_id"),
    }


def _compact_lifecycle_result(name: str, result: Any) -> Any:
    """Return only the fields needed for the model's next decision."""
    if _profile_tools() is None or not isinstance(result, dict):
        return result
    if name in {
        "get_task_context", "prepare_review_location", "prepare_task_review",
        "report_location_status", "complete_location_analysis",
    }:
        return result
    if name == "run_acceptance_checks":
        checks = []
        for item in result.get("checks", []):
            checks.append({
                "criterion": item.get("criterion"),
                "status": item.get("status"),
                "exit_code": item.get("exit_code"),
                "duration_ms": item.get("duration_ms"),
                "cache_hit": bool(item.get("cache_hit")),
                "output": str(item.get("output") or "")[-2000:],
            })
        return {
            "ok": True,
            "task_id": result.get("task_id"),
            "run_id": result.get("run_id"),
            "delivery_run_id": result.get("delivery_run_id"),
            "workspace_fingerprint": result.get("workspace_fingerprint"),
            "cache_hits": result.get("cache_hits", 0),
            "all_required_passed": bool(result.get("all_required_passed")),
            "checks": checks,
        }
    compact = _task_ack(result)
    if name == "submit_task_delivery":
        run = result.get("run") or {}
        compact.update({
            "run_id": run.get("id"),
            "next_stage": (result.get("task") or {}).get("status"),
            "review_dispatch_required": bool(result.get("review_dispatch_required")),
        })
    return compact


def _response(request_id: Any, result: Any = None, error: dict[str, Any] | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result
    return payload


def handle(message: dict[str, Any]) -> dict[str, Any] | None:
    method = message.get("method")
    request_id = message.get("id")
    if method == "initialize":
        return _response(request_id, {
            "protocolVersion": message.get("params", {}).get("protocolVersion", "2025-03-26"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "codex-taskboard", "version": VERSION},
        })
    if method == "notifications/initialized":
        return None
    if method == "ping":
        return _response(request_id, {})
    if method == "tools/list":
        return _response(request_id, {"tools": _visible_tools()})
    if method == "tools/call":
        try:
            result = _call_tool(message["params"]["name"], message["params"].get("arguments", {}))
            result = _compact_lifecycle_result(message["params"]["name"], result)
            return _response(request_id, {
                "content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, separators=(",", ":"))}],
                "structuredContent": result,
                "isError": False,
            })
        except Exception as exc:
            return _response(request_id, {
                "content": [{"type": "text", "text": str(exc)}],
                "isError": True,
            })
    if request_id is not None:
        return _response(request_id, error={"code": -32601, "message": f"Method not found: {method}"})
    return None


def main() -> None:
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
            response = handle(message)
            if response is not None:
                sys.stdout.write(json.dumps(response, ensure_ascii=False) + "\n")
                sys.stdout.flush()
        except Exception as exc:
            sys.stdout.write(json.dumps(_response(None, error={"code": -32603, "message": str(exc)}), ensure_ascii=False) + "\n")
            sys.stdout.flush()


if __name__ == "__main__":
    main()
