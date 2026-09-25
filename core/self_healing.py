from __future__ import annotations

import re
from typing import Any


SELF_HEAL_ATTEMPT_LIMIT = 2

_ENVIRONMENT_PATTERNS = (
    r"\bcommand not found\b",
    r"\bmodule not found\b",
    r"\bmodulenotfounderror\b",
    r"\bcannot find module\b",
    r"\bno module named\b",
    r"\bfailed to resolve import\b",
    r"\bunable to resolve dependency\b",
    r"\bmissing dependency\b",
    r"\berr_pnpm_no_importer_manifest_found\b",
)

_PROJECT_PATTERNS = (
    r"\bmissing script(?::|\s)[ '\"]*test",
    r"\bno tests? (?:ran|collected|found)\b",
    r"\bno test files? found\b",
    r"\btest (?:suite|cases?|files?) (?:is|are )?missing\b",
    r"\bmissing test (?:suite|cases?|files?)\b",
    r"\bcould not find (?:a )?(?:test|config|configuration)\b",
    r"缺少测试(?:用例|文件|脚本|配置)?",
    r"未找到测试(?:用例|文件|脚本|配置)?",
    r"没有测试(?:用例|文件|脚本|配置)?",
)


def classify_recoverable_failure(*values: Any) -> str:
    """Classify only failures that the workflow may safely try to self-heal."""
    text = "\n".join(str(value or "") for value in values).lower()
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _PROJECT_PATTERNS):
        return "project"
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in _ENVIRONMENT_PATTERNS):
        return "environment"
    return "implementation"


def normalize_self_heal_locations(locations: Any) -> list[dict[str, Any]]:
    if not isinstance(locations, list):
        return []
    normalized: list[dict[str, Any]] = []
    seen: set[tuple[str, tuple[str, ...]]] = set()
    for item in locations:
        if not isinstance(item, dict):
            continue
        file = str(item.get("file") or "").strip()
        if not file:
            continue
        symbols = item.get("symbols") or []
        if not isinstance(symbols, list):
            symbols = []
        clean_symbols = tuple(
            dict.fromkeys(str(symbol).strip() for symbol in symbols if str(symbol).strip())
        )
        mode = str(item.get("mode") or "").strip().lower()
        if mode and mode not in {"modify", "create", "delete", "config"}:
            continue
        tasks = item.get("tasks") or []
        if not isinstance(tasks, list):
            tasks = []
        clean_tasks = [
            {
                key: value for key, value in task.items()
                if key in {"symbol", "action", "expected"} and value not in (None, "")
            }
            for task in tasks if isinstance(task, dict) and str(task.get("action") or "").strip()
        ]
        key = (file, clean_symbols)
        if key in seen:
            continue
        seen.add(key)
        normalized.append({
            "file": file,
            "symbols": list(clean_symbols),
            **({"mode": mode} if mode else {}),
            **({"tasks": clean_tasks} if clean_tasks else {}),
        })
    return normalized
