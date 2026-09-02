from __future__ import annotations

import base64
import binascii
import hmac
from http import HTTPStatus
from typing import Mapping

from .config import CLOUD_MODE, ServerConfig


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

    def __init__(self, config: ServerConfig, actual_port: int):
        self.config = config
        self.actual_port = actual_port

    @property
    def trusted_origins(self) -> set[str]:
        return self.config.trusted_origins(self.actual_port)

    def require_authentication(self, headers: Mapping[str, str]) -> None:
        if self.config.mode != CLOUD_MODE:
            return
        header = headers.get("Authorization", "")
        challenge = {"WWW-Authenticate": 'Basic realm="DoTasks", charset="UTF-8"'}
        if not header.startswith("Basic "):
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
        valid = hmac.compare_digest(username, self.config.http_user)
        valid = hmac.compare_digest(password, self.config.http_password) and valid
        if not valid:
            raise HTTPRequestError(
                HTTPStatus.UNAUTHORIZED,
                "Invalid authentication credentials",
                challenge,
            )

    def validate_api_request(self, headers: Mapping[str, str]) -> None:
        self.require_authentication(headers)
        host_header = headers.get("Host", "").lower()
        if host_header not in self.config.trusted_hosts(self.actual_port):
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted Host header")
        origin = headers.get("Origin")
        if origin and origin not in self.trusted_origins:
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, "Untrusted request origin")
