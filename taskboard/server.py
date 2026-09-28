from __future__ import annotations

import argparse
import base64
import json
import re
import time
from http import HTTPStatus
from http.server import ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from core.service import TaskboardService
from core.service.visuals import VISUAL_UPLOAD_BODY_LIMIT
from core.workflow import workflow_metadata

from .codex_projects import discover_codex_projects
from .config import LOCAL_MODE, ServerConfig
from .http_base import BaseDoTasksHandler
from .version import VERSION
from .web_auth import WebSessions

class TaskboardHandler(BaseDoTasksHandler):
    service: TaskboardService

    def _codex_projects(self) -> list[dict[str, str]]:
        return discover_codex_projects()

    def _dispatcher_status(self) -> dict[str, Any]:
        return {
            "control_plane": "local",
            "enabled": self.service.dispatcher_enabled(),
            "execution_mode": "codex_cli_app_server",
            "agent_configured": (self.service.data_home / "cloud-agent.json").is_file(),
            "running": None,
        }

    def _serve_event_stream(self) -> None:
        try:
            cursor = int(self.headers.get("Last-Event-ID", "0") or "0")
        except ValueError:
            cursor = 0
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.send_header("X-Accel-Buffering", "no")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(b"retry: 2000\n\n")
        self.wfile.flush()
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                self._require_authentication()
            except ValueError:
                break
            latest = self.service.latest_event_id()
            if latest > cursor:
                cursor = latest
                payload = json.dumps({"event_id": latest}, ensure_ascii=False)
                self.wfile.write(
                    f"id: {latest}\nevent: board_changed\ndata: {payload}\n\n".encode(
                        "utf-8"
                    )
                )
                self.wfile.flush()
            time.sleep(0.5)
        self.wfile.write(b": reconnect\n\n")
        self.wfile.flush()
        self.close_connection = True

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if self._handle_web_auth("GET", parsed.path):
                return
            if parsed.path.startswith("/api/"):
                self._validate_api_request()
            if parsed.path == "/api/health":
                self._json(
                    HTTPStatus.OK,
                    {
                        "ok": True,
                        "version": VERSION,
                        "dispatcher": self._dispatcher_status(),
                    },
                )
            elif parsed.path == "/api/visual-artifacts/content":
                artifact_id = parse_qs(parsed.query).get("artifact_id", [""])[0]
                # Only managed raster images can be served inline, within the
                # same authenticated/tenant service used by the upload route.
                if not re.fullmatch(r"artifact://visuals/[0-9a-f]{64}\.(png|jpg|gif|webp)", artifact_id):
                    raise ValueError("Only managed image artifacts can be previewed")
                artifact = self.service.read_visual_artifact(artifact_id)
                content = base64.b64decode(artifact["content_base64"], validate=True)
                content_type, _ = TaskboardService._detect_image(content)
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(content)))
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Security-Policy", "default-src 'none'")
                self.end_headers()
                self.wfile.write(content)
            elif parsed.path == "/api/workflow":
                self._json(HTTPStatus.OK, workflow_metadata())
            elif parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, self.service.task_settings())
            elif parsed.path == "/api/codex/projects":
                self._json(HTTPStatus.OK, {"projects": self._codex_projects()})
            elif parsed.path == "/api/events/stream":
                self._serve_event_stream()
            elif parsed.path == "/api/board":
                board = self.service.board()
                board["dispatcher"] = self._dispatcher_status()
                self._json(HTTPStatus.OK, board)
            elif parsed.path == "/api/integrations":
                query = parse_qs(parsed.query)
                project = query.get("project", [None])[0]
                self._json(HTTPStatus.OK, self.service.integration_status(project))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)", parsed.path):
                self._json(HTTPStatus.OK, self.service.get_task(match.group(1)))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/context", parsed.path):
                query = parse_qs(parsed.query)
                self._json(
                    HTTPStatus.OK,
                    self.service.build_context(
                        match.group(1), query.get("project_path", [None])[0]
                    ),
                )
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/details", parsed.path):
                self._json(HTTPStatus.OK, self.service.task_details(match.group(1)))
            elif parsed.path.startswith("/api/"):
                self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
            else:
                self._serve_static(parsed.path)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:  # pragma: no cover - exercised by integration behavior
            self._handle_error(exc)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        try:
            if self._handle_web_auth("POST", parsed.path):
                return
            self._validate_api_request()
            if parsed.path == "/api/visual-artifacts":
                payload = self._read_json(VISUAL_UPLOAD_BODY_LIMIT)
                reference = self.service.upload_visual_artifact(payload)
                self._json(HTTPStatus.CREATED, {
                    key: value for key, value in reference.items() if key != "path"
                })
                return
            payload = self._read_json()
            if parsed.path == "/api/settings":
                self._json(HTTPStatus.OK, self.service.update_task_settings(payload))
            elif parsed.path == "/api/task-intakes/enqueue":
                self._json(HTTPStatus.CREATED, self.service.enqueue_task_intake(payload))
            elif parsed.path == "/api/task-intakes/finalize":
                self._json(HTTPStatus.OK, self.service.finalize_task_intake(payload))
            elif match := re.fullmatch(
                r"/api/requirements/([^/]+)/redecompose", parsed.path
            ):
                self._json(
                    HTTPStatus.OK,
                    self.service.redecompose_requirement(match.group(1)),
                )
            elif match := re.fullmatch(
                r"/api/requirements/([^/]+)/delete", parsed.path
            ):
                self._json(
                    HTTPStatus.OK,
                    self.service.delete_requirement(match.group(1)),
                )
            elif parsed.path == "/api/location-analyses":
                self._json(
                    HTTPStatus.CREATED,
                    self.service.prepare_location_analysis(
                        payload,
                        payload.get("stage", "creation"),
                        payload.get("task_id"),
                    ),
                )
            elif match := re.fullmatch(
                r"/api/task-changes/([^/]+)/resolve", parsed.path
            ):
                self._json(
                    HTTPStatus.OK,
                    self.service.resolve_task_change_confirmation(
                        match.group(1),
                        payload["decision"],
                    ),
                )
            elif parsed.path == "/api/integrations/location/report":
                self._json(
                    HTTPStatus.OK,
                    self.service.report_location_status(
                        payload["project"],
                        payload["available"],
                        payload["state"],
                        payload.get("summary", ""),
                        payload["evidence"],
                        payload.get("agent_id", ""),
                    ),
                )
            elif parsed.path == "/api/conversations/bind":
                self._json(
                    HTTPStatus.CREATED,
                    self.service.bind_conversation(
                        payload["task_id"],
                        payload["role"],
                        payload["thread_id"],
                        payload.get("run_id"),
                        payload.get("title", ""),
                    ),
                )
            elif parsed.path == "/api/dispatcher/pause":
                self._json(
                    HTTPStatus.OK,
                    self.service.pause_dispatcher(),
                )
            elif parsed.path == "/api/dispatcher/resume":
                result = (
                    self.service.resume_all_tasks()
                    if payload.get("resume_tasks")
                    else self.service.set_dispatcher_enabled(True)
                )
                self._json(
                    HTTPStatus.OK,
                    {
                        **result,
                        "agent_signal": "pending",
                        "agent_configured": bool(
                            self._dispatcher_status().get("agent_configured")
                        ),
                    },
                )
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/resume", parsed.path):
                self._json(HTTPStatus.OK, self.service.resume_task(match.group(1)))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/delete", parsed.path):
                self._json(HTTPStatus.OK, self.service.delete_task(match.group(1)))
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/transition", parsed.path):
                updates = payload.get("updates", {})
                self._json(
                    HTTPStatus.OK,
                    self.service.transition_task(
                        match.group(1),
                        payload["status"],
                        payload.get("reason", ""),
                        **updates,
                    ),
                )
            elif match := re.fullmatch(r"/api/runs/([^/]+)/delivery", parsed.path):
                self._json(
                    HTTPStatus.OK,
                    self.service.submit_delivery(
                        match.group(1),
                        payload["delivery_summary"],
                        payload["verification_result"],
                        payload["changed_locations"],
                        payload["acceptance_evidence"],
                        payload.get("token_used", 0),
                        workspace_path=payload.get("workspace_path"),
                    ),
                )
            elif match := re.fullmatch(
                r"/api/tasks/([^/]+)/conversation-summary", parsed.path
            ):
                self._json(
                    HTTPStatus.OK,
                    self.service.update_conversation_summary(
                        match.group(1),
                        payload["thread_id"],
                        payload["summary"],
                        payload.get("status", "completed"),
                    ),
                )
            elif match := re.fullmatch(r"/api/tasks/([^/]+)/relations", parsed.path):
                self._json(
                    HTTPStatus.CREATED,
                    self.service.add_relation(
                        match.group(1),
                        payload["target_task_id"],
                        payload["relation_type"],
                        payload.get("description", ""),
                    ),
                )
            else:
                self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def do_PATCH(self) -> None:  # noqa: N802
        try:
            self._validate_api_request()
            self._json(HTTPStatus.NOT_FOUND, {"error": "API route not found"})
        except Exception as exc:
            self._handle_error(exc)

class TaskboardHTTPServer(ThreadingHTTPServer):
    def __init__(self, *args: Any, **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.web_sessions = WebSessions()


def build_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    home: str | None = None,
) -> TaskboardHTTPServer:
    runtime = ServerConfig(
        mode=LOCAL_MODE,
        host=host,
        port=port,
        home=home,
    ).validate()
    service = TaskboardService(str(runtime.data_home) if runtime.data_home else None)
    handler = type(
        "ConfiguredTaskboardHandler",
        (TaskboardHandler,),
        {
            "service": service,
            "static_root": service.package_home / "static",
            "config": runtime,
        },
    )
    server = TaskboardHTTPServer((runtime.host, runtime.port), handler)
    return server


def main() -> None:
    parser = argparse.ArgumentParser(description="Run DoTasks")
    parser.add_argument("--host")
    parser.add_argument("--port", type=int)
    parser.add_argument("--home")
    args = parser.parse_args()
    config = ServerConfig.from_environment(
        mode=LOCAL_MODE,
        host=args.host,
        port=args.port,
        home=args.home,
    )
    server = build_server(config.host, config.port, str(config.data_home) if config.data_home else None)
    print(f"DoTasks: http://{config.host}:{server.server_port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
