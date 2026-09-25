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
    if bundled.is_file():
        return str(bundled)
    brew_python = os.environ.get('DOTASKS_BREW_PYTHON')
    if brew_python:
        executable = Path(brew_python)
        if not executable.is_absolute() or executable.resolve() != Path(sys.executable).resolve():
            raise RuntimeError('Homebrew Python 与当前解释器不一致。')
        return str(executable)
    return sys.executable


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
        installed = service.validate_installation()
        brew = homebrew_runtime()
        from .runtime_paths import default_data_home
        managed = default_data_home() / 'cli/current'
        expected = brew or managed
        if installed != expected:
            raise RuntimeError('后台服务属于另一种安装。先用原 CLI 执行 stop，再用新 CLI 的 install 命令迁移；账号与任务保留。')
        if brew is None and installed.resolve() != root:
            from .cli_install import validate_runtime
            if validate_runtime(installed)['version'] != validate_runtime(root)['version']:
                raise RuntimeError('已有其他版本。请用已安装的 dotasks update 升级，或停止服务后运行本包的 install。')
        return
    if any(service.plist(name).exists() for name in LABELS):
        raise RuntimeError('后台配置不完整，请停止服务后运行 dotasks install 修复。')
    print('安装本机后台服务……', flush=True)
    install(homebrew_runtime() or root, homebrew=homebrew_runtime() is not None)
