#!/usr/bin/env python3
"""Local service lifecycle. Does not install or restart the DoTasks application."""
from __future__ import annotations

import argparse
import os
import plistlib
import secrets
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LABEL = "local.laya-dotasks-decision"
DEFAULT_HOME = Path.home() / "Library/Application Support/laya-dotasks-decision"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["init", "download", "run", "install", "stop", "status"])
    parser.add_argument("--home", type=Path, default=DEFAULT_HOME)
    args = parser.parse_args()
    home = args.home.expanduser().resolve()
    python = ROOT / ".venv/bin/python"
    if args.command == "init":
        home.mkdir(parents=True, exist_ok=True, mode=0o700)
        token_path = home / "token"
        if not token_path.exists():
            descriptor = os.open(token_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as target:
                target.write(secrets.token_urlsafe(48) + "\n")
        print(f"Service home: {home}")
        return
    if not python.is_file():
        raise SystemExit("Run uv sync --locked in the service directory first")
    if args.command == "download":
        subprocess.run([str(python), "-m", "decision_service.download_model", str(home / "model")],
                       cwd=ROOT, check=True)
        return
    if args.command == "run":
        os.environ.setdefault("LAYA_DECISION_TOKEN_FILE", str(home / "token"))
        os.environ.setdefault("LAYA_DECISION_MODEL_PATH", str(home / "model"))
        os.environ.setdefault("LAYA_DECISION_DEVICE", "cpu")
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
        os.chdir(ROOT)
        os.execv(str(python), [str(python), "-m", "uvicorn", "decision_service.api:create_app",
                             "--factory", "--host", "127.0.0.1", "--port", "8791",
                             "--workers", "1", "--limit-concurrency", "16", "--no-access-log"])
    if sys.platform != "darwin":
        raise SystemExit("install/stop/status use macOS launchd; use Docker Compose on Linux")
    domain = f"gui/{os.getuid()}"
    plist_path = Path.home() / "Library/LaunchAgents" / f"{LABEL}.plist"
    if args.command == "stop":
        subprocess.run(["launchctl", "bootout", f"{domain}/{LABEL}"], check=True)
        deadline = time.monotonic() + 15
        while subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"],
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            if time.monotonic() >= deadline:
                raise SystemExit("Service is still stopping; check status before reinstalling")
            time.sleep(0.1)
    elif args.command == "status":
        subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"], check=True)
    else:
        if not (home / "model/manifest.json").is_file() or not (home / "token").is_file():
            raise SystemExit("Run init and download first")
        if subprocess.run(["launchctl", "print", f"{domain}/{LABEL}"],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0:
            raise SystemExit("Service already installed; stop it before reinstalling")
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        with plist_path.open("wb") as target:
            plistlib.dump({
                "Label": LABEL,
                "ProgramArguments": [str(python), str(ROOT / "manage.py"), "run", "--home", str(home)],
                "WorkingDirectory": str(ROOT), "RunAtLoad": True,
                "KeepAlive": {"SuccessfulExit": False}, "ThrottleInterval": 30,
                "StandardOutPath": str(home / "service.log"),
                "StandardErrorPath": str(home / "service.log"),
            }, target)
        plist_path.chmod(0o600)
        subprocess.run(["launchctl", "bootstrap", domain, str(plist_path)], check=True)
        print(f"Installed {LABEL} on http://127.0.0.1:8791")


if __name__ == "__main__":
    main()
