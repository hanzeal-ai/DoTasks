from __future__ import annotations

from typing import Any, Iterable

# Canonical task/run vocabulary. Database validation and frontend presentation
# are derived from these values instead of maintaining separate copies.
TASK_TRANSITIONS = {
    "draft": {"ready", "paused", "cancelled"},
    "ready": {"claimed", "waiting_confirmation", "paused", "cancelled", "blocked"},
    "claimed": {
        "investigating",
        "implementing",
        "ready",
        "waiting_confirmation",
        "failed",
        "paused",
        "blocked",
    },
    "investigating": {
        "implementing",
        "code_review",
        "done",
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
        "code_review",
        "done",
        "failed",
        "paused",
        "blocked",
        "cancelled",
    },
    "blocked": {
        "ready",
        "rework",
        "code_review",
        "paused",
        "cancelled",
    },
    "paused": {"cancelled"},
    "failed": {"ready", "paused", "blocked", "cancelled"},
    "code_review": {"waiting_confirmation", "paused", "blocked", "cancelled", "rework", "done"},
    "rework": {"claimed", "waiting_confirmation", "code_review", "done", "paused", "blocked", "cancelled"},
    "done": set(),
    "cancelled": set(),
}

TASK_STATUSES = frozenset(TASK_TRANSITIONS)
RUN_TYPES = frozenset({"execution", "rework", "bugfix", "code_review"})
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
        "statuses": ("code_review",),
    },
    {
        "key": "attention",
        "title": "待处理",
        "statuses": (),
    },
)
STATUS_LABELS = {
    "draft": "草稿",
    "ready": "待领取",
    "claimed": "已领取",
    "investigating": "分析中",
    "implementing": "实现中",
    "rework": "返工",
    "code_review": "Code Review",
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
)
ATTENTION_STATUSES = frozenset({"waiting_confirmation", "failed", "blocked", "paused"})
AUTO_DISPATCH_ATTENTION_STATUSES = frozenset(
    {"ready", "rework", "code_review"}
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
