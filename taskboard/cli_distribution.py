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
