from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


def _default_data_home() -> Path:
    configured = os.environ.get("DOTASKS_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "DoTasks"


class RemoteToolClient:
    """Authenticated client for the cloud-owned DoTasks service."""

    def __init__(self, cloud_url: str, agent_id: str, agent_token: str):
        self.cloud_url = str(cloud_url or "").rstrip("/")
        self.agent_id = str(agent_id or "").strip()
        self.agent_token = str(agent_token or "")
        parsed = urlparse(self.cloud_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not self.agent_id
            or len(self.agent_token) < 24
        ):
            raise ValueError("Cloud Agent configuration is incomplete")

    @classmethod
    def from_environment(cls) -> "RemoteToolClient | None":
        configured = os.environ.get("DOTASKS_AGENT_CONFIG")
        path = (
            Path(configured).expanduser().resolve()
            if configured
            else _default_data_home() / "cloud-agent.json"
        )
        if not path.is_file():
            return None
        stored = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            str(os.environ.get("DOTASKS_CLOUD_URL") or stored.get("cloud_url") or ""),
            str(os.environ.get("DOTASKS_AGENT_ID") or stored.get("agent_id") or "default"),
            str(os.environ.get("DOTASKS_AGENT_TOKEN") or stored.get("agent_token") or ""),
        )

    def call(self, name: str, arguments: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            self.cloud_url + "/_agent/v1/tools/call",
            data=json.dumps(
                {
                    "agent_id": self.agent_id,
                    "name": str(name or ""),
                    "arguments": arguments,
                },
                ensure_ascii=False,
            ).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.agent_token}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=70) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read()
            try:
                detail = json.loads(body or b"{}").get("error")
            except (json.JSONDecodeError, AttributeError):
                detail = body.decode("utf-8", errors="replace")
            raise RuntimeError(
                str(detail or f"Cloud service returned HTTP {exc.code}")
            ) from exc
        result = json.loads(body or b"{}")
        if not isinstance(result, dict) or "result" not in result:
            raise RuntimeError("Cloud service returned an invalid tool response")
        return result["result"]


class RemoteTaskboardService:
    """Taskboard operations used by LocalCodexExecutor against cloud state."""

    _MISSING_LIFECYCLE_CALLBACK_REASON = "原生 Codex 任务已结束但未提交生命周期回调"

    def __init__(self, client: RemoteToolClient):
        self.client = client

    def claim_schedule_cycle(
        self,
        worker_id: str,
        project: str | None = None,
        lease_seconds: int = 1800,
        *,
        force: bool = False,
    ) -> dict[str, Any]:
        return self.client.call(
            "claim_schedule_cycle",
            {
                "worker_id": worker_id,
                "project": project,
                "lease_seconds": lease_seconds,
                "force": force,
            },
        )

    def complete_schedule_cycle(
        self, worker_id: str, generation: int
    ) -> dict[str, Any]:
        return self.client.call(
            "complete_schedule_cycle",
            {"worker_id": worker_id, "generation": generation},
        )

    def bind_native_dispatch(
        self,
        run_id: str,
        thread_id: str,
        host_id: str = "",
        codex_project_id: str = "",
        *,
        resume_fallback_reason: str = "",
        dispatch_attempt_id: str,
    ) -> dict[str, Any]:
        return self.client.call(
            "bind_native_dispatch",
            {
                "run_id": run_id,
                "thread_id": thread_id,
                "host_id": host_id,
                "codex_project_id": codex_project_id,
                "resume_fallback_reason": resume_fallback_reason,
                "dispatch_attempt_id": dispatch_attempt_id,
            },
        )

    def get_native_dispatch(self, run_id: str) -> dict[str, Any]:
        return self.client.call("get_dispatch_status", {"run_id": run_id})

    def fail_native_dispatch(self, run_id: str, reason: str) -> dict[str, Any]:
        return self.client.call(
            "report_dispatch_failed", {"run_id": run_id, "reason": reason}
        )

    def renew_native_dispatch(
        self, run_id: str, lease_seconds: int = 1800
    ) -> dict[str, Any]:
        return self.client.call(
            "renew_dispatch_lease",
            {"run_id": run_id, "lease_seconds": lease_seconds},
        )
