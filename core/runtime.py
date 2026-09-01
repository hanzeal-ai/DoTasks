from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Mapping


DEFAULT_TOOL_DIRECTORIES = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/usr/local/go/bin",
    "/opt/homebrew/opt/go/libexec/bin",
    "/usr/bin",
    "/bin",
    "/usr/sbin",
    "/sbin",
)


def _prepend_unique(directories: list[str], value: str | Path | None) -> None:
    if not value:
        return
    path = str(Path(value).expanduser())
    if path and path not in directories:
        directories.append(path)


def _nvm_version_key(path: Path) -> tuple[int, ...]:
    value = path.parent.name.removeprefix("v")
    try:
        return tuple(int(part) for part in value.split("."))
    except ValueError:
        return ()


def project_runtime_environment(
    project: str | Path | None,
    base: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return a stable project tool PATH for workers and acceptance commands."""
    environment = dict(os.environ if base is None else base)
    project_path = Path(project).expanduser().resolve() if project else None
    directories: list[str] = []

    for variable in ("DOTASKS_PYTHON_BIN", "DOTASKS_NODE_BIN"):
        configured = environment.get(variable)
        if configured:
            _prepend_unique(directories, Path(configured).expanduser().parent)

    home = Path(environment.get("HOME") or Path.home()).expanduser()
    _prepend_unique(directories, home / ".local" / "bin")
    _prepend_unique(directories, home / "go" / "bin")
    nvm_bins = sorted(
        (path for path in (home / ".nvm" / "versions" / "node").glob("*/bin") if path.is_dir()),
        key=_nvm_version_key,
        reverse=True,
    )
    for path in nvm_bins:
        _prepend_unique(directories, path)
    for path in DEFAULT_TOOL_DIRECTORIES:
        _prepend_unique(directories, path)
    for path in str(environment.get("PATH") or "").split(os.pathsep):
        _prepend_unique(directories, path)

    if project_path:
        python_home: Path | None = None
        version_file = project_path / ".python-version"
        required_version = ""
        try:
            required_version = version_file.read_text(encoding="utf-8").strip().splitlines()[0]
        except (OSError, IndexError):
            pass
        uv = shutil.which("uv", path=os.pathsep.join(directories))
        if required_version and uv:
            lookup_environment = dict(environment)
            lookup_environment["PATH"] = os.pathsep.join(directories)
            lookup_environment["UV_PYTHON_DOWNLOADS"] = "never"
            try:
                found = subprocess.run(
                    [uv, "python", "find", required_version],
                    cwd=project_path,
                    env=lookup_environment,
                    capture_output=True,
                    text=True,
                    timeout=5,
                    check=False,
                )
                candidate = Path(found.stdout.strip()).expanduser()
                if found.returncode == 0 and candidate.is_file():
                    python_home = candidate.parent
            except (OSError, subprocess.TimeoutExpired):
                pass
        if python_home is None and (project_path / ".venv" / "bin").is_dir():
            python_home = project_path / ".venv" / "bin"
        if python_home:
            directories = [str(python_home), *[item for item in directories if item != str(python_home)]]

    environment["PATH"] = os.pathsep.join(directories)
    return environment
