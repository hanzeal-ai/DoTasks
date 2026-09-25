"""Local runtime paths shared by CLI, Agent and MCP/mobile clients."""
from __future__ import annotations

import os
from pathlib import Path


def default_data_home() -> Path:
    configured = os.environ.get("DOTASKS_HOME")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path.home() / "Library" / "Application Support" / "DoTasks"


def default_config_path() -> Path:
    configured = os.environ.get("DOTASKS_AGENT_CONFIG")
    return (
        Path(configured).expanduser().resolve()
        if configured
        else default_data_home() / "cloud-agent.json"
    )
