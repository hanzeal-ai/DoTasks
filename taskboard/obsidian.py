from __future__ import annotations

import os
import re
import threading
from pathlib import Path
from typing import Any


def _safe_name(value: str) -> str:
    value = re.sub(r"[\\/:*?\"<>|]", "-", value).strip()
    return value[:100] or "untitled"


def _search_tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for raw in re.findall(r"[A-Za-z0-9_]{2,}|[\u4e00-\u9fff]+", str(value or "").lower()):
        tokens.add(raw)
        if re.fullmatch(r"[\u4e00-\u9fff]+", raw) and len(raw) > 2:
            for size in (2, 3, 4):
                if len(raw) >= size:
                    tokens.update(raw[index:index + size] for index in range(len(raw) - size + 1))
    return tokens


class ObsidianAdapter:
    def __init__(self, project_home: Path):
        configured = os.environ.get("CODEX_TASKBOARD_OBSIDIAN_VAULT")
        self.vault = (
            Path(configured).expanduser().resolve()
            if configured
            else project_home / "data" / "obsidian-vault"
        )
        self.root = self.vault / "Codex Taskboard"
        self._document_cache: dict[str, tuple[int, int, str]] = {}
        self._cache_lock = threading.Lock()

    def status(self) -> dict[str, Any]:
        return {
            "configured": bool(os.environ.get("CODEX_TASKBOARD_OBSIDIAN_VAULT")),
            "vault": str(self.vault),
            "exists": self.vault.exists(),
        }

    def sync_task(
        self,
        task: dict[str, Any],
        relations: list[dict[str, Any]],
        delivery: dict[str, str] | None = None,
    ) -> str:
        folder = self.root / "Tasks"
        folder.mkdir(parents=True, exist_ok=True)
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
        content = f"""---
task_id: {task['id']}
requirement_id: {task.get('requirement_id') or ''}
project: {task.get('project') or ''}
status: {task['status']}
priority: {task['priority']}
codex_thread_id: {task.get('codex_thread_id') or ''}
modules: {task.get('modules', [])}
---

# {task['title']}

## 目标

{task.get('goal') or ''}

## 范围

{scope}

## 验收标准

{acceptance}

## 任务关系

{relation_text}

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

    def search(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not self.root.exists() or not query.strip():
            return []
        terms = _search_tokens(query)
        results: list[dict[str, Any]] = []
        for path, content in self._documents():
            haystack = f"{path.name}\n{content}".lower()
            score = sum((len(term) - 1) * haystack.count(term) for term in terms)
            if score:
                results.append(
                    {
                        "path": str(path),
                        "title": path.stem,
                        "score": score,
                        "summary": " ".join(content.split())[:280],
                    }
                )
        results.sort(key=lambda item: (-item["score"], item["title"]))
        return results[:limit]

    def _documents(self) -> list[tuple[Path, str]]:
        """Reuse Markdown text until its mtime or size changes."""
        documents: list[tuple[Path, str]] = []
        seen: set[str] = set()
        with self._cache_lock:
            for path in self.root.rglob("*.md"):
                key = str(path)
                seen.add(key)
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
            for key in set(self._document_cache) - seen:
                self._document_cache.pop(key, None)
        return documents

    def search_task_dependencies(self, title: str, goal: str, modules: list[str] | None = None,
                                 located_symbols: list[str] | None = None, limit: int = 8) -> list[dict[str, Any]]:
        """Return structured historical candidates; callers still confirm the relation."""
        query = " ".join([title, goal, *(modules or []), *(located_symbols or [])])
        candidates = []
        for item in self.search(query, limit=limit * 2):
            match = re.search(r"(TASK-\d+)", item.get("title", "") or item.get("path", ""))
            candidates.append({
                "task_id": match.group(1) if match else "",
                "status": "historical",
                "evidence_path": item["path"],
                "summary": item["summary"],
                "score": item["score"],
            })
        return candidates[:limit]
