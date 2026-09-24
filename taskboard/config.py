from __future__ import annotations

import ipaddress
import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse


LOCAL_MODE = "local"
CLOUD_MODE = "cloud"
SUPPORTED_MODES = frozenset({LOCAL_MODE, CLOUD_MODE})


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _split_csv(value: str | None) -> tuple[str, ...]:
    return tuple(item.strip() for item in str(value or "").split(",") if item.strip())


@dataclass(frozen=True)
class ServerConfig:
    """Runtime-only settings for local and deployable cloud HTTP modes."""

    mode: str = LOCAL_MODE
    host: str = "127.0.0.1"
    port: int = 8765
    home: str | None = None
    public_url: str = ""
    http_user: str = ""
    http_password: str = ""
    account_mode: str = "single"
    extra_trusted_hosts: tuple[str, ...] = ()
    extra_trusted_origins: tuple[str, ...] = ()

    def validate(self) -> "ServerConfig":
        if self.mode not in SUPPORTED_MODES:
            raise ValueError("DoTasks mode must be local or cloud")
        if not 0 <= int(self.port) <= 65535:
            raise ValueError("DoTasks port must be between 0 and 65535")
        if self.mode == LOCAL_MODE:
            if not _is_loopback(self.host):
                raise ValueError("DoTasks local mode only supports loopback HTTP binding")
            return self

        if self.account_mode not in {"single", "multi"}:
            raise ValueError("DOTASKS_ACCOUNT_MODE must be single or multi")
        if self.account_mode == "single" and (not self.http_user or not self.http_password):
            raise ValueError(
                "DoTasks cloud mode requires DOTASKS_HTTP_USER and "
                "DOTASKS_HTTP_PASSWORD"
            )
        if not self.public_url:
            raise ValueError("DoTasks cloud mode requires DOTASKS_PUBLIC_URL")
        parsed = urlparse(self.public_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("DOTASKS_PUBLIC_URL must be an absolute http(s) URL")
        if parsed.path not in {"", "/"} or parsed.params or parsed.query or parsed.fragment:
            raise ValueError("DOTASKS_PUBLIC_URL must not contain a path, query, or fragment")
        return self

    @property
    def data_home(self) -> Path | None:
        return Path(self.home).expanduser().resolve() if self.home else None

    def trusted_hosts(self, actual_port: int) -> set[str]:
        if self.mode == LOCAL_MODE:
            return {
                "localhost",
                f"localhost:{actual_port}",
                "127.0.0.1",
                f"127.0.0.1:{actual_port}",
                "[::1]",
                f"[::1]:{actual_port}",
            }
        parsed = urlparse(self.public_url)
        hosts = {str(parsed.netloc).lower(), *[item.lower() for item in self.extra_trusted_hosts]}
        return {item for item in hosts if item}

    def trusted_origins(self, actual_port: int) -> set[str]:
        local = {
            "app://-",
            f"http://127.0.0.1:{actual_port}",
            f"http://localhost:{actual_port}",
            f"http://[::1]:{actual_port}",
        }
        if self.mode == LOCAL_MODE:
            return local
        return {
            self.public_url.rstrip("/"),
            *self.extra_trusted_origins,
        }

    @classmethod
    def from_environment(
        cls,
        *,
        mode: str | None = None,
        host: str | None = None,
        port: int | None = None,
        home: str | None = None,
        public_url: str | None = None,
    ) -> "ServerConfig":
        resolved_mode = str(mode or os.environ.get("DOTASKS_MODE") or LOCAL_MODE).strip().lower()
        default_host = "0.0.0.0" if resolved_mode == CLOUD_MODE else "127.0.0.1"
        resolved_port = port if port is not None else int(os.environ.get("DOTASKS_PORT") or 8765)
        config = cls(
            mode=resolved_mode,
            host=str(host or os.environ.get("DOTASKS_HOST") or default_host).strip(),
            port=resolved_port,
            home=home or os.environ.get("DOTASKS_HOME"),
            public_url=str(public_url or os.environ.get("DOTASKS_PUBLIC_URL") or "").strip(),
            http_user=str(os.environ.get("DOTASKS_HTTP_USER") or "").strip(),
            http_password=str(os.environ.get("DOTASKS_HTTP_PASSWORD") or ""),
            account_mode=str(os.environ.get("DOTASKS_ACCOUNT_MODE") or "single"),
            extra_trusted_hosts=_split_csv(os.environ.get("DOTASKS_TRUSTED_HOSTS")),
            extra_trusted_origins=_split_csv(os.environ.get("DOTASKS_TRUSTED_ORIGINS")),
        )
        return config.validate()
