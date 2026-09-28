from __future__ import annotations

import json
import mimetypes
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .config import CLOUD_MODE, ServerConfig
from .web_auth import session_cookie, session_token
from .http_security import HTTPRequestError, RequestSecurityPolicy


MAX_JSON_BODY_BYTES = 1024 * 1024


class BaseDoTasksHandler(BaseHTTPRequestHandler):
    """Shared HTTP transport behavior for local and cloud-facing adapters."""

    static_root: Path
    config: ServerConfig

    def _security(self) -> RequestSecurityPolicy:
        return RequestSecurityPolicy(self.config, self.server.server_port, self.server.web_sessions)

    def _trusted_origins(self) -> set[str]:
        return self._security().trusted_origins

    def _require_authentication(self) -> None:
        self._security().require_authentication(self.headers)

    def _validate_api_request(self) -> None:
        self._security().validate_api_request(self.headers)

    def _cors_headers(self) -> None:
        origin = self.headers.get("Origin")
        if origin in self._trusted_origins():
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PATCH, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        if self.headers.get("Access-Control-Request-Private-Network") == "true":
            self.send_header("Access-Control-Allow-Private-Network", "true")

    def _json(
        self,
        status: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def _content_length(self, max_bytes: int) -> int:
        raw_length = self.headers.get("Content-Length", "0")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Invalid Content-Length header"
            ) from exc
        if length < 0:
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "Content-Length must be non-negative"
            )
        if length > max_bytes:
            raise HTTPRequestError(
                HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "JSON request body is too large"
            )
        return length


    def _read_json(self, max_bytes: int = MAX_JSON_BODY_BYTES) -> dict[str, Any]:
        length = self._content_length(max_bytes)
        if length == 0:
            return {}
        if self.headers.get_content_type() != "application/json":
            raise HTTPRequestError(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "Content-Type must be application/json",
            )
        body = self.rfile.read(length)
        if len(body) != length:
            raise HTTPRequestError(HTTPStatus.BAD_REQUEST, "Incomplete request body")
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise HTTPRequestError(
                HTTPStatus.BAD_REQUEST, "JSON request body must be an object"
            )
        return payload

    def _handle_web_auth(self, method: str, path: str) -> bool:
        if path not in {"/api/auth/status", "/api/auth/login", "/api/auth/logout"}:
            return False
        security = self._security()
        security.validate_origin(self.headers)
        enabled = self.config.mode == CLOUD_MODE
        token = session_token(self.headers.get("Cookie", ""))
        sessions = self.server.web_sessions
        if method == "GET" and path == "/api/auth/status":
            authenticated = not enabled or sessions.valid(token)
            status = {"enabled": enabled, "authenticated": authenticated}
            if authenticated and enabled:
                status["username"] = self.config.http_user
            self._json(HTTPStatus.OK, status)
            return True
        if method != "POST" or path == "/api/auth/status":
            raise HTTPRequestError(HTTPStatus.METHOD_NOT_ALLOWED, "Method not allowed")
        if self.headers.get("Origin") not in security.trusted_origins:
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted request origin")
        payload = self._read_json(4096)
        if not enabled:
            raise HTTPRequestError(HTTPStatus.BAD_REQUEST, "本地模式无需登录")
        if path == "/api/auth/login":
            if not sessions.allow_login(self.client_address[0], payload.get("username", "")):
                raise HTTPRequestError(HTTPStatus.TOO_MANY_REQUESTS, "登录尝试过于频繁，请稍后重试", {"Retry-After": "60"})
            username, password = payload.get("username"), payload.get("password")
            if not isinstance(username, str) or not isinstance(password, str) or not security.valid_credentials(username, password):
                raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, "账号或密码错误")
            sessions.revoke(token)
            token = sessions.create()
        else:
            sessions.revoke(token)
            # Keep a non-authenticating marker so cached legacy Basic credentials cannot undo logout.
            token = "signed-out"
        self._json(HTTPStatus.OK, {"authenticated": path == "/api/auth/login"}, {
            "Set-Cookie": session_cookie(token, secure=urlparse(self.config.public_url).scheme == "https"),
        })
        return True

    def _serve_static(self, path: str) -> None:
        # The public application shell contains no account data; APIs enforce authentication.
        self._security().validate_origin(self.headers)
        relative = "index.html" if path in {"", "/"} else path.lstrip("/")
        candidate = (self.static_root / relative).resolve()
        if self.static_root not in candidate.parents and candidate != self.static_root:
            self.send_error(HTTPStatus.FORBIDDEN)
            return
        if not candidate.is_file():
            candidate = self.static_root / "index.html"
        body = candidate.read_bytes()
        content_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header(
            "Content-Type",
            f"{content_type}; charset=utf-8"
            if content_type.startswith("text/")
            else content_type,
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self) -> None:  # noqa: N802
        try:
            self._validate_api_request()
            self.send_response(HTTPStatus.NO_CONTENT)
            self._cors_headers()
            self.send_header("Content-Length", "0")
            self.end_headers()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def _handle_error(self, exc: Exception) -> None:
        if isinstance(exc, HTTPRequestError):
            self._json(exc.status, {"error": str(exc)}, exc.headers)
        elif isinstance(exc, KeyError):
            self._json(HTTPStatus.NOT_FOUND, {"error": str(exc)})
        elif isinstance(exc, (ValueError, json.JSONDecodeError)):
            self._json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})
        else:
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(exc)})

    def log_message(self, format: str, *args: Any) -> None:
        print(f"[taskboard] {self.address_string()} - {format % args}")
