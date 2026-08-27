from __future__ import annotations

from typing import Any, Iterable

# Canonical task/run vocabulary. Database validation and frontend presentation
# are derived from these values instead of maintaining separate copies.
TASK_TRANSITIONS = {
    "draft": {"ready", "paused", "cancelled"},
    "ready": {"claimed", "paused", "cancelled", "blocked"},
    "claimed": {"investigating", "ready", "failed", "paused", "blocked"},
    "investigating": {
        "implementing",
        "waiting_confirmation",
        "failed",
        "paused",
        "blocked",
        "cancelled",
    },
    "waiting_confirmation": {"ready", "rework", "paused", "cancelled"},
    "implementing": {
        "ready",
        "waiting_confirmation",
        "failed",
        "paused",
        "blocked",
        "cancelled",
    },
    "blocked": {
        "ready",
        "rework",
        "review",
        "code_review",
        "acceptance",
        "paused",
        "cancelled",
    },
    "paused": {"cancelled"},
    "failed": {"ready", "paused", "blocked", "cancelled"},
    "review": {"paused", "blocked", "cancelled", "acceptance"},
    "code_review": {"paused", "blocked", "cancelled", "rework", "acceptance"},
    "acceptance": {"paused", "blocked", "cancelled", "done", "acceptance_blocked"},
    "acceptance_blocked": {"paused", "cancelled", "acceptance"},
    "rework": {"claimed", "waiting_confirmation", "paused", "blocked", "cancelled"},
    "done": set(),
    "cancelled": set(),
}

TASK_STATUSES = frozenset(TASK_TRANSITIONS)
RUN_TYPES = frozenset(
    {"execution", "rework", "bugfix", "review", "code_review", "acceptance"}
)
RUN_STATUSES = frozenset(
    {
        "awaiting_thread",
        "running",
        "waiting_review",
        "review_failed",
        "completed",
        "interrupted",
        "expired",
    }
)
ACTIVE_RUN_STATUSES = frozenset({"awaiting_thread", "running"})
CONVERSATION_ROLES = frozenset({"source", *RUN_TYPES})

BOARD_COLUMNS = (
    {"key": "ready", "title": "任务队列", "statuses": ("draft", "ready", "claimed")},
    {
        "key": "implementing",
        "title": "开发中",
        "statuses": ("investigating", "implementing", "rework"),
    },
    {
        "key": "code-review",
        "title": "Code Review",
        "statuses": ("code_review", "review"),
    },
    {
        "key": "acceptance",
        "title": "验收中",
        "statuses": ("acceptance", "acceptance_blocked"),
    },
    {"key": "done", "title": "完成", "statuses": ("done",)},
)
STATUS_LABELS = {
    "draft": "草稿",
    "ready": "待领取",
    "claimed": "已领取",
    "investigating": "分析中",
    "implementing": "实现中",
    "rework": "返工",
    "review": "待验收",
    "code_review": "Code Review",
    "acceptance": "验收中",
    "acceptance_blocked": "验收阻塞",
    "waiting_confirmation": "待确认",
    "failed": "执行失败",
    "blocked": "阻塞",
    "paused": "已暂停",
    "done": "已完成",
    "cancelled": "已取消",
}
TOKEN_STAGES = (
    {"key": "execution", "label": "开发"},
    {"key": "rework", "label": "返工"},
    {"key": "bugfix", "label": "Bug 修复"},
    {"key": "code_review", "label": "Code Review"},
    {"key": "acceptance", "label": "功能验收"},
    {"key": "review", "label": "旧版验收"},
)
ATTENTION_STATUSES = frozenset({"waiting_confirmation", "failed", "blocked", "paused"})
AUTO_DISPATCH_ATTENTION_STATUSES = frozenset(
    {"ready", "rework", "review", "code_review", "acceptance"}
)


def task_requires_attention(task: dict[str, Any]) -> bool:
    """Return whether an operator must inspect or resume a non-terminal task."""
    status = str(task.get("status") or "")
    return status in ATTENTION_STATUSES or (
        status in AUTO_DISPATCH_ATTENTION_STATUSES
        and int(task.get("auto_dispatch", 1)) == 0
    )


def sql_values(values: Iterable[str]) -> str:
    """Build a deterministic SQL string list from trusted workflow constants."""
    return ",".join(f"'{value}'" for value in sorted(values))


def workflow_metadata() -> dict[str, Any]:
    return {
        "columns": [
            {**column, "statuses": list(column["statuses"])} for column in BOARD_COLUMNS
        ],
        "status_labels": dict(STATUS_LABELS),
        "token_stages": [dict(stage) for stage in TOKEN_STAGES],
        "attention_statuses": sorted(ATTENTION_STATUSES),
        "attention_auto_dispatch_statuses": sorted(AUTO_DISPATCH_ATTENTION_STATUSES),
    }
