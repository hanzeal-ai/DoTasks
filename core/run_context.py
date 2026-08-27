from __future__ import annotations

import hashlib
import json
from typing import Any

from taskboard.version import VERSION


RUN_CONTEXT_SCHEMA_VERSION = 5
LIFECYCLE_TOOL_SCHEMA_REVISION = 5

LIFECYCLE_TOOL_NAMES = frozenset({
    "get_task_context",
    "transition_task",
    "report_run_blocked",
    "submit_task_delivery",
    "prepare_review_location",
    "prepare_task_review",
    "report_location_status",
    "complete_location_analysis",
    "run_acceptance_checks",
    "review_task",
    "review_code",
    "accept_task",
    "update_conversation_summary",
})

# The dispatcher starts one app-server process per active stage, so each worker
# only receives tools that can complete that stage. Non-automated review and
# reused acceptance threads keep the stable verifier profile.
TOOL_PROFILES = {
    "execution": frozenset({
        "report_run_blocked",
        "submit_task_delivery",
    }),
    "verifier": frozenset({
        "review_code",
        "run_acceptance_checks",
        "accept_task",
    }),
    "code_review": frozenset({"review_code"}),
    "acceptance": frozenset({"run_acceptance_checks", "accept_task"}),
    "legacy_review": frozenset({
        "get_task_context",
        "prepare_review_location",
        "prepare_task_review",
        "report_location_status",
        "complete_location_analysis",
        "run_acceptance_checks",
        "review_task",
    }),
    "lifecycle": LIFECYCLE_TOOL_NAMES,
}

STAGE_COMPLETION_TOOLS = {
    "execution": ("report_run_blocked", "submit_task_delivery"),
    "rework": ("report_run_blocked", "submit_task_delivery"),
    "bugfix": ("report_run_blocked", "submit_task_delivery"),
    "code_review": ("review_code",),
    "acceptance": ("run_acceptance_checks", "accept_task"),
    "review": (
        "report_location_status", "complete_location_analysis", "prepare_task_review",
        "run_acceptance_checks", "review_task",
    ),
}


def lifecycle_tool_schema_version() -> str:
    payload = json.dumps({
        "package_version": VERSION,
        "schema_revision": LIFECYCLE_TOOL_SCHEMA_REVISION,
        "tools": sorted(LIFECYCLE_TOOL_NAMES),
        "stages": STAGE_COMPLETION_TOOLS,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def freeze_run_context(
    context: dict[str, Any], *, task: dict[str, Any], run_id: str,
    stage: str, delivery_run_id: str = "",
) -> dict[str, Any]:
    """Attach immutable cache identity and the exact lifecycle tool contract."""
    snapshot = dict(context)
    snapshot["stage"] = stage
    snapshot["cache_metadata"] = {
        "task_id": task["id"],
        "run_id": run_id,
        "snapshot_schema_version": RUN_CONTEXT_SCHEMA_VERSION,
        "context_version": int(task.get("context_version") or 1),
        "delivery_run_id": delivery_run_id,
        "tool_schema_version": lifecycle_tool_schema_version(),
    }
    snapshot["tool_contract"] = {
        "server": "codex-taskboard",
        "read_context": {
            "tool": "mcp__codex_taskboard__get_task_context",
            "arguments": {"task_id": task["id"], "run_id": run_id},
            "fallback_only": True,
        },
        "allowed_completion_tools": list(STAGE_COMPLETION_TOOLS.get(stage, ())),
    }
    return snapshot


def compact_acceptance_commands(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group identical execution checks while preserving criterion-level evidence keys."""
    groups: list[dict[str, Any]] = []
    indexes: dict[tuple[str, str, int], int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        check_type = str(item.get("check_type") or "")
        command = str(item.get("command") or "")
        timeout_seconds = int(item.get("timeout_seconds") or 300)
        key = (check_type, command, timeout_seconds)
        criteria = item.get("criteria")
        if not isinstance(criteria, list):
            criterion = {
                field: item.get(field)
                for field in ("criterion", "expected")
                if item.get(field) not in (None, "", [], {})
            }
            if item.get("required") is False:
                criterion["required"] = False
            criteria = [criterion] if criterion else []
        else:
            criteria = [dict(criterion) for criterion in criteria if isinstance(criterion, dict)]
        if not criteria:
            continue
        group_index = indexes.get(key)
        if group_index is None:
            group = {
                field: value
                for field, value in (("check_type", check_type), ("command", command))
                if value
            }
            if timeout_seconds != 300:
                group["timeout_seconds"] = timeout_seconds
            group["criteria"] = []
            group_index = len(groups)
            indexes[key] = group_index
            groups.append(group)
        groups[group_index]["criteria"].extend(criteria)
    return groups


def model_run_context(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Return the model-facing snapshot without server-only validation state."""
    public = dict(snapshot)
    public.pop("workspace_baseline", None)
    public.pop("retry_chain_root_run_id", None)
    public.pop("execution_profile", None)
    public.pop("cache_metadata", None)
    public.pop("tool_contract", None)
    if isinstance(public.get("acceptance_commands"), list):
        public["acceptance_commands"] = compact_acceptance_commands(public["acceptance_commands"])
    return public


def prompt_context(snapshot: dict[str, Any]) -> str:
    """Render compact JSON once in the first turn so agents do not refetch it."""
    return json.dumps(model_run_context(snapshot), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
