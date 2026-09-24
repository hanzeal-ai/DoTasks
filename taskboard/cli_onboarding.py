"""Resumable account/device onboarding. Passwords are never persisted locally."""
from __future__ import annotations

import getpass
import json
import os
import secrets
from pathlib import Path
import subprocess
import tempfile
import time
import urllib.error
from urllib.parse import urlparse
import uuid

from .agent import AgentConfig, default_config_path, default_data_home, load_agent_config
from .app_server import CodexAppServerClient

DEFAULT_CLOUD_URL = 'https://dotasks.hanzeal.com'


def private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(payload, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    try:
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def ensure_codex_login() -> None:
    executable = CodexAppServerClient(default_data_home(), Path(__file__).resolve().parents[1]).executable
    try:
        result = subprocess.run([executable, 'login', 'status'], capture_output=True, timeout=15)
        if result.returncode == 0:
            return
        print('需要登录 Codex；请完成接下来显示的官方登录步骤。')
        if subprocess.call([executable, 'login']) != 0:
            raise RuntimeError('Codex 登录未完成，尚未创建 DoTasks 账号。')
        result = subprocess.run([executable, 'login', 'status'], capture_output=True, timeout=15)
        if result.returncode:
            raise RuntimeError('未能确认 Codex 登录状态。')
    except FileNotFoundError as exc:
        raise RuntimeError('未找到 Codex CLI，请安装 Codex 并重新运行 dotasks init。') from exc


def initialize(args) -> None:
    from .cli import BackgroundService, read_json

    service = BackgroundService()
    service.validate_installation()
    cloud = args.cloud_url.rstrip('/')
    parsed = urlparse(cloud)
    if parsed.scheme != 'https' or not parsed.hostname or parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise ValueError('init 的云端地址必须是无路径、无账号信息的 HTTPS 地址。')
    pending_path = default_data_home() / 'onboarding.json'
    pending = json.loads(pending_path.read_text()) if pending_path.is_file() else {}
    username = (args.username or pending.get('username') or input('创建 DoTasks 账号：').strip()).lower()
    if pending and (pending.get('username') != username or pending.get('cloud_url') != cloud):
        raise ValueError('此安装已有其他账号或云端的初始化记录；不能自动切换账号。')
    if default_config_path().exists() and not pending.get('agent_id'):
        raise ValueError('检测到已有 Agent 配置，不能覆盖现有绑定；请使用独立安装或先完成明确的账号迁移。')
    ensure_codex_login()
    if not pending:
        pending = {'username': username, 'cloud_url': cloud, 'device_id': uuid.uuid4().hex,
                   'device_token': secrets.token_urlsafe(32)}
        private_json(pending_path, pending)
    if pending.get('agent_id') and default_config_path().exists():
        config = load_agent_config()
        if config.cloud_url != cloud or config.agent_id != pending['agent_id']:
            raise ValueError('已有 Agent 配置与初始化记录不一致，停止以保护现有绑定。')
    else:
        password = getpass.getpass('创建密码（12–128 个字符，输入不显示）：')
        if password != getpass.getpass('再次输入密码：'):
            raise ValueError('两次密码不一致；重新运行 dotasks init 即可。')
        if not 12 <= len(password) <= 128:
            raise ValueError('密码需为 12–128 个字符。')
        print('正在创建账号并绑定本机……')
        try:
            response = read_json(cloud + '/api/cli/init', {
                'username': username, 'password': password, 'device_id': pending['device_id'],
                'device_token': pending['device_token'],
            })
        except urllib.error.HTTPError as exc:
            messages = {401: '账号不可注册或密码不正确', 409: '账号已绑定其他安装',
                        404: '云端尚未部署注册接口', 429: '请求过于频繁，请稍后重试'}
            raise RuntimeError(messages.get(exc.code, f'账号初始化失败（HTTP {exc.code}）')) from None
        finally:
            password = ''
        if response.get('cloud_url') != cloud or response.get('username') != username:
            raise ValueError('云端返回的账号或地址不匹配。')
        if not isinstance(response.get('agent_id'), str) or not isinstance(response.get('agent_token'), str):
            raise ValueError('云端未返回有效的本机凭证。')
        if response['agent_token'] != pending['device_token']:
            raise ValueError('云端绑定的本机凭证不匹配。')
        config = AgentConfig(cloud_url=cloud, agent_id=response['agent_id'], agent_token=response['agent_token'], data_home=str(default_data_home())).validate()
        # Save the resumable receipt first. If saving the config fails, the next
        # init retries the same device registration instead of creating another.
        pending['agent_id'] = config.agent_id
        private_json(pending_path, pending)
        private_json(default_config_path(), {
            'cloud_url': cloud, 'agent_id': config.agent_id, 'agent_token': config.agent_token,
            'local_url': config.local_url, 'vault': config.vault,
        })
    service.start()
    deadline = time.monotonic() + 30
    print('等待本地服务和云端 Agent 连接……')
    while time.monotonic() < deadline:
        try:
            local = read_json(config.local_url + '/api/health')
            remote = read_json(cloud + '/_agent/v1/status', token=config.agent_token)
            if local.get('ok') is True and remote.get('agent_id') == config.agent_id and remote.get('connected') is True:
                print(f'初始化完成。打开 {cloud}，使用账号 {username} 和刚才设置的密码登录。')
                if not remote.get('dispatcher_enabled'):
                    print('账号的调度当前已暂停，可在云端看板恢复。')
                return
        except (OSError, ValueError):
            pass
        time.sleep(1)
    raise RuntimeError('账号和本机凭证已保存，但连接检查未通过；运行 dotasks doctor / dotasks logs 排查后重试 dotasks init，无需重新注册。')
