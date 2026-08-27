from __future__ import annotations

import threading
import re
from pathlib import Path
from typing import Any

from .app_server import AppServerError, CodexAppServerClient, task_thread_start_params
from .helper import TaskboardHelperClient
from core.service import TaskboardService


THREAD_SOURCE_KINDS = ["appServer", "vscode"]
ROLE_STAGE_LABELS = {
    "execution": "开发",
    "rework": "开发",
    "review": "验收",
    "code_review": "Review",
    "acceptance": "验收",
    "bugfix": "开发",
    "source": "需求",
}


def thread_group_name(project: str | Path, task_id: str = "", role: str = "") -> str:
    project_name = Path(project).name or str(project)
    match = re.fullmatch(r"TASK-(\d+)", str(task_id or ""))
    compact_task_id = f"TASK-{int(match.group(1)):03d}" if match else str(task_id or "")
    if not compact_task_id:
        return f"{project_name}-其他会话"
    return compact_task_id


def thread_stage_name(role: str = "", fallback: str = "") -> str:
    return ROLE_STAGE_LABELS.get(str(role or ""), str(fallback or "").strip() or "其他")


class CodexWorkspaceService:
    """Read and continue local Codex threads for the Taskboard workspace UI."""

    def __init__(
        self,
        service: TaskboardService,
        client: CodexAppServerClient | None = None,
        helper: TaskboardHelperClient | None = None,
    ):
        self.service = service
        self.client: CodexAppServerClient | None = client or self._new_client()
        self.helper = helper or TaskboardHelperClient(service.data_home)
        self._request_lock = threading.RLock()

    def _new_client(self) -> CodexAppServerClient:
        return CodexAppServerClient(timeout=30, codex_home=self.service.codex_home)

    @staticmethod
    def _is_recoverable_client_error(error: AppServerError) -> bool:
        message = str(error).casefold()
        return any(marker in message for marker in (
            "connection closed",
            "failed to start",
            "initialize failed",
            "not connected",
        ))

    def stop(self) -> None:
        with self._request_lock:
            if self.client is not None:
                self.client.stop()
                self.client = None

    def _request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        with self._request_lock:
            if self.client is None:
                self.client = self._new_client()
            try:
                return self.client.request(method, params)
            except AppServerError as exc:
                if not self._is_recoverable_client_error(exc):
                    raise
                self.client.stop()
                self.client = self._new_client()
                return self.client.request(method, params)

    @staticmethod
    def _decorate_thread(
        thread: dict[str, Any], conversation: dict[str, Any], project: str | None = None,
    ) -> dict[str, Any]:
        task_id = str(conversation.get("task_id") or "")
        role = str(conversation.get("role") or "")
        thread["taskId"] = task_id
        thread["conversationRole"] = role
        thread["groupName"] = thread_group_name(thread.get("cwd") or project or "项目", task_id, role)
        fallback = thread.get("name") or thread.get("preview") or "未命名会话"
        thread["displayName"] = thread_stage_name(role, fallback) if task_id else str(fallback)
        return thread

    def list_threads(self, project: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "limit": max(1, min(int(limit), 500)),
            "sortKey": "updated_at",
            "sortDirection": "desc",
            "sourceKinds": THREAD_SOURCE_KINDS,
        }
        if project:
            params["cwd"] = self.service._require_project_directory(project)
        result = self._request("thread/list", params)
        threads = [dict(item) for item in result.get("data") or []]
        metadata = self.service.conversation_metadata([
            str(thread.get("id") or "") for thread in threads
        ])
        for thread in threads:
            thread_id = str(thread.get("id") or "")
            conversation = metadata.get(thread_id) or {}
            self._decorate_thread(thread, conversation, project)
        return threads

    def list_projects(self, archived: bool = False) -> list[dict[str, Any]]:
        task_projects = set(self.service.board().get("projects") or [])
        manual_projects = set(self.service.workspace_projects())
        archived_projects = set(self.service.archived_workspace_projects())
        threads = self.list_threads()
        catalog: dict[str, dict[str, Any]] = {}

        def ensure(path_value: Any, source: str) -> dict[str, Any] | None:
            value = str(path_value or "").strip()
            if not value:
                return None
            path = Path(value).expanduser().resolve()
            key = str(path)
            item = catalog.setdefault(key, {
                "path": key,
                "name": path.name or key,
                "exists": path.is_dir(),
                "sources": [],
                "thread_count": 0,
                "task_count": 0,
                "updated_at": 0,
            })
            if source not in item["sources"]:
                item["sources"].append(source)
            return item

        for project in manual_projects:
            ensure(project, "manual")
        for project in task_projects:
            ensure(project, "taskboard")
        for thread in threads:
            item = ensure(thread.get("cwd"), "codex")
            if item is None:
                continue
            item["thread_count"] += 1
            item["updated_at"] = max(item["updated_at"], int(thread.get("updatedAt") or 0))

        for task in self.service.list_tasks():
            item = ensure(task.get("project"), "taskboard")
            if item is not None:
                item["task_count"] += 1

        for project in archived_projects:
            ensure(project, "archived")

        catalog = {
            path: item for path, item in catalog.items()
            if (path in archived_projects) is archived
        }
        for item in catalog.values():
            item["archived"] = archived

        return sorted(
            catalog.values(),
            key=lambda item: (-int(item["updated_at"]), str(item["name"]).casefold()),
        )

    def add_project(self, project: str | None) -> dict[str, Any]:
        path = self.service.add_workspace_project(project)
        project_item = next((item for item in self.list_projects() if item["path"] == path), None)
        if project_item is not None:
            return project_item
        return next(item for item in self.list_projects(archived=True) if item["path"] == path)

    def archive_project(self, project: str | None) -> dict[str, Any]:
        return self.service.archive_workspace_project(project)

    def restore_project(self, project: str | None) -> dict[str, Any]:
        return self.service.restore_workspace_project(project)

    def pick_project(self) -> dict[str, Any] | None:
        """Authorize a project through the signed helper and register it."""
        project = self.helper.authorize_project()
        return self.add_project(project) if project else None

    def read_thread(self, thread_id: str) -> dict[str, Any]:
        value = str(thread_id or "").strip()
        if not value:
            raise ValueError("thread_id is required")
        result = self._request("thread/read", {"threadId": value, "includeTurns": True})
        thread = dict(result.get("thread") or {})
        conversation = self.service.conversation_metadata([value]).get(value) or {}
        return self._decorate_thread(thread, conversation)

    def create_thread(self, project: str | None, title: str = "") -> dict[str, Any]:
        normalized = self.service.add_workspace_project(project)
        params = task_thread_start_params(normalized)
        params["sandbox"] = "workspace-write"
        # User-created workspace chats are not autonomous lifecycle workers;
        # they must not inherit Taskboard MCP auto-approval.
        params["approvalPolicy"] = "never"
        result = self._request("thread/start", params)
        thread = dict(result.get("thread") or {})
        thread_id = str(thread.get("id") or "")
        name = str(title or "").strip()
        if name and thread_id:
            self._request("thread/name/set", {"threadId": thread_id, "name": name})
            thread["name"] = name
        return thread

    def start_turn(self, thread_id: str, text: str) -> dict[str, Any]:
        value = str(text or "").strip()
        if not value:
            raise ValueError("message is required")
        thread = self.read_thread(thread_id)
        status = thread.get("status") or {}
        if status.get("type") == "active":
            raise ValueError("当前会话仍在运行，请等待完成后再发送消息")
        try:
            self._request("thread/resume", {"threadId": thread_id})
        except AppServerError as exc:
            raise ValueError("当前会话无法从 Taskboard app-server 恢复") from exc
        result = self._request("turn/start", {
            "threadId": thread_id,
            "input": [{"type": "text", "text": value}],
        })
        return dict(result.get("turn") or {})
