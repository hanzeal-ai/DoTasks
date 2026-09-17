from __future__ import annotations

import base64
import binascii
import hmac
from http import HTTPStatus
from typing import Mapping

from .config import CLOUD_MODE, ServerConfig
from .web_auth import WebSessions, has_session_cookie, session_token


class HTTPRequestError(ValueError):
    def __init__(
        self,
        status: HTTPStatus,
        message: str,
        headers: dict[str, str] | None = None,
    ):
        super().__init__(message)
        self.status = status
        self.headers = headers or {}


class RequestSecurityPolicy:
    """Validate one HTTP surface without coupling it to route handlers."""

    def __init__(self, config: ServerConfig, actual_port: int, sessions: WebSessions | None = None):
        self.config = config
        self.actual_port = actual_port
        self.sessions = sessions

    @property
    def trusted_origins(self) -> set[str]:
        return self.config.trusted_origins(self.actual_port)

    def require_authentication(self, headers: Mapping[str, str]) -> None:
        if self.config.mode != CLOUD_MODE:
            return
        if self.sessions and self.sessions.valid(session_token(headers.get("Cookie", ""))):
            return
        header = headers.get("Authorization", "")
        challenge = {}
        # Browser requests must use revocable sessions, even if legacy Basic credentials are cached.
        if has_session_cookie(headers.get("Cookie", "")) or headers.get("Sec-Fetch-Mode") or not header.startswith("Basic "):
            raise HTTPRequestError(
                HTTPStatus.UNAUTHORIZED, "Authentication required", challenge
            )
        try:
            decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
            username, password = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError) as exc:
            raise HTTPRequestError(
                HTTPStatus.UNAUTHORIZED,
                "Invalid authentication credentials",
                challenge,
            ) from exc
        valid = self.valid_credentials(username, password)
        if not valid:
            raise HTTPRequestError(
                HTTPStatus.UNAUTHORIZED,
                "Invalid authentication credentials",
                challenge,
            )

    def valid_credentials(self, username: str, password: str) -> bool:
        valid = hmac.compare_digest(username.encode("utf-8"), self.config.http_user.encode("utf-8"))
        return hmac.compare_digest(password.encode("utf-8"), self.config.http_password.encode("utf-8")) and valid

    def validate_api_request(self, headers: Mapping[str, str]) -> None:
        self.require_authentication(headers)
        self.validate_origin(headers)

    def validate_origin(self, headers: Mapping[str, str]) -> None:
        host_header = headers.get("Host", "").lower()
        if host_header not in self.config.trusted_hosts(self.actual_port):
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted Host header")
        origin = headers.get("Origin")
        if origin and origin not in self.trusted_origins:
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted request origin")
