from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode


class TaskboardHelperError(RuntimeError):
    pass


class TaskboardHelperClient:
    """Request project-scoped macOS authorization from the signed helper app."""

    def __init__(
        self,
        data_home: str | Path,
        helper_app: str | Path | None = None,
        opener: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.data_home = Path(data_home).expanduser().resolve()
        configured = helper_app or os.environ.get("CODEX_TASKBOARD_HELPER_APP")
        self.helper_app = (
            Path(configured).expanduser().resolve()
            if configured
            else self.data_home / "Taskboard Helper.app"
        )
        self.request_home = self.data_home / "helper-requests"
        self._opener = opener
        self._sleep = sleep

    @property
    def available(self) -> bool:
        return (self.helper_app / "Contents" / "MacOS" / "TaskboardHelper").is_file()

    def authorize_project(
        self,
        timeout: float = 300.0,
        poll_interval: float = 0.1,
    ) -> str | None:
        if not self.available:
            raise TaskboardHelperError(
                f"Taskboard Helper is not installed: {self.helper_app}"
            )
        self.request_home.mkdir(parents=True, exist_ok=True)
        request_id = str(uuid.uuid4())
        response_path = self.request_home / f"{request_id}.json"
        url = "codex-taskboard-helper://authorize?" + urlencode({"request": request_id})
        completed = self._opener(
            ["/usr/bin/open", "-g", url],
            capture_output=True,
            text=True,
            check=False,
        )
        if completed.returncode != 0:
            error = str(completed.stderr or completed.stdout or "").strip()
            raise TaskboardHelperError(error or "Unable to open Taskboard Helper")

        deadline = time.monotonic() + max(0.1, float(timeout))
        while time.monotonic() < deadline:
            if response_path.is_file():
                try:
                    payload: Any = json.loads(response_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError) as exc:
                    raise TaskboardHelperError("Invalid Taskboard Helper response") from exc
                finally:
                    response_path.unlink(missing_ok=True)
                if not isinstance(payload, dict):
                    raise TaskboardHelperError("Invalid Taskboard Helper response")
                if payload.get("cancelled"):
                    return None
                if payload.get("error"):
                    raise TaskboardHelperError(str(payload["error"]))
                value = str(payload.get("project") or "").strip()
                path = Path(value).expanduser()
                if not value or not path.is_absolute():
                    raise TaskboardHelperError("Taskboard Helper returned an invalid project path")
                return str(path.resolve())
            self._sleep(max(0.01, float(poll_interval)))
        raise TaskboardHelperError("Timed out waiting for project authorization")
