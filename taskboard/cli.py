"""Terminal controls for the standalone DoTasks CLI runtime."""
from __future__ import annotations

import argparse
from collections import deque
import getpass
import json
import os
from pathlib import Path
import plistlib
import re
import subprocess
import sys
import urllib.request

from .agent import AgentConfig, default_config_path, default_data_home, load_agent_config, save_agent_config
from .app_server import CodexAppServerClient
from core.workflow import ACTIVE_RUN_STATUSES

from .cli_service import BackgroundService


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def configure(args: argparse.Namespace) -> None:
    target = default_config_path()
    stored = json.loads(target.read_text()) if target.is_file() else {}
    if not isinstance(stored, dict):
        raise ValueError("Agent 配置必须是 JSON 对象。")
    url = args.cloud_url or stored.get("cloud_url")
    if not url:
        url = input("云端地址：https://你的域名\n> ").strip()
    token = args.agent_token or stored.get("agent_token")
    if not token:
        token = getpass.getpass("Agent Token（输入不显示）：")
    config = AgentConfig(
        cloud_url=url, agent_token=token,
        agent_id=args.agent_id or stored.get("agent_id", "default"),
        local_url=args.local_url or stored.get("local_url", "http://127.0.0.1:8765"),
        vault=args.vault if args.vault is not None else stored.get("vault", ""),
        data_home=str(default_data_home()),
    )
    print(f"配置已保存：{save_agent_config(config)}")
    print("已有配置项会保留。若后台正在运行，执行 dotasks stop 后再执行 dotasks start 使新配置生效。")


def read_json(url: str, payload: dict | None = None, token: str = "") -> dict:
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=json.dumps(payload).encode() if payload is not None else None, headers=headers)
    with urllib.request.build_opener(NoRedirect()).open(request, timeout=5) as response:
        result = json.load(response)
    if not isinstance(result, dict):
        raise ValueError("服务返回了无效响应")
    return result


def status(*, doctor: bool = False) -> int:
    failures = 0
    try:
        service = BackgroundService()
        service.validate_installation()
        state = service.state()
        print(f"后台服务：{state}")
        if state != "running":
            failures += 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"后台服务：{exc}")
        failures += 1
    try:
        config = load_agent_config()
    except (OSError, ValueError, TypeError, AttributeError):
        print("Agent 配置：缺失或无效，请执行 dotasks configure。")
        return 1
    print(f"Agent：{config.agent_id}")
    try:
        health = read_json(config.local_url.rstrip("/") + "/api/health")
        if health.get("ok") is not True:
            raise ValueError("健康检查未通过")
        print(f"本地服务：健康，版本 {health.get('version', '未知')}")
    except (OSError, ValueError):
        print("本地服务：不可用或需要认证。")
        failures += 1
    try:
        response = read_json(config.cloud_url.rstrip("/") + "/_agent/v1/tools/call", {
            "agent_id": config.agent_id, "name": "list_board", "arguments": {},
        }, config.agent_token)
        board = response["result"]
        enabled = board["dispatcher"]["enabled"]
        active = [task for task in board["tasks"] if task.get("active_run_status") in ACTIVE_RUN_STATUSES]
        print("云端 API：可达，Agent 凭证有效（不代表后台 WSS 已连接）。")
        print(f"云端调度：{'已启用' if enabled else '已暂停'}")
        print(f"云端活动任务：{len(active)}")
        for task in active:
            print(f"  {task['id']}  {task.get('title', '')}")
        print("后台 Agent 在线状态请以云端看板为准。")
    except (OSError, ValueError, KeyError, TypeError):
        print("云端 API：无法确认，请检查网络、云端地址、Agent ID 和 Token。")
        failures += 1
    if doctor:
        print(f"Python：{sys.version.split()[0]}")
        executable = CodexAppServerClient(default_data_home(), Path(__file__).resolve().parents[1]).executable
        try:
            result = subprocess.run([executable, "login", "status"], capture_output=True, text=True, timeout=10)
            if result.returncode:
                raise RuntimeError("未登录")
            print("Codex：CLI 可执行，已登录。")
        except (OSError, RuntimeError, subprocess.SubprocessError):
            print("Codex：不可执行或未登录，请检查 Codex CLI 并执行 codex login。")
            failures += 1
        print("诊断使用当前终端环境；后台运行环境与项目目录权限仍以实际任务为准。")
    return 1 if failures else 0


def logs(args: argparse.Namespace) -> int:
    paths = [default_data_home() / "logs" / f"{args.service}.{suffix}.log" for suffix in ("out", "err")]
    existing = [path for path in paths if path.is_file()]
    if not existing:
        print("暂无日志；请先启动后台服务。")
        return 1
    if args.follow:
        return subprocess.call(["/usr/bin/tail", "-n", str(args.lines), "-F", *map(str, existing)])
    for path in existing:
        print(f"==> {path.name} <==")
        with path.open(errors="replace") as stream:
            print("".join(deque(stream, maxlen=args.lines)), end="")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # Keep existing consumers of the original server entry point working.
    if not argv or argv[0] == "serve" or (argv[0].startswith("-") and argv[0] not in {"-h", "--help"}):
        from .server import main as serve
        previous = sys.argv
        try:
            sys.argv = [previous[0], *(argv[1:] if argv and argv[0] == "serve" else argv)]
            serve()
            return 0
        finally:
            sys.argv = previous
    parser = argparse.ArgumentParser(prog="dotasks", description="管理本地 DoTasks 后台服务和云端 Agent")
    commands = parser.add_subparsers(dest="command", required=True)
    from .cli_onboarding import DEFAULT_CLOUD_URL
    onboarding = commands.add_parser("init", help="创建云端账号、授权本机并自动启动")
    onboarding.add_argument("--cloud-url", default=DEFAULT_CLOUD_URL)
    onboarding.add_argument("--username")
    commands.add_parser("account", help="显示本机绑定账号和钥匙串中保存的密码")
    upgrade = commands.add_parser("update", help="校验并安装新版 CLI；失败时恢复旧版")
    upgrade.add_argument("--check", action="store_true", help="只检查新版本")
    settings = commands.add_parser("configure", help="交互配置云端地址和 Agent 凭证")
    for name in ("cloud-url", "agent-id", "agent-token", "local-url", "vault"):
        settings.add_argument(f"--{name}")
    commands.add_parser("start", help="启动已安装后台服务并启用登录自启")
    commands.add_parser("stop", help="停止后台服务并关闭登录自启（可能中断任务）")
    commands.add_parser("status", help="查看后台服务、云端 API 和活动任务")
    commands.add_parser("doctor", help="检查服务、配置和 Codex 登录状态")
    commands.add_parser("serve", help="前台运行本地 HTTP 服务；参数见 dotasks serve --help")
    log_parser = commands.add_parser("logs", help="查看后台日志")
    log_parser.add_argument("--service", choices=("agent", "server"), default="agent")
    log_parser.add_argument("-f", "--follow", action="store_true")
    log_parser.add_argument("-n", "--lines", type=int, default=50)
    args = parser.parse_args(argv)
    try:
        if args.command == "init":
            from .cli_onboarding import initialize
            initialize(args)
        elif args.command == "account":
            from .cli_account import show_account
            show_account()
        elif args.command == "update":
            from .cli_update import update
            update(check_only=args.check)
        elif args.command == "configure":
            configure(args)
        elif args.command in {"start", "stop"}:
            getattr(BackgroundService(), args.command)()
        elif args.command in {"status", "doctor"}:
            return status(doctor=args.command == "doctor")
        elif args.command == "logs":
            if args.lines < 1:
                raise ValueError("日志行数必须大于 0。")
            return logs(args)
        return 0
    except (OSError, ValueError, RuntimeError, TypeError, AttributeError, subprocess.SubprocessError) as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
