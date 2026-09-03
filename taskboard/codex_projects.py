from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def default_codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else Path.home() / ".codex"
    )


def sanitize_codex_projects(value: Any) -> list[dict[str, str]]:
    """Return the bounded public shape accepted from a local Codex agent."""
    if not isinstance(value, list):
        return []
    projects: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in value[:200]:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if not path or not Path(path).is_absolute() or path in seen:
            continue
        seen.add(path)
        projects.append(
            {
                "id": str(item.get("id") or "").strip(),
                "name": str(item.get("name") or Path(path).name).strip(),
                "path": path,
            }
        )
    return projects


def discover_codex_projects(codex_home: str | Path | None = None) -> list[dict[str, str]]:
    """Read existing local project roots from Codex Desktop's state file."""
    home = (
        Path(codex_home).expanduser().resolve()
        if codex_home is not None
        else default_codex_home()
    )
    try:
        state = json.loads((home / ".codex-global-state.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(state, dict):
        return []
    stored = state.get("local-projects")
    if not isinstance(stored, dict):
        return []

    project_order = state.get("project-order")
    if not isinstance(project_order, list):
        project_order = []
    ordered_ids = [
        str(project_id)
        for project_id in project_order
        if isinstance(project_id, str) and project_id in stored
    ]

    def project_name(project_id: str) -> str:
        project = stored.get(project_id)
        return str(project.get("name") or project_id) if isinstance(project, dict) else project_id

    ordered_ids.extend(
        sorted(
            (str(project_id) for project_id in stored if project_id not in ordered_ids),
            key=lambda project_id: project_name(project_id).casefold(),
        )
    )

    discovered: list[dict[str, str]] = []
    for project_id in ordered_ids:
        project = stored.get(project_id)
        if not isinstance(project, dict):
            continue
        roots = project.get("rootPaths")
        if not isinstance(roots, list):
            continue
        name = str(project.get("name") or "").strip()
        for index, value in enumerate(roots):
            raw_path = str(value or "").strip()
            path = Path(raw_path).expanduser()
            if not raw_path or not path.is_absolute() or not path.is_dir():
                continue
            discovered.append(
                {
                    "id": project_id if len(roots) == 1 else f"{project_id}:{index}",
                    "name": name or path.name,
                    "path": str(path.resolve()),
                }
            )
    return sanitize_codex_projects(discovered)
