from __future__ import annotations

import json
import re
import shlex
from pathlib import Path
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


def infer_environment_repair_command(project: str | Path, output: str) -> str:
    """Return a deterministic dependency restore command, never a system installer."""
    root = Path(project).expanduser().resolve()
    normalized = str(output or "").lower()
    if classify_recoverable_failure(normalized) != "environment":
        return ""

    if (root / "package.json").is_file() and any(
        marker in normalized
        for marker in (
            "module not found",
            "cannot find module",
            "failed to resolve import",
            "unable to resolve dependency",
            "missing dependency",
        )
    ):
        package_json: dict[str, Any] = {}
        try:
            package_json = json.loads((root / "package.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            pass
        package_manager = str(package_json.get("packageManager") or "").split("@", 1)[0]
        if (root / "pnpm-lock.yaml").is_file() or package_manager == "pnpm":
            return "pnpm install --frozen-lockfile"
        if (root / "yarn.lock").is_file() or package_manager == "yarn":
            return "yarn install --frozen-lockfile"
        if (root / "package-lock.json").is_file() or (root / "npm-shrinkwrap.json").is_file():
            return "npm ci"

    if any(
        marker in normalized
        for marker in ("modulenotfounderror", "no module named", "module not found")
    ):
        if (root / "pyproject.toml").is_file() and (root / "uv.lock").is_file():
            return "uv sync --frozen"
        if (root / "requirements.txt").is_file():
            return f"python -m pip install -r {shlex.quote(str(root / 'requirements.txt'))}"
    return ""


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
