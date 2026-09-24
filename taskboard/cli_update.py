"""Download, verify, activate and roll back CLI releases without touching user data."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import tempfile
import time
import urllib.request
from urllib.parse import urljoin, urlparse
import zipfile

from .agent import default_data_home, default_config_path, load_agent_config
from .cli_install import activate, install, validate_runtime
from .cli_service import BackgroundService, LABELS
from .cli_onboarding import DEFAULT_CLOUD_URL

MAX_ARCHIVE = 50 * 1024 * 1024
MAX_EXPANDED = 150 * 1024 * 1024


def download_release(cloud: str, directory: Path) -> tuple[dict, Path]:
    from .cli import NoRedirect, read_json
    if urlparse(cloud).scheme != 'https':
        raise ValueError('升级地址必须使用 HTTPS。')
    manifest = read_json(cloud + '/downloads/cli/latest.json')
    if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', str(manifest.get('version', ''))) or not re.fullmatch(r'[0-9a-f]{64}', str(manifest.get('sha256', ''))):
        raise ValueError('Invalid release manifest')
    size = manifest.get('size')
    if type(size) is not int or not 0 < size <= MAX_ARCHIVE:
        raise ValueError('Invalid release size')
    url = urljoin(cloud + '/', manifest.get('url', ''))
    if urlparse(url).scheme != 'https' or urlparse(url).netloc != urlparse(cloud).netloc or urlparse(url).username:
        raise ValueError('升级包必须来自同一 HTTPS 云端。')
    archive = directory / 'release.zip'
    digest = hashlib.sha256()
    received = 0
    with urllib.request.build_opener(NoRedirect()).open(url, timeout=30) as response, archive.open('wb') as stream:
        while chunk := response.read(1024 * 1024):
            received += len(chunk)
            if received > size:
                raise ValueError('Archive exceeds declared size')
            digest.update(chunk)
            stream.write(chunk)
    if received != size or digest.hexdigest() != manifest['sha256']:
        raise ValueError('升级包校验失败，未修改当前版本。')
    try:
        runtime = extract_release(archive, directory / 'extracted')
    except zipfile.BadZipFile as exc:
        raise ValueError('Invalid release ZIP') from exc
    if validate_runtime(runtime)['version'] != manifest['version']:
        raise ValueError('升级包版本不匹配。')
    return manifest, runtime


def extract_release(archive: Path, destination: Path) -> Path:
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 10000 or sum(entry.file_size for entry in entries) > MAX_EXPANDED:
            raise ValueError('Expanded release is too large')
        seen = set()
        for entry in entries:
            path = PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or not path.parts or path.parts[0] != 'DoTasksCLI' or entry.filename in seen or stat.S_ISLNK(entry.external_attr >> 16):
                raise ValueError('Unsafe release archive member')
            seen.add(entry.filename)
        package.extractall(destination)
    return destination / 'DoTasksCLI/runtime'


def ensure_idle(config):
    from .cli import read_json
    from core.workflow import ACTIVE_RUN_STATUSES
    board = read_json(config.cloud_url + '/_agent/v1/tools/call', {
        'agent_id': config.agent_id, 'name': 'list_board', 'arguments': {},
    }, config.agent_token)['result']
    if any(item.get('active_run_status') in ACTIVE_RUN_STATUSES for item in board['tasks']) or any(item.get('status') == 'decomposing' for item in board['requirements']):
        raise RuntimeError('存在活动任务，请等待任务空闲后再升级。')


def wait_ready(service, config):
    from .cli import read_json
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            if service.state() == 'running' and read_json(config.local_url + '/api/health').get('ok') is True:
                if not (default_data_home() / 'onboarding.json').is_file() or read_json(config.cloud_url + '/_agent/v1/status', token=config.agent_token).get('connected') is True:
                    return
        except (OSError, ValueError, RuntimeError):
            pass
        time.sleep(1)
    raise RuntimeError('新版服务未通过启动检查。')


def apply_update(runtime: Path, service: BackgroundService):
    root = default_data_home() / 'cli'
    current = root / 'current'
    previous = current.resolve()
    state = service.state()
    if state not in {'running', 'stopped'}:
        raise RuntimeError('后台服务状态不完整，请先检查 dotasks status。')
    config = load_agent_config() if state == 'running' else None
    if config:
        ensure_idle(config)
    files = [service.plist(name) for name in LABELS] + [Path.home() / '.local/bin/dotasks']
    snapshots = {path: (path.read_bytes(), path.stat().st_mode & 0o777) for path in files}
    # Keep the CLI and daemon configuration in one rollback boundary.
    try:
        if config:
            service.stop()
        install(runtime, configure_path=False)
        if config:
            service.start()
            wait_ready(service, config)
    except Exception as failure:
        try:
            service.stop()
            activate(current, previous)
            for path, (content, mode) in snapshots.items():
                path.write_bytes(content)
                path.chmod(mode)
            if config:
                service.start()
                wait_ready(service, config)
        except Exception as recovery:
            raise RuntimeError(f'升级失败且自动恢复未完成：{recovery}；旧版本保留在 {previous}') from failure
        raise RuntimeError(f'升级失败，已恢复旧版本：{failure}') from failure


def update(*, check_only=False):
    service = BackgroundService()
    service.validate_installation()
    root = default_data_home() / 'cli'
    current = root / 'current'
    previous_version = validate_runtime(current)['version']
    cloud = load_agent_config().cloud_url.rstrip('/') if default_config_path().is_file() else DEFAULT_CLOUD_URL
    # A lock prevents simultaneous updates from interleaving rollback pointers.
    import fcntl
    with (root / 'update.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('另一个 CLI 升级正在进行。') from None
        with tempfile.TemporaryDirectory(prefix='.update-', dir=root) as temporary:
            manifest, runtime = download_release(cloud, Path(temporary))
            if manifest['version'] == previous_version:
                print(f'已是最新版本：{previous_version}')
                return
            print(f'{previous_version} → {manifest["version"]}')
            if check_only:
                print('新版已校验；运行 dotasks update 安装。')
                return
            apply_update(runtime, service)
            print('CLI 升级完成，账号、凭证和任务数据已保留。')
