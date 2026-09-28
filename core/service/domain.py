from __future__ import annotations

from ..search import search_tokens

import json
import re
from typing import Any

from ..workflow import ACTIVE_RUN_STATUSES, CONVERSATION_ROLES, TASK_TRANSITIONS

__all__ = [
    "ACTIVE_RUN_STATUSES",
    "ACTIVE_TASK_STATUSES",
    "LOCATION_REPORT_MAX_AGE_SECONDS",
    "CONVERSATION_ROLES",
    "DISPATCH_PREPARATION_LIMIT",
    "DISPATCH_RETRY_DELAYS_SECONDS",
    "REQUIREMENT_DECOMPOSITION_ATTEMPT_LIMIT",
    "JSON_FIELDS",
    "GENERIC_MATCH_TERMS",
    "GENERIC_MODULE_KEYS",
    "RELATION_TYPES",
    "REVIEW_INTERRUPT_LIMIT",
    "REVIEW_REWORK_LIMIT",
    "REVIEW_RETRY_DELAYS_SECONDS",
    "REVIEW_STAGE_BY_RUN_TYPE",
    "RUN_JSON_FIELDS",
    "TASK_OBJECT_FIELDS",
    "TASK_TRANSITIONS",
    "decode_row",
    "search_tokens",
    "specific_modules",
]


LOCATION_REPORT_MAX_AGE_SECONDS = 3600
JSON_FIELDS = {
    "modules",
    "scope",
    "out_of_scope",
    "acceptance_criteria",
    "acceptance_plan",
    "last_review_reasons",
    "last_failed_criteria",
}
TASK_OBJECT_FIELDS = {
    "location_context",
    "dependency_analysis",
    "implementation_contract",
    "review_contract",
}
RUN_JSON_FIELDS = {
    "context_snapshot",
    "changed_locations",
    "acceptance_evidence",
    "artifact_snapshot",
}
RELATION_TYPES = {
    "changed_from",
    "defect_of",
    "depends_on",
    "blocks",
    "split_from",
    "continues_from",
    "child_of",
    "references",
    "duplicates",
    "replaces",
    "conflicts_with",
}
ACTIVE_TASK_STATUSES = {
    "claimed",
    "investigating",
    "implementing",
    "waiting_confirmation",
    "code_review",
    "failed",
    "blocked",
}
REVIEW_INTERRUPT_LIMIT = 3
REVIEW_REWORK_LIMIT = 1
EXECUTION_RECOVERY_LIMIT = 2
REVIEW_RETRY_DELAYS_SECONDS = (30, 120, 600)
REVIEW_STAGE_BY_RUN_TYPE = {
    "code_review": "code_review",
}
DISPATCH_PREPARATION_LIMIT = 3
DISPATCH_RETRY_DELAYS_SECONDS = (30, 120, 600)
REQUIREMENT_DECOMPOSITION_ATTEMPT_LIMIT = 3

# Broad layer names and common change verbs are useful search hints, but they
# are not strong enough to relate two tasks or interrupt a fast intake flow.
GENERIC_MODULE_KEYS = {
    "api",
    "backend",
    "frontend",
    "src",
    "web",
    "前端",
    "后端",
    "服务端",
    "页面",
}
GENERIC_MATCH_TERMS = {
    "api",
    "web",
    "一个",
    "代码",
    "任务",
    "优化",
    "修改",
    "功能",
    "后端",
    "增加",
    "实现",
    "当前",
    "新增",
    "本次",
    "测试",
    "相关",
    "调整",
    "需求",
    "项目",
    "页面",
    "前端",
}



def specific_modules(values: list[str] | tuple[str, ...] | set[str]) -> set[str]:
    """Return module keys that identify a business area rather than a code layer."""
    return {
        str(value).strip().lower()
        for value in values
        if str(value).strip()
        and str(value).strip().lower() not in GENERIC_MODULE_KEYS
    }


def decode_row(
    row: Any, fields: set[str] | frozenset[str] | None = None
) -> dict[str, Any]:
    item = dict(row)
    selected_fields = JSON_FIELDS if fields is None else fields
    for field in selected_fields:
        if field in item:
            item[field] = json.loads(
                item[field] or "{}"
                if field in {"context_snapshot", "location_context"}
                else item[field] or "[]"
            )
    if fields is None or set(selected_fields) == JSON_FIELDS:
        for field in TASK_OBJECT_FIELDS:
            if field in item:
                item[field] = json.loads(item[field] or "{}")
    return item


def quality_gate_required(task: dict[str, Any], gate: str) -> bool:
    gates = (task.get("review_contract") or {}).get("quality_gates")
    if not isinstance(gates, dict) or not isinstance(gates.get(gate), dict):
        raise ValueError(f"Task review contract is missing quality_gates.{gate}")
    return bool(gates[gate]["required"])


def target_conflicts(connection: Any, task_id: str, locking_only: bool = False) -> list[dict[str, Any]]:
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

def insert_relation(connection: Any, source: str, target: str, relation_type: str, description: str = "") -> None:
    if relation_type not in RELATION_TYPES:
        raise ValueError(f"Invalid relation type: {relation_type}")
    if source == target:
        raise ValueError("A task cannot relate to itself")
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


def has_project_target_scope(task: dict[str, Any]) -> bool:
    """A direct project task locates files during its exclusive execution."""
    return bool(task.get("project")) and (
        task.get("implementation_contract") or {}
    ).get("target_scope") == "project"
