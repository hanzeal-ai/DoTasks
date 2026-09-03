from __future__ import annotations

import json
import os
import sys
from typing import Any, Callable

from core.service import TaskboardService
from .remote_service import RemoteToolClient
from .version import VERSION


SERVICE = TaskboardService()

QUALITY_GATES_SCHEMA = {
    "type": "object",
    "description": (
        "Per-task stage decision made during analysis. A skipped stage does not "
        "remove acceptance criteria or development verification evidence."
    ),
    "properties": {
        gate: {
            "type": "object",
            "properties": {
                "required": {"type": "boolean"},
                "reason": {"type": "string", "minLength": 1},
            },
            "required": ["required", "reason"],
            "additionalProperties": False,
        }
        for gate in ("code_review",)
    },
    "required": ["code_review"],
    "additionalProperties": False,
}

TARGET_SCHEMA = {
    "type": "object",
    "properties": {
        "file": {"type": "string", "minLength": 1},
        "mode": {"type": "string", "enum": ["modify", "create", "delete", "config", "inspect"], "default": "modify"},
        "symbols": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "reason": {"type": "string"},
        "tasks": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "symbol": {"type": "string"},
                "action": {"type": "string", "minLength": 1},
                "expected": {"type": "string"},
            },
            "required": ["action"],
            "additionalProperties": False,
        }},
    },
    "required": ["file", "mode", "symbols"],
    "additionalProperties": False,
}

DEPENDENCY_ANALYSIS_SCHEMA = {
    "type": "object",
    "description": "DoTasks-only scheduling metadata; never exposed to development or Code Review workers.",
    "properties": {
        "decision": {"type": "string", "enum": ["independent", "depends_on", "continues_from"]},
        "depends_tasks": {"type": "array", "items": {"type": "string"}},
        "conflicts_tasks": {"type": "array", "items": {"type": "string"}},
        "history_tasks": {"type": "array", "items": {"type": "string"}},
        "continues_from_task_id": {"type": "string"},
        "relation_evidence": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "relation_type": {"type": "string", "enum": ["depends_on", "continues_from", "conflicts_with"]},
                "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                "kind": {"type": "string", "enum": ["artifact_dependency", "thread_continuation", "target_overlap"]},
                "reason": {"type": "string", "minLength": 1},
                "source": {"type": "string", "minLength": 1},
                "files": {"type": "array", "items": {"type": "string"}},
                "symbols": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["task_id", "relation_type", "confidence", "kind", "reason", "source", "files", "symbols"],
            "additionalProperties": False,
        }},
        "history_edges": {"type": "array", "items": {
            "type": "object",
            "properties": {
                "from": {"type": "string"}, "to": {"type": "string"}, "type": {"type": "string"},
            },
            "required": ["from", "to", "type"],
            "additionalProperties": False,
        }},
    },
    "required": ["decision"],
    "additionalProperties": False,
}


def _location_evidence_variant(
    tools: list[str],
    *,
    required: list[str] | None = None,
    properties: dict[str, Any] | None = None,
) -> dict[str, Any]:
    schema_properties: dict[str, Any] = {
        "tool": {"type": "string", "enum": tools},
        "query": {"type": "string", "minLength": 1},
        "files": {
            "type": "array", "minItems": 1,
            "items": {"type": "string", "minLength": 1},
        },
        "symbols": {"type": "array", "items": {"type": "string", "minLength": 1}},
        "project_path": {"type": "string", "minLength": 1},
        "repository_revision": {"type": "string"},
        "repository_workspace_fingerprint": {"type": "string"},
    }
    schema_properties.update(properties or {})
    return {
        "type": "object",
        "properties": schema_properties,
        "required": ["tool", "query", "files", *(required or [])],
        "additionalProperties": False,
    }


LOCATION_EVIDENCE_SCHEMA = {
    "description": (
        "Evidence from exactly one bounded location route. CLI evidence uses the "
        "canonical command field containing the executed argv array; never use an "
        "argv field or a shell command string."
    ),
    "oneOf": [
        _location_evidence_variant(["codegraph_explore"]),
        _location_evidence_variant(
            ["codegraph_cli_explore"],
            required=["command", "exit_code"],
            properties={
                "command": {
                    "type": "array", "minItems": 4,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Exact executed argv beginning with codegraph, explore.",
                },
                "exit_code": {"type": "integer", "enum": [0]},
            },
        ),
        _location_evidence_variant(
            ["gitnexus_query", "gitnexus_context"],
            required=["project_path"],
        ),
        _location_evidence_variant(
            ["gitnexus_cli_query"],
            required=["project_path", "command", "exit_code"],
            properties={
                "command": {
                    "type": "array", "minItems": 3,
                    "items": {"type": "string", "minLength": 1},
                    "description": "Exact executed GitNexus argv array.",
                },
                "exit_code": {"type": "integer", "enum": [0]},
            },
        ),
        _location_evidence_variant(
            ["source_match"],
            required=["project_path", "commands"],
            properties={
                "commands": {
                    "type": "array", "minItems": 1,
                    "items": {
                        "type": "array", "minItems": 1,
                        "items": {"type": "string", "minLength": 1},
                    },
                },
            },
        ),
    ],
}


TOOLS = [
    {
        "name": "list_board",
        "description": "List confirmed delivery tasks, counts, projects and integration status for the taskboard.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "upload_visual_artifact",
        "description": "Upload one PNG, JPEG, GIF, or WebP from a local source path into DoTasks-managed server storage before creating a requirement or task. Use the returned artifact_id in visual_references.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "minLength": 1},
                "purpose": {"type": "string"},
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "read_visual_artifact",
        "description": "Read one managed visual artifact by artifact_id. This is primarily used by the Local Agent to materialize server images before dispatch.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "artifact_id": {"type": "string", "pattern": "^artifact://"},
            },
            "required": ["artifact_id"],
            "additionalProperties": False,
        },
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
                "evidence": LOCATION_EVIDENCE_SCHEMA,
                "agent_id": {"type": "string"},
            },
            "required": ["project", "available", "state", "evidence"],
        },
    },
    {
        "name": "complete_location_analysis",
        "description": "Persist bounded location evidence, an explicit code-review decision, and a criterion-level acceptance plan. Exact targets and target tasks are required only for code-changing work; non-applicable read-only fields may be empty.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "analysis_id": {"type": "string"}, "location_evidence": LOCATION_EVIDENCE_SCHEMA,
                "targets": {"type": "array", "items": TARGET_SCHEMA},
                "dependency_analysis": DEPENDENCY_ANALYSIS_SCHEMA,
                "implementation_contract": {"type": "object", "description": "For code-changing tasks, targets and their tasks must match the completed location evidence exactly. Read-only tasks may omit this contract or use an empty target array.", "properties": {
                    "targets": {"type": "array", "items": TARGET_SCHEMA},
                }, "required": ["targets"], "additionalProperties": False},
                "review_contract": {
                    "type": "object",
                    "description": "Code-quality-only Review contract. Task goals and acceptance_plan remain outside Code Review and are handled by the existing human acceptance process. Non-code tasks set quality_gates.code_review.required=false.",
                    "properties": {
                        "checks": {
                            "type": "array",
                            "description": "Optional explicit code-quality checks. Read-only tasks may use an empty array; code-changing tasks may omit this field to use defaults.",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "id": {"type": "string", "minLength": 1},
                                    "description": {"type": "string", "minLength": 1},
                                    "kind": {"type": "string", "enum": ["code", "static"]},
                                },
                                "required": ["id", "description", "kind"],
                                "additionalProperties": False,
                            },
                        },
                        "quality_gates": QUALITY_GATES_SCHEMA,
                    },
                    "required": ["quality_gates"],
                    "additionalProperties": False,
                },
                "acceptance_plan": {"type": "array", "items": {"type": "object", "properties": {
                    "criterion": {"type": "string"}, "file": {"type": "string"}, "symbol": {"type": "string"},
                    "method": {"type": "string"}, "command": {"type": "string"}, "expected": {"type": "string"},
                    "check_type": {"type": "string", "enum": ["automated", "static_review", "manual_runtime"]},
                    "required": {"type": "boolean"}, "timeout_seconds": {"type": "integer"},
                    "failure_category": {"type": "string", "enum": ["project", "environment", "implementation"]},
                    "repair_command": {"type": "string"}, "repair_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800}
                }, "required": ["criterion", "method", "expected"]}},
            },
            "required": ["analysis_id", "location_evidence", "review_contract", "acceptance_plan"],
        },
    },
    {
        "name": "finalize_task_intake",
        "description": "Persist intake_kind=requirement as a planning entity, or create a ready direct task for intake_kind=task. A successful auto-dispatched result returns controller_kickoff_required=true and a mandatory controller_kickoff next action.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "intake_kind": {"type": "string", "enum": ["requirement", "task"]},
                "analysis_id": {"type": "string"},
                "title": {"type": "string"},
                "project": {
                    "type": "string",
                    "description": "Absolute path to the existing project directory used by the prepared location analysis.",
                },
                "modules": {"type": "array", "items": {"type": "string"}},
                "goal": {"type": "string"},
                "original_content": {"type": "string", "description": "Original complete requirement text; used only for requirement intake."},
                "description": {"type": "string"},
                "scope": {"type": "array", "items": {"type": "string"}},
                "out_of_scope": {"type": "array", "items": {"type": "string"}},
                "priority": {"type": "string", "enum": ["P0", "P1", "P2", "P3"]},
                "source_thread_id": {"type": "string"},
                "type": {"type": "string", "enum": ["feature", "optimization", "refactor", "bug"]},
                "token_budget": {"type": "integer"},
                "auto_dispatch": {"type": "boolean"},
                "location_summary": {"type": "string"},
                "agent_id": {"type": "string"},
                "location_evidence": LOCATION_EVIDENCE_SCHEMA,
                "targets": {"type": "array", "items": TARGET_SCHEMA},
                "visual_references": {"type": "array", "maxItems": 8, "description": "All requirement screenshots and visual references. Pass either a source path or the artifact_id returned by upload_visual_artifact.", "items": {"type": "object", "properties": {"path": {"type": "string", "minLength": 1}, "artifact_id": {"type": "string", "pattern": "^artifact://"}, "filename": {"type": "string"}, "purpose": {"type": "string"}}, "anyOf": [{"required": ["path"]}, {"required": ["artifact_id"]}], "additionalProperties": False}},
                "review_checks": {"type": "array", "description": "Optional explicit code-quality checks. Read-only tasks may use an empty array; code-changing tasks may omit this field to use defaults. Never copy task acceptance criteria here.", "items": {"type": "object", "properties": {"id": {"type": "string", "minLength": 1}, "description": {"type": "string", "minLength": 1}, "kind": {"type": "string", "enum": ["code", "static"]}}, "required": ["id", "description", "kind"]}},
                "quality_gates": QUALITY_GATES_SCHEMA,
                "acceptance_plan": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
                    "criterion": {"type": "string"}, "file": {"type": "string"}, "symbol": {"type": "string"},
                    "method": {"type": "string"}, "command": {"type": "string"}, "expected": {"type": "string"},
                    "check_type": {"type": "string", "enum": ["automated", "static_review", "manual_runtime"]},
                    "required": {"type": "boolean", "default": True}, "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                    "failure_category": {"type": "string", "enum": ["project", "environment", "implementation"]},
                    "repair_command": {"type": "string"}, "repair_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
                    "artifact_refs": {"type": "array", "items": {"type": "string"}}
                }, "required": ["criterion", "method", "expected"]}},
                "dependency_analysis": DEPENDENCY_ANALYSIS_SCHEMA,
                "relations": {"type": "array", "items": {"type": "object"}},
                "acceptance_criteria": {"type": "array", "items": {"type": "string"}, "description": "Requirement-level outcomes; direct task criteria continue to be derived from acceptance_plan."},
                "decomposition_tasks": {"type": "array", "items": {"type": "object"}, "description": "Optional stable decomposition plan, materialized only when the requirement is dispatched."},
            },
            "required": ["intake_kind", "title", "project", "goal"],
            "anyOf": [
                {"properties": {"intake_kind": {"const": "requirement"}}, "required": ["intake_kind"]},
                {"properties": {"intake_kind": {"const": "task"}}, "required": [
                    "intake_kind",
                    "analysis_id", "location_evidence", "quality_gates",
                    "acceptance_plan"
                ]}
            ],
            "additionalProperties": False,
        },
    },
    {
        "name": "get_requirement",
        "description": "Query a requirement, its decomposition state, every child task and their persisted dependency relations.",
        "inputSchema": {"type": "object", "properties": {"requirement_id": {"type": "string"}}, "required": ["requirement_id"]},
    },
    {
        "name": "submit_requirement_decomposition",
        "description": "Idempotently complete an active requirement decomposition run by creating ready child tasks and dependency relations.",
        "inputSchema": {"type": "object", "properties": {
            "requirement_id": {"type": "string"}, "run_id": {"type": "string"},
            "tasks": {"type": "array", "minItems": 1, "items": {
                "type": "object", "properties": {
                    "key": {"type": "string", "minLength": 1},
                    "title": {"type": "string", "minLength": 1},
                    "goal": {"type": "string", "minLength": 1},
                    "analysis_id": {"type": "string", "minLength": 1},
                    "location_evidence": LOCATION_EVIDENCE_SCHEMA,
                    "targets": {"type": "array", "items": TARGET_SCHEMA},
                    "review_checks": {"type": "array", "description": "Optional explicit code-quality checks. Read-only tasks may use an empty array; code-changing tasks may omit this field to use defaults."},
                    "quality_gates": QUALITY_GATES_SCHEMA,
                    "acceptance_plan": {"type": "array", "minItems": 1, "items": {
                        "type": "object",
                        "properties": {
                            "criterion": {"type": "string", "minLength": 1},
                            "file": {"type": "string"},
                            "symbol": {"type": "string"},
                            "method": {"type": "string", "minLength": 1},
                            "command": {"type": "string"},
                            "expected": {"type": "string", "minLength": 1},
                            "check_type": {"type": "string", "enum": ["automated", "static_review", "manual_runtime"]},
                            "required": {"type": "boolean"},
                            "timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 3600},
                            "failure_category": {"type": "string", "enum": ["project", "environment", "implementation"]},
                            "repair_command": {"type": "string"},
                            "repair_timeout_seconds": {"type": "integer", "minimum": 1, "maximum": 1800},
                            "artifact_refs": {"type": "array", "items": {"type": "string"}},
                        },
                        "required": ["criterion", "method", "expected"],
                    }},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                }, "required": [
                    "key", "title", "goal", "analysis_id", "location_evidence",
                    "quality_gates", "acceptance_plan",
                ],
            }},
        }, "required": ["requirement_id", "run_id", "tasks"]},
    },
    {
        "name": "report_requirement_decomposition_failed",
        "description": "Release a failed decomposition claim so the same requirement can be safely retried.",
        "inputSchema": {"type": "object", "properties": {
            "requirement_id": {"type": "string"}, "run_id": {"type": "string"},
            "error": {"type": "string"},
        }, "required": ["requirement_id", "run_id", "error"]},
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
        "name": "set_dispatcher_enabled",
        "description": "Enable or disable new DoTasks dispatch claims without changing task states or interrupting active runs.",
        "inputSchema": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}},
            "required": ["enabled"],
        },
    },
    {
        "name": "claim_schedule_cycle",
        "description": "Lease one durable scheduling wakeup, recover the code_review lane, and fill every available development slot in one cycle.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "minLength": 1},
                "project": {"type": "string"},
                "lease_seconds": {"type": "integer", "minimum": 300, "maximum": 7200},
                "force": {"type": "boolean"},
            },
            "required": ["worker_id"],
        },
    },
    {
        "name": "complete_schedule_cycle",
        "description": "Acknowledge the processed scheduler generation and release the global Controller lease. A newer state change remains pending.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "worker_id": {"type": "string", "minLength": 1},
                "generation": {"type": "integer", "minimum": 0},
            },
            "required": ["worker_id", "generation"],
        },
    },
    {
        "name": "mark_dispatch_pending",
        "description": "Persist an asynchronous native Codex task creation while only clientThreadId is available. Do not bind the run until a real threadId is resolved.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}, "client_thread_id": {"type": "string"},
            "host_id": {"type": "string"}, "codex_project_id": {"type": "string"},
            "dispatch_attempt_id": {"type": "string"},
        }, "required": ["run_id", "client_thread_id", "dispatch_attempt_id"]},
    },
    {
        "name": "bind_native_dispatch",
        "description": "Bind a persisted dispatch to its real native Codex thread. Supply resume_fallback_reason only when resuming the recorded thread failed and a replacement native task was created.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}, "thread_id": {"type": "string"},
            "host_id": {"type": "string"}, "codex_project_id": {"type": "string"},
            "resume_fallback_reason": {"type": "string"},
            "dispatch_attempt_id": {"type": "string"},
        }, "required": ["run_id", "thread_id", "dispatch_attempt_id"]},
    },
    {
        "name": "renew_dispatch_lease",
        "description": "Renew the claimed stage while the native Codex task is being created or executed.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"},
            "lease_seconds": {"type": "integer", "minimum": 300, "maximum": 7200},
        }, "required": ["run_id"]},
    },
    {
        "name": "get_dispatch_status",
        "description": "Read a native dispatch and reconcile its terminal state from the underlying DoTasks run.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"},
        }, "required": ["run_id"]},
    },
    {
        "name": "report_dispatch_failed",
        "description": "Fail an unsubmitted native dispatch and release it for the scheduler's retry policy.",
        "inputSchema": {"type": "object", "properties": {
            "run_id": {"type": "string"}, "reason": {"type": "string", "minLength": 1},
        }, "required": ["run_id", "reason"]},
    },
    {
        "name": "submit_task_delivery",
        "description": "Submit an execution run's compact delivery summary and verification result. Code-changing runs must report exact changed_locations; read-only and projectless runs must pass changed_locations=[]. When RUN_CONTEXT_JSON.batch has appended tasks, pass its exact batch_revision. Validation failures leave the run active: correct the payload and retry instead of reporting the run blocked.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"}, "delivery_summary": {"type": "string"},
                "verification_result": {"type": "string"},
                "workspace_path": {"type": "string", "description": "Required for a worktree dispatch; use the worker's absolute current Git worktree path."},
                "batch_revision": {"type": "integer", "minimum": 1},
                "changed_locations": {"type": "array", "items": {"type": "object"}},
                "acceptance_evidence": {"type": "array", "items": {"type": "object", "properties": {"criterion": {"type": "string", "minLength": 1}, "status": {"type": "string", "enum": ["passed", "failed", "blocked", "pending"]}, "evidence": {"type": "string", "minLength": 1}, "artifact_refs": {"type": "array", "items": {"type": "string"}}}, "required": ["criterion", "status", "evidence"]}},
            },
            "required": ["run_id", "delivery_summary", "verification_result", "changed_locations", "acceptance_evidence"],
        },
    },
    {
        "name": "report_run_blocked",
        "description": "Stop an active implementation run for attention, or schedule one bounded safely located project/environment repair.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "run_id": {"type": "string"},
                "status": {"type": "string", "enum": ["waiting_confirmation", "blocked"]},
                "reason": {"type": "string", "minLength": 1},
                "failure_category": {
                    "type": "string",
                    "enum": ["project", "environment", "implementation"],
                    "description": "Optional safe-repair classification. Project/environment repair also requires exact failure_locations.",
                },
                "failure_locations": {
                    "type": "array",
                    "minItems": 1,
                    "description": "Optional exact files for one bounded project/environment repair before attention.",
                    "items": TARGET_SCHEMA,
                },
            },
            "required": ["task_id", "run_id", "status", "reason"],
        },
    },
    {
        "name": "review_code",
        "description": "Complete code-quality-only Review. Judge code quality, security vulnerabilities, and cohesion/coupling; do not judge task goals or acceptance criteria. Only concrete blocking quality findings may fail, with exact actionable evidence.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}, "run_id": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail"]}, "reasons": {"type": "array", "items": {"type": "string"}}, "passed_items": {"type": "array", "items": {"type": "string"}}, "failed_criteria": {"type": "array", "items": {"type": "string"}}, "failure_category": {"type": "string", "enum": ["project", "environment", "implementation"]}, "failure_locations": {"type": "array", "minItems": 1, "description": "Required for project/environment failures. Each item identifies an exact project file to create or modify during bounded self-healing rework.", "items": TARGET_SCHEMA}}, "required": ["task_id", "run_id", "verdict"]},
    },
    {
        "name": "get_task_details",
        "description": "Return a task with all run, conversation, review and relation records.",
        "inputSchema": {"type": "object", "properties": {"task_id": {"type": "string"}}, "required": ["task_id"]},
    },
    {
        "name": "open_taskboard",
        "description": "Return the local taskboard URL and startup command.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def tool_handlers_for(
    service: TaskboardService,
) -> dict[str, Callable[[dict[str, Any]], Any]]:
    return {
        "list_board": lambda _arguments: service.board(),
        "upload_visual_artifact": service.upload_visual_artifact,
        "read_visual_artifact": lambda arguments: service.read_visual_artifact(
            arguments["artifact_id"]
        ),
        "prepare_task_location": lambda arguments: service.prepare_location_analysis(
            arguments, "creation"
        ),
        "detect_task_change": service.detect_task_change,
        "prepare_task_change_location": lambda arguments: service.prepare_location_analysis(
            arguments, "change", arguments["candidate_task_id"]
        ),
        "prepare_task_change_confirmation": service.prepare_task_change_confirmation,
        "resolve_task_change_confirmation": lambda arguments: service.resolve_task_change_confirmation(
            arguments["change_request_id"], arguments["decision"]
        ),
        "get_task_change_confirmation": lambda arguments: service.get_task_change_request(
            arguments["change_request_id"]
        ),
        "report_location_status": lambda arguments: service.report_location_status(
            arguments["project"],
            arguments["available"],
            arguments["state"],
            arguments.get("summary", ""),
            arguments["evidence"],
            arguments.get("agent_id", ""),
        ),
        "complete_location_analysis": lambda arguments: service.complete_location_analysis(
            arguments["analysis_id"],
            arguments["location_evidence"],
            arguments.get("targets", []),
            arguments["acceptance_plan"],
            arguments.get("dependency_analysis"),
            arguments.get("implementation_contract"),
            arguments.get("review_contract"),
        ),
        "finalize_task_intake": service.finalize_task_intake,
        "get_requirement": lambda arguments: service.get_requirement(
            arguments["requirement_id"]
        ),
        "submit_requirement_decomposition": lambda arguments: service.submit_requirement_decomposition(
            arguments["requirement_id"], arguments["run_id"], arguments["tasks"]
        ),
        "report_requirement_decomposition_failed": lambda arguments: service.fail_requirement_decomposition(
            arguments["requirement_id"], arguments["run_id"], arguments["error"]
        ),
        "set_dispatcher_enabled": lambda arguments: service.set_dispatcher_enabled(
            arguments["enabled"]
        ),
        "claim_schedule_cycle": lambda arguments: service.claim_schedule_cycle(
            arguments["worker_id"],
            arguments.get("project"),
            arguments.get("lease_seconds", 1800),
            force=bool(arguments.get("force", False)),
        ),
        "complete_schedule_cycle": lambda arguments: service.complete_schedule_cycle(
            arguments["worker_id"], arguments["generation"]
        ),
        "mark_dispatch_pending": lambda arguments: service.mark_native_dispatch_pending(
            arguments["run_id"],
            arguments["client_thread_id"],
            arguments.get("host_id", ""),
            arguments.get("codex_project_id", ""),
            dispatch_attempt_id=arguments["dispatch_attempt_id"],
        ),
        "bind_native_dispatch": lambda arguments: service.bind_native_dispatch(
            arguments["run_id"],
            arguments["thread_id"],
            arguments.get("host_id", ""),
            arguments.get("codex_project_id", ""),
            resume_fallback_reason=arguments.get("resume_fallback_reason", ""),
            dispatch_attempt_id=arguments["dispatch_attempt_id"],
        ),
        "renew_dispatch_lease": lambda arguments: service.renew_native_dispatch(
            arguments["run_id"], arguments.get("lease_seconds", 1800)
        ),
        "get_dispatch_status": lambda arguments: service.get_native_dispatch(
            arguments["run_id"]
        ),
        "report_dispatch_failed": lambda arguments: service.fail_native_dispatch(
            arguments["run_id"], arguments["reason"]
        ),
        "submit_task_delivery": lambda arguments: service.submit_delivery(
            arguments["run_id"],
            arguments["delivery_summary"],
            arguments["verification_result"],
            arguments["changed_locations"],
            arguments["acceptance_evidence"],
            batch_revision=arguments.get("batch_revision"),
            workspace_path=arguments.get("workspace_path"),
        ),
        "report_run_blocked": lambda arguments: service.report_run_blocked(
            arguments["task_id"],
            arguments["run_id"],
            arguments["status"],
            arguments["reason"],
            arguments.get("failure_category"),
            arguments.get("failure_locations"),
        ),
        "review_code": lambda arguments: service.review_code(
            arguments["task_id"],
            arguments["run_id"],
            arguments["verdict"],
            arguments.get("reasons"),
            arguments.get("passed_items"),
            arguments.get("failed_criteria"),
            arguments.get("failure_category"),
            arguments.get("failure_locations"),
        ),
        "get_task_details": lambda arguments: service.task_details(
            arguments["task_id"]
        ),
        "open_taskboard": lambda _arguments: {
            "url": getattr(service, "taskboard_url", "http://127.0.0.1:8765"),
            "startup": "./scripts/start",
        },
    }


TOOL_HANDLERS: dict[str, Callable[[dict[str, Any]], Any]] = tool_handlers_for(SERVICE)


def _visible_tools() -> list[dict[str, Any]]:
    return TOOLS


def _call_tool(name: str, arguments: dict[str, Any]) -> Any:
    if os.environ.get("DOTASKS_REMOTE_SERVICE") == "1":
        remote = RemoteToolClient.from_environment()
        if remote is None:
            raise RuntimeError("DOTASKS_REMOTE_SERVICE requires Cloud Agent configuration")
        if name == "upload_visual_artifact" and arguments.get("path"):
            return remote.upload_local_visual_artifact(arguments)
        if name == "finalize_task_intake" and arguments.get("visual_references"):
            arguments = dict(arguments)
            references = []
            for item in arguments["visual_references"]:
                if item.get("artifact_id") or item.get("content_base64"):
                    references.append(item)
                else:
                    references.append(remote.upload_local_visual_artifact(item))
            arguments["visual_references"] = references
        return remote.call(name, arguments)
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
    }


DISPATCH_RESULT_TOOLS = frozenset({
    "mark_dispatch_pending",
    "bind_native_dispatch",
    "renew_dispatch_lease",
    "get_dispatch_status",
    "report_dispatch_failed",
})


def _dispatch_result(result: Any) -> Any:
    if not isinstance(result, dict):
        return result
    fields = (
        "run_id", "entity_type", "entity_id", "role", "status", "worker_id",
        "project_path", "dispatch_title", "dispatch_prompt", "resume_thread_id",
        "client_thread_id", "thread_id", "host_id", "codex_project_id",
        "resume_fallback_reason", "error", "execution_environment",
        "parallel_fallback_reason", "base_revision", "base_ref",
        "dispatch_attempt_id",
        "resume_required", "can_create_fallback",
    )
    return {field: result.get(field) for field in fields}


def _compact_lifecycle_result(name: str, result: Any) -> Any:
    """Return only the fields needed for the model's next decision."""
    if not isinstance(result, dict):
        return result
    if name in {
        "upload_visual_artifact",
        "prepare_task_location", "report_location_status", "complete_location_analysis",
        "finalize_task_intake", "get_requirement",
        "submit_requirement_decomposition",
        "set_dispatcher_enabled", "complete_schedule_cycle",
    }:
        return result
    if name in DISPATCH_RESULT_TOOLS:
        return _dispatch_result(result)
    if name == "claim_schedule_cycle":
        return {
            "status": result.get("status"),
            "worker_id": result.get("worker_id"),
            "cycle_generation": result.get("cycle_generation"),
            "scheduler": result.get("scheduler"),
            "code_review": {
                "stage": (result.get("code_review") or {}).get("stage"),
                "capacity": (result.get("code_review") or {}).get("capacity"),
                "dispatches": [
                    _dispatch_result(dispatch)
                    for dispatch in (result.get("code_review") or {}).get("dispatches", [])
                ],
            },
            "development": {
                "stage": (result.get("development") or {}).get("stage"),
                "capacity": (result.get("development") or {}).get("capacity"),
                "dispatches": [
                    _dispatch_result(dispatch)
                    for dispatch in (result.get("development") or {}).get("dispatches", [])
                ],
            },
        }
    compact = _task_ack(result)
    if name == "submit_task_delivery":
        run = result.get("run") or {}
        compact.update({
            "run_id": run.get("id"),
            "next_stage": (result.get("task") or {}).get("status"),
            "review_dispatch_required": bool(result.get("review_dispatch_required")),
            "continue_development": bool(result.get("continue_development")),
            "batch": result.get("batch"),
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
            "serverInfo": {"name": "dotasks", "version": VERSION},
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


def _call_tool_from_cli(arguments: list[str]) -> int:
    """Call one lifecycle tool when the Codex MCP registry failed to load."""
    if len(arguments) != 2 or arguments[0] != "--call-tool":
        sys.stderr.write("usage: mcp-server --call-tool TOOL_NAME\n")
        return 2
    raw_arguments = sys.stdin.read().strip() or "{}"
    try:
        tool_arguments = json.loads(raw_arguments)
    except json.JSONDecodeError as exc:
        sys.stderr.write(f"Invalid tool arguments JSON: {exc}\n")
        return 2
    if not isinstance(tool_arguments, dict):
        sys.stderr.write("Tool arguments must be a JSON object\n")
        return 2
    response = handle({
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": arguments[1], "arguments": tool_arguments},
    })
    result = (response or {}).get("result") or {}
    if (response or {}).get("error") or result.get("isError"):
        error = (response or {}).get("error")
        if not error:
            content = result.get("content") or []
            error = content[0].get("text") if content else "DoTasks tool call failed"
        sys.stderr.write(f"{error}\n")
        return 1
    sys.stdout.write(
        json.dumps(result.get("structuredContent"), ensure_ascii=False) + "\n"
    )
    return 0


def main() -> None:
    if len(sys.argv) > 1:
        raise SystemExit(_call_tool_from_cli(sys.argv[1:]))
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
