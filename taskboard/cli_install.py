"""Install a versioned Python runtime and two direct launchd jobs."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import uuid

from .cli_service import BackgroundService, LABELS
from .runtime_paths import default_data_home
from .cli_distribution import runtime_python

PATH_LINE = 'export PATH="$HOME/.local/bin:$PATH"'
MARKER = '# DoTasks CLI'


def validate_runtime(runtime: Path) -> dict:
    release = json.loads((runtime / 'release.json').read_text())
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', str(release.get('version', ''))):
        raise ValueError('Invalid CLI release version')
    files = release.get('files')
    if not isinstance(files, dict) or not all(name in files for name in ('taskboard/cli.py', 'taskboard/cli_service.py', 'taskboard/cli_onboarding.py', 'taskboard/server.py', 'taskboard/agent.py', 'scripts/mcp-server')):
        raise ValueError('CLI release is incomplete')
    if release.get('platform'):
        from .cli_distribution import platform_key
        if release['platform'] != platform_key() or release.get('python') != 'python/bin/python3' or not (runtime / 'python/bin/python3').is_file():
            raise ValueError('CLI 安装包与当前系统架构不匹配。')
    actual = {}
    for path in runtime.rglob('*'):
        if path.is_symlink():
            raise ValueError('CLI runtime must not contain symlinks')
        if path.is_file() and path != runtime / 'release.json':
            actual[path.relative_to(runtime).as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    if actual != files:
        raise ValueError('CLI runtime file checksums do not match its manifest')
    return release


def activate(current: Path, release: Path):
    temporary = current.with_name('.current-' + uuid.uuid4().hex)
    temporary.symlink_to(release)
    try:
        os.replace(temporary, current)
    finally:
        temporary.unlink(missing_ok=True)


def install(runtime: Path, *, configure_path=True, homebrew=False) -> Path:
    if sys.platform != 'darwin' or sys.version_info < (3, 14):
        raise RuntimeError('安装需要 macOS 和 Python 3.14+。')
    if os.environ.get('DOTASKS_HOME') or os.environ.get('DOTASKS_AGENT_CONFIG'):
        raise RuntimeError('标准安装请取消 DOTASKS_HOME 和 DOTASKS_AGENT_CONFIG。')
    stable_runtime = runtime.expanduser().absolute()
    runtime = stable_runtime.resolve()
    if homebrew:
        configure_path = False
    release = validate_runtime(runtime)
    service = BackgroundService()
    if service.state() != 'stopped':
        raise RuntimeError('已有 CLI 服务运行，请使用 dotasks update 升级或先执行 dotasks stop。')
    shell = Path(os.environ.get('SHELL', '/bin/zsh')).name
    if configure_path and shell not in {'zsh', 'bash'}:
        raise RuntimeError('自动 PATH 配置支持 zsh 和 bash。')
    home = Path.home()
    data = default_data_home()
    root = data / 'cli'
    current = stable_runtime if homebrew else root / 'current'
    if not homebrew and current.exists() and not current.is_symlink():
        raise RuntimeError('CLI current 必须为受管理的版本链接。')
    previous = current.resolve() if not homebrew and current.is_symlink() else None
    target = stable_runtime if homebrew else root / 'releases' / release['version']
    launcher = stable_runtime.parent / 'dotasks' if homebrew else home / '.local/bin/dotasks'
    profiles = ([home / '.zshrc'] if shell == 'zsh' else [home / '.bashrc', home / '.bash_profile']) if configure_path else []
    old_launcher = home / '.local/bin/dotasks'
    if homebrew and old_launcher.exists() and (old_launcher.is_symlink() or MARKER not in old_launcher.read_text()):
        raise RuntimeError('~/.local/bin/dotasks 不属于本安装器，请先处理命令冲突。')
    outputs = ([old_launcher] if homebrew and old_launcher.exists() else [] if homebrew else [launcher]) + [service.plist(name) for name in LABELS] + profiles
    for path in outputs:
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise RuntimeError(f'拒绝覆盖非普通文件：{path}')
    if not homebrew and launcher.exists() and MARKER not in launcher.read_text():
        raise RuntimeError('命令目录已有其他 dotasks，拒绝覆盖。')
    snapshots = {p: (p.read_bytes(), p.stat().st_mode & 0o777) if p.exists() else None for p in outputs}
    data.mkdir(parents=True, exist_ok=True)
    if not homebrew:
        target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        if validate_runtime(target) != release:
            raise ValueError('Installed version has conflicting content')
    else:
        with tempfile.TemporaryDirectory(prefix='.install-', dir=root) as temporary:
            staged = Path(temporary) / 'runtime'
            shutil.copytree(runtime, staged)
            validate_runtime(staged)
            subprocess.run([runtime_python(staged), '-B', '-m', 'taskboard.cli', '--help'], cwd=staged, capture_output=True, check=True, timeout=20)
            staged.rename(target)
    try:
        # ZIP extraction does not preserve executable bits; this is the worker callback entrypoint.
        if not homebrew:
            (target / 'scripts/mcp-server').chmod(0o755)
            activate(current, target)
            launcher.parent.mkdir(parents=True, exist_ok=True)
            launcher.write_text(f'#!/bin/sh\n{MARKER}\nset -eu\ncd {shlex.quote(str(current))}\nexec {shlex.quote(runtime_python(current))} -B -m taskboard.cli "$@"\n')
            launcher.chmod(0o755)
        log_home = data / 'logs'
        log_home.mkdir(exist_ok=True)
        environment = {'DOTASKS_HOME': str(data), 'PYTHONUNBUFFERED': '1', 'PYTHONDONTWRITEBYTECODE': '1',
            'PATH': str(home / '.local/bin') + ':/opt/homebrew/bin:/usr/local/bin:' + os.environ.get('PATH', '/usr/bin:/bin')}
        if (current / 'python/bin/python3').is_file() and Path('/etc/ssl/cert.pem').is_file():
            environment['SSL_CERT_FILE'] = os.environ.get('SSL_CERT_FILE', '/etc/ssl/cert.pem')
        for key in ('CODEX_HOME', 'DOTASKS_CODEX_BIN'):
            if os.environ.get(key):
                environment[key] = os.environ[key]
        for name, label in LABELS.items():
            arguments = [runtime_python(current), '-B', '-m', 'taskboard.' + name]
            if name == 'server':
                arguments += ['--home', str(data)]
            path = service.plist(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({'Label': label, 'ProgramArguments': arguments,
                'WorkingDirectory': str(current), 'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 5,
                'EnvironmentVariables': environment,
                'StandardOutPath': str(log_home / f'{name}.out.log'), 'StandardErrorPath': str(log_home / f'{name}.err.log')}))
            path.chmod(0o600)
            service.run('disable', service.target(name))
        if homebrew:
            old_launcher.unlink(missing_ok=True)
        for profile in profiles:
            content = profile.read_text() if profile.exists() else ''
            if PATH_LINE not in content.splitlines():
                with profile.open('a') as stream:
                    stream.write(f'\n{MARKER}\n{PATH_LINE}\n')
    except Exception:
        if not homebrew:
            if previous:
                activate(current, previous)
            else:
                current.unlink(missing_ok=True)
        for path, snapshot in snapshots.items():
            if snapshot is None:
                path.unlink(missing_ok=True)
            else:
                path.write_bytes(snapshot[0])
                path.chmod(snapshot[1])
        raise
    print(f'CLI {release["version"]} 安装完成，无需 Helper App。')
    if configure_path:
        print(f'打开新终端运行 dotasks init；当前终端可运行 {shlex.quote(str(launcher))} init。')
    return launcher


def main():
    parser = argparse.ArgumentParser(description='安装独立 DoTasks CLI')
    parser.add_argument('--runtime', type=Path, required=True)
    args = parser.parse_args()
    try:
        install(args.runtime)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f'安装失败：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
