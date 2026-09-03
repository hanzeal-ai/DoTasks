from __future__ import annotations

import hashlib
import json
from typing import Any

from taskboard.version import VERSION


RUN_CONTEXT_SCHEMA_VERSION = 14
LIFECYCLE_TOOL_SCHEMA_REVISION = 14

LIFECYCLE_TOOL_NAMES = frozenset({
    "report_run_blocked",
    "submit_task_delivery",
    "review_code",
})

STAGE_COMPLETION_TOOLS = {
    "execution": ("report_run_blocked", "submit_task_delivery"),
    "rework": ("report_run_blocked", "submit_task_delivery"),
    "bugfix": ("report_run_blocked", "submit_task_delivery"),
    "code_review": ("review_code",),
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
        "server": "dotasks",
        "allowed_completion_tools": list(STAGE_COMPLETION_TOOLS.get(stage, ())),
    }
    return snapshot


def group_verification_checks(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group identical execution checks while preserving criterion-level evidence keys."""
    groups: list[dict[str, Any]] = []
    indexes: dict[tuple[str, str, int, str, int, str], int] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        check_type = str(item.get("check_type") or "")
        command = str(item.get("command") or "")
        timeout_seconds = int(item.get("timeout_seconds") or 300)
        repair_command = str(item.get("repair_command") or "")
        repair_timeout_seconds = int(
            item.get("repair_timeout_seconds") or timeout_seconds
        )
        failure_category = str(item.get("failure_category") or "")
        key = (
            check_type,
            command,
            timeout_seconds,
            repair_command,
            repair_timeout_seconds,
            failure_category,
        )
        criteria = item.get("criteria")
        if not isinstance(criteria, list):
            criterion = {
                field: item.get(field)
                for field in (
                    "criterion", "file", "symbol", "method", "expected",
                    "required", "artifact_refs",
                )
                if item.get(field) not in (None, "", [], {})
            }
            # Required is a contract field, not an optimization hint. Keep its
            # effective value even when the intake omitted the default.
            criterion["required"] = bool(item.get("required", True))
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
            if repair_command:
                group["repair_command"] = repair_command
                group["repair_timeout_seconds"] = repair_timeout_seconds
            if failure_category:
                group["failure_category"] = failure_category
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
    public.pop("target_snippet", None)
    stage = str(public.get("stage") or "")
    if stage in {"execution", "rework", "bugfix"}:
        task = dict(public.get("task") or {})
        constraints = {
            key: task.pop(key)
            for key in ("scope", "out_of_scope")
            if task.get(key) not in (None, "", [], {})
        }
        public["task"] = task
        if constraints:
            public["constraints"] = constraints
        if not public.get("visual_references"):
            public.pop("visual_references", None)
    elif stage == "code_review":
        task = dict(public.get("task") or {})
        public = {
            "stage": stage,
            "task": {
                key: task.get(key) for key in ("id", "title")
                if task.get(key) not in (None, "", [], {})
            },
            "diff_scope": {
                key: (public.get("diff_scope") or {}).get(key)
                for key in ("base_revision", "changed_files", "workspace_path")
                if (public.get("diff_scope") or {}).get(key) not in (None, "", [], {})
            },
            "review_checks": list(public.get("review_checks") or []),
        }
        if snapshot.get("batch"):
            public["batch"] = snapshot["batch"]
        if snapshot.get("tasks"):
            public["tasks"] = [
                {
                    key: item.get(key) for key in ("id", "title")
                    if item.get(key) not in (None, "", [], {})
                }
                for item in snapshot["tasks"] if isinstance(item, dict)
            ]
    return public


def prompt_context(snapshot: dict[str, Any]) -> str:
    """Render only the stage input that the dispatched agent must act on."""
    public = model_run_context(snapshot)
    if str(public.get("stage") or "") in {"execution", "rework", "bugfix"}:
        target_locks = [
            {
                "file": target["file"],
                "mode": str(target.get("mode") or "modify"),
                "symbols": list(target.get("symbols") or []),
            }
            for target in (public.get("targets") or [])
            if isinstance(target, dict) and target.get("file")
        ]
        batch_task_ids = [
            {"id": item["id"]}
            for item in (public.get("tasks") or [])
            if isinstance(item, dict) and item.get("id")
        ]
        public["targets"] = target_locks
        if batch_task_ids:
            public["tasks"] = batch_task_ids
        public = {
            key: public[key]
            for key in (
                "execution_environment",
                "targets",
                "verify",
                "delivery",
                "visual_references",
                "batch",
                "tasks",
            )
            if public.get(key) not in (None, "", [], {})
        }
    return json.dumps(
        public, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
