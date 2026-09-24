"""Own the two launchd jobs used by the standalone CLI."""
from __future__ import annotations

import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys

from .agent import default_config_path, default_data_home, load_agent_config

LABELS = {'server': 'com.dotasks.cli.server', 'agent': 'com.dotasks.cli.agent'}
LEGACY_LABEL = 'local.sanmws.dotasks-helper'


class BackgroundService:
    def __init__(self):
        if sys.platform != 'darwin':
            raise RuntimeError('CLI 后台管理目前支持 macOS。')
        self.domain = f'gui/{os.getuid()}'

    def target(self, name):
        return f'{self.domain}/{LABELS[name]}'

    def plist(self, name):
        return Path.home() / 'Library/LaunchAgents' / (LABELS[name] + '.plist')

    def run(self, *args, check=True):
        result = subprocess.run(['/bin/launchctl', *args], capture_output=True, text=True, timeout=15)
        if check and result.returncode:
            raise RuntimeError(f'launchctl {args[0]} 失败：{result.stderr.strip()}')
        return result

    def job_state(self, target):
        result = self.run('print', target, check=False)
        if result.returncode:
            if 'Could not find service' in result.stderr:
                return 'stopped'
            raise RuntimeError(f'无法读取后台状态：{result.stderr.strip()}')
        match = re.search(r'^\s*state = (.+)$', result.stdout, re.MULTILINE)
        return match.group(1).strip() if match else 'loaded'

    def states(self):
        return {name: self.job_state(self.target(name)) for name in LABELS}

    def state(self):
        states = set(self.states().values())
        return next(iter(states)) if len(states) == 1 else 'degraded'

    def legacy_running(self):
        return self.job_state(f'{self.domain}/{LEGACY_LABEL}') != 'stopped'

    def validate_installation(self):
        for name in LABELS:
            path = self.plist(name)
            if not path.is_file():
                raise RuntimeError('尚未安装独立 CLI 运行时，请运行 CLI 安装器。')
            with path.open('rb') as stream:
                payload = plistlib.load(stream)
            args = payload.get('ProgramArguments') or []
            env = payload.get('EnvironmentVariables') or {}
            if payload.get('Label') != LABELS[name] or len(args) < 4 or args[1:4] != ['-B', '-m', 'taskboard.' + ('server' if name == 'server' else 'agent')] or not os.access(args[0], os.X_OK):
                raise RuntimeError('CLI 后台配置不完整，请重新安装。')
            runtime = Path(payload.get('WorkingDirectory', ''))
            if not (runtime / 'taskboard' / 'cli.py').is_file():
                raise RuntimeError('CLI 运行时缺失，请重新安装。')
            if Path(env.get('DOTASKS_HOME', '')).resolve() != default_data_home().resolve() or default_config_path().resolve() != (default_data_home() / 'cloud-agent.json').resolve():
                raise RuntimeError('CLI 数据目录或配置路径与后台服务不一致。')
            for key in ('DOTASKS_CLOUD_URL', 'DOTASKS_AGENT_ID', 'DOTASKS_AGENT_TOKEN', 'DOTASKS_LOCAL_URL', 'DOTASKS_OBSIDIAN_VAULT'):
                if os.environ.get(key) and os.environ[key] != env.get(key):
                    raise RuntimeError('请使用 dotasks configure 保存配置并取消临时 Agent 环境变量。')

    def start(self):
        self.validate_installation()
        load_agent_config()
        if self.legacy_running():
            raise RuntimeError('旧 Helper 正在运行；请用安装器 --replace-helper 迁移，避免重复执行任务。')
        before = self.states()
        created = []
        try:
            for name in LABELS:
                self.run('enable', self.target(name))
                if before[name] == 'stopped':
                    created.append(name)
                    self.run('bootstrap', self.domain, str(self.plist(name)))
                elif before[name] != 'running':
                    self.run('kickstart', self.target(name))
        except Exception:
            for name in reversed(created):
                self.run('disable', self.target(name), check=False)
                self.run('bootout', self.target(name), check=False)
            raise
        print('本地服务和 Agent 已启用；登录自启已开启。')

    def stop(self):
        before = self.states()
        errors = []
        for name in reversed(LABELS):
            try:
                self.run('disable', self.target(name))
                if before[name] != 'stopped':
                    self.run('bootout', self.target(name))
            except RuntimeError as exc:
                errors.append(str(exc))
        if errors:
            raise RuntimeError('; '.join(errors))
        print('本地服务和 Agent 已停止，登录自启已关闭。')
