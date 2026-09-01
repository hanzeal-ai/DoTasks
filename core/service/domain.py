from __future__ import annotations

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
    "REVIEW_RETRY_DELAYS_SECONDS",
    "REVIEW_STAGE_BY_RUN_TYPE",
    "RUN_JSON_FIELDS",
    "TASK_OBJECT_FIELDS",
    "TASK_TRANSITIONS",
    "decode_row",
    "search_tokens",
    "specific_modules",
    "decode_row",
    "search_tokens",
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


def search_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(
        r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+", str(value or "").lower()
    ):
        tokens.add(raw)
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw) and len(raw) > 2:
            for size in (2, 3, 4):
                if len(raw) >= size:
                    tokens.update(
                        raw[index : index + size]
                        for index in range(len(raw) - size + 1)
                    )
    return tokens


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
