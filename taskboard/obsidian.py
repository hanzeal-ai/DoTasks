from __future__ import annotations

import hashlib
import os
import re
import threading
from pathlib import Path
from typing import Any

from .decision_client import DecisionClient
from core.search import search_tokens


def _safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "-", value).strip()
    return value[:100] or "untitled"



class ObsidianAdapter:
    def __init__(self, project_home: Path, decision_client: DecisionClient | None = None):
        self.decisions = decision_client or DecisionClient(project_home)
        configured = os.environ.get("DOTASKS_OBSIDIAN_VAULT")
        self.vault = (
            Path(configured).expanduser().resolve()
            if configured
            else project_home / "data" / "obsidian-vault"
        )
        self.root = self.vault / "DoTasks"
        self._document_cache: dict[str, tuple[int, int, str]] = {}
        self._cache_lock = threading.Lock()

    @staticmethod
    def _project_key(project: str) -> str:
        normalized = str(Path(str(project or "")).expanduser().resolve()) if project else "unknown"
        name = _safe_name(Path(normalized).name or "project")
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:10]
        return f"{name}-{digest}"

    def project_root(self, project: str) -> Path:
        return self.root / "Projects" / self._project_key(project)

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(os.environ.get("DOTASKS_OBSIDIAN_VAULT")),
            "vault": str(self.vault),
            "exists": self.vault.exists(),
        }

    def sync_task(
        self,
        task: dict[str, Any],
        relations: list[dict[str, Any]],
        delivery: dict[str, str] | None = None,
    ) -> str:
        project = str(task.get("project") or "")
        project_id = self._project_key(project)
        project_root = self.project_root(project)
        folder = project_root / "Tasks"
        folder.mkdir(parents=True, exist_ok=True)
        project_note = project_root / "Project.md"
        if not project_note.exists():
            project_note.write_text(
                f"---\nproject_id: {project_id}\nproject: {project}\n---\n\n# {Path(project).name or project_id}\n",
                encoding="utf-8",
            )
        path = folder / f"{task['id']} {_safe_name(task['title'])}.md"
        relation_lines = []
        for item in relations:
            if item.get("source_task_id") == task["id"]:
                relation_lines.append(f"- {item['relation_type']}：[[{item['target_task_id']}]]")
            else:
                relation_lines.append(f"- 被 [[{item['source_task_id']}]] {item['relation_type']}")
        relation_text = "\n".join(relation_lines) or "- 无"
        acceptance = "\n".join(
            f"- [ ] {item}" for item in task.get("acceptance_criteria", [])
        ) or "- 待补充"
        scope = "\n".join(f"- {item}" for item in task.get("scope", [])) or "- 待补充"
        implementation = task.get("implementation_contract") or {}
        target_lines = []
        for target in implementation.get("targets") or []:
            if not isinstance(target, dict):
                continue
            file = str(target.get("file") or "").strip()
            if not file:
                continue
            target_lines.append(
                f"- `{file}` ({str(target.get('mode') or 'modify')})"
            )
            for target_task in target.get("tasks") or []:
                if isinstance(target_task, dict) and str(target_task.get("action") or "").strip():
                    symbol = str(target_task.get("symbol") or "").strip()
                    target_lines.append(
                        f"  - {f'`{symbol}`：' if symbol else ''}{target_task['action']}"
                    )
        target_text = "\n".join(target_lines) or "- 无"
        scheduling = task.get("dependency_analysis") or {}
        history_edge_text = "\n".join(
            f"- [[{edge['from']}]] --{edge['type']}--> [[{edge['to']}]]"
            for edge in scheduling.get("history_edges") or []
            if isinstance(edge, dict)
            and str(edge.get("from") or "").strip()
            and str(edge.get("to") or "").strip()
            and str(edge.get("type") or "").strip()
        ) or "- 无"
        content = f"""---
task_id: {task['id']}
requirement_id: {task.get('requirement_id') or ''}
project_id: {project_id}
project: {project}
status: {task['status']}
priority: {task['priority']}
codex_thread_id: {task.get('codex_thread_id') or ''}
modules: {task.get('modules', [])}
depends_tasks: {scheduling.get('depends_tasks', [])}
conflicts_tasks: {scheduling.get('conflicts_tasks', [])}
history_tasks: {scheduling.get('history_tasks', [])}
---

# {task['title']}

## 目标

{task.get('goal') or ''}

## 范围

{scope}

## 精确执行目标

{target_text}

## 验收标准

{acceptance}

## 任务关系

{relation_text}

## 历史路径

{history_edge_text}

## 执行摘要

{(delivery or {}).get('summary') or '待任务完成后补充。'}

## 验证结果

{(delivery or {}).get('verification') or '待验证。'}
"""
        path.write_text(content, encoding="utf-8")
        return str(path)

    def sync_experience(self, experience: dict[str, Any]) -> str:
        folder = self.root / "Experiences"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{experience['id']} {_safe_name(experience['title'])}.md"
        sources = "\n".join(f"- [[{task_id}]]" for task_id in experience.get("source_tasks", [])) or "- 无"
        content = f"""---
experience_id: {experience['id']}
project: {experience.get('project') or ''}
status: {experience.get('status') or 'active'}
modules: {experience.get('modules', [])}
keywords: {experience.get('keywords', [])}
---

# {experience['title']}

## 项目经验

{experience['content']}

## 来源任务

{sources}
"""
        path.write_text(content, encoding="utf-8")
        return str(path)

    def search(self, query: str, limit: int = 8, project: str = "") -> list[dict[str, Any]]:
        if not self.root.exists() or not query.strip():
            return []
        terms = search_tokens(query)
        results: list[dict[str, Any]] = []
        roots = None
        if project:
            roots = [self.project_root(project), self.root / "Tasks"]
        normalized_project = str(Path(project).expanduser().resolve()) if project else ""
        for path, content in self._documents(roots):
            note_project_match = re.search(r"(?m)^project:\s*(.+?)\s*$", content)
            note_project = str(note_project_match.group(1)).strip() if note_project_match else ""
            if normalized_project and note_project:
                try:
                    if str(Path(note_project).expanduser().resolve()) != normalized_project:
                        continue
                except OSError:
                    continue
            haystack = f"{path.name}\n{content}".lower()
            score = sum((len(term) - 1) * haystack.count(term) for term in terms)
            if score:
                results.append(
                    {
                        "path": str(path),
                        "title": path.stem,
                        "project": note_project,
                        "score": score,
                        "summary": " ".join(content.split())[:280],
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["title"]))
        return results[:limit]

    def _documents(self, roots: list[Path] | None = None) -> list[tuple[Path, str]]:
        """Reuse Markdown text until its mtime or size changes."""
        documents: list[tuple[Path, str]] = []
        candidates: list[Path] = []
        for root in roots or [self.root]:
            if root.exists():
                candidates.extend(root.rglob("*.md"))
        with self._cache_lock:
            for path in dict.fromkeys(candidates):
                key = str(path)
                try:
                    stat = path.stat()
                    cached = self._document_cache.get(key)
                    if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
                        content = cached[2]
                    else:
                        content = path.read_text(encoding="utf-8")
                        self._document_cache[key] = (stat.st_mtime_ns, stat.st_size, content)
                except OSError:
                    continue
                documents.append((path, content))
        return documents

    def search_task_dependencies(self, title: str, goal: str, modules: list[str] | None = None,
                                 located_symbols: list[str] | None = None, limit: int = 8,
                                 project: str = "", located_files: list[str] | None = None,
                                 actions: list[str] | None = None) -> list[dict[str, Any]]:
        """Return direct matches plus their project-scoped historical ancestors."""
        query = " ".join([
            title, goal, *(modules or []), *(located_files or []),
            *(located_symbols or []), *(actions or []),
        ])
        if not self.root.exists() or not query.strip():
            return []
        terms = search_tokens(query)
        roots = [self.project_root(project), self.root / "Tasks"] if project else [self.root]
        normalized_project = str(Path(project).expanduser().resolve()) if project else ""
        notes: dict[str, dict[str, Any]] = {}
        for path, content in self._documents(roots):
            project_match = re.search(r"(?m)^project:\s*(.+?)\s*$", content)
            note_project = str(project_match.group(1)).strip() if project_match else ""
            if normalized_project and note_project:
                try:
                    if str(Path(note_project).expanduser().resolve()) != normalized_project:
                        continue
                except OSError:
                    continue
            match = re.search(r"((?:TASK|BUG)-\d+)", path.stem)
            if not match:
                continue
            task_id = match.group(1)
            history_edges: list[dict[str, str]] = []
            lineage_types = {"changed_from", "continues_from", "defect_of", "replaces", "split_from"}
            for source, relation_type, target in re.findall(
                r"(?m)^-\s+\[\[((?:TASK|BUG)-\d+)\]\]\s+--([a-z_]+)-->\s+\[\[((?:TASK|BUG)-\d+)\]\]\s*$",
                content,
            ):
                if relation_type in lineage_types:
                    history_edges.append({"from": source, "to": target, "type": relation_type})
            for relation_type, related_id in re.findall(
                r"(?m)^-\s+([a-z_]+)：\[\[((?:TASK|BUG)-\d+)\]\]\s*$", content,
            ):
                if task_id and relation_type in lineage_types:
                    history_edges.append({"from": related_id, "to": task_id, "type": relation_type})
            for related_id, relation_type in re.findall(
                r"(?m)^-\s+被\s+\[\[((?:TASK|BUG)-\d+)\]\]\s+([a-z_]+)\s*$", content,
            ):
                if task_id and relation_type in lineage_types:
                    history_edges.append({"from": task_id, "to": related_id, "type": relation_type})
            deduplicated_edges = list({
                (edge["from"], edge["to"], edge["type"]): edge
                for edge in history_edges
            }.values())
            haystack = f"{path.name}\n{content}".lower()
            score = sum((len(term) - 1) * haystack.count(term) for term in terms)
            notes[task_id] = {
                "task_id": task_id,
                "status": "historical",
                "project": note_project or project,
                "evidence_path": str(path),
                "summary": " ".join(content.split())[:280],
                "score": score,
                "history_edges": deduplicated_edges,
            }

        direct = sorted(
            (item for item in notes.values() if int(item["score"]) > 0),
            key=lambda item: (-int(item["score"]), item["task_id"]),
        )[:limit]
        # Advice reorders only this bounded set. Ancestors, membership, lexical
        # scores and the authoritative dependency classifier remain unchanged.
        direct = self.decisions.rank_history(query, direct)
        reverse: dict[str, set[str]] = {}
        for item in notes.values():
            for edge in item["history_edges"]:
                reverse.setdefault(edge["to"], set()).add(edge["from"])
        selected = {item["task_id"] for item in direct}
        pending = list(selected)
        while pending and len(selected) < 256:
            current = pending.pop(0)
            for ancestor in sorted(reverse.get(current, set())):
                if ancestor in notes and ancestor not in selected:
                    selected.add(ancestor)
                    pending.append(ancestor)
        ancestors = [
            {**notes[task_id], "lineage_expansion": True}
            for task_id in sorted(selected - {item["task_id"] for item in direct})
        ]
        return [*direct, *ancestors]
