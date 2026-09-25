"""Executable and ownership rules for downloadable and Homebrew CLI runtimes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import platform
import sys


def runtime_root() -> Path:
    return Path(__file__).resolve().parents[1]


def runtime_python(runtime: Path) -> str:
    bundled = runtime / 'python/bin/python3'
    return str(bundled) if bundled.is_file() else sys.executable


def platform_key() -> str:
    machine = {'arm64': 'arm64', 'aarch64': 'arm64', 'x86_64': 'x86_64'}.get(platform.machine())
    if sys.platform != 'darwin' or machine is None:
        raise RuntimeError('独立 CLI 支持 macOS Apple Silicon 和 Intel。')
    return 'macos-' + machine


def homebrew_runtime() -> Path | None:
    value = os.environ.get('DOTASKS_BREW_RUNTIME')
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute() or path.resolve() != runtime_root():
        raise RuntimeError('Homebrew 运行路径与当前程序不一致。')
    return path


def prepare_initialization() -> None:
    """Register services on first run; never replace a running installation."""
    from .cli_install import install
    from .cli_service import BackgroundService, LABELS
    root = runtime_root()
    if not (root / 'release.json').is_file():
        return  # Source/developer entry points require the explicit installer.
    service = BackgroundService()
    if all(service.plist(name).is_file() for name in LABELS):
        service.validate_installation()
        return
    print('安装本机后台服务……', flush=True)
    install(homebrew_runtime() or root, homebrew=homebrew_runtime() is not None)
