from __future__ import annotations

import hashlib
import json
import os
import posixpath
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


class ProjectWorkspaceGuard:
    """Validate project paths and capture reproducible Git workspace baselines."""

    @staticmethod
    def _require_helper_authorization(project: str) -> None:
        """Enforce persisted folder consent when running under Taskboard Helper."""
        if not os.environ.get("CODEX_TASKBOARD_HELPER_APP"):
            return
        data_home = Path(
            os.environ.get("CODEX_TASKBOARD_HOME")
            or Path.home() / "Library/Application Support/Codex Taskboard"
        ).expanduser()
        store_path = data_home / "authorized-projects.json"
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
            bookmarks = payload.get("bookmarks") if isinstance(payload, dict) else None
        except (OSError, json.JSONDecodeError):
            bookmarks = None
        if not isinstance(bookmarks, dict) or project not in bookmarks:
            raise ValueError(
                "Project has not been authorized by Taskboard Helper; "
                "add it from the Taskboard project picker first"
            )

    @staticmethod
    def normalize_project(project: str | Path | None) -> str:
        value = str(project or "").strip()
        return str(Path(value).expanduser().resolve()) if value else ""

    @staticmethod
    def require_project_directory(project: str | Path | None) -> str:
        value = str(project or "").strip()
        if not value:
            raise ValueError("project is required")
        path = Path(value).expanduser()
        if not path.is_absolute():
            raise ValueError("project must be an absolute path")
        try:
            resolved = path.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ValueError(f"Project directory does not exist: {path}") from exc
        if not resolved.is_dir():
            raise ValueError(f"Project path is not a directory: {resolved}")
        normalized = str(resolved)
        ProjectWorkspaceGuard._require_helper_authorization(normalized)
        return normalized

    @staticmethod
    def normalize_target_file(value: Any) -> str:
        raw = str(value or "").strip()
        if not raw or "\x00" in raw or "\\" in raw or re.match(r"^[A-Za-z]:", raw):
            raise ValueError("Target file must be a non-empty project-relative POSIX path")
        path = PurePosixPath(raw)
        normalized = posixpath.normpath(raw)
        if path.is_absolute() or normalized in {".", ".."} or normalized.startswith("../"):
            raise ValueError(f"Target file escapes the project directory: {raw}")
        return normalized

    @staticmethod
    def git(project: str, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", "-C", project, *arguments], check=False, capture_output=True, text=True,
        )

    def project_revision(self, project: str | None) -> str:
        normalized = self.normalize_project(project)
        if not normalized or not Path(normalized).is_dir():
            return ""
        result = self.git(normalized, "rev-parse", "HEAD")
        return result.stdout.strip() if result.returncode == 0 else ""

    def workspace_state(self, project: str | None) -> dict[str, Any]:
        normalized = self.normalize_project(project)
        revision = self.project_revision(normalized)
        if not revision:
            return {"available": False, "reason": "git_repository_unavailable", "project": normalized}
        changed = self.git(normalized, "diff", "--name-only", "-z", "HEAD", "--")
        untracked = self.git(normalized, "ls-files", "--others", "--exclude-standard", "-z")
        if changed.returncode != 0 or untracked.returncode != 0:
            return {
                "available": False,
                "reason": "git_status_failed",
                "project": normalized,
                "revision": revision,
            }
        paths = {
            posixpath.normpath(value)
            for value in (changed.stdout + untracked.stdout).split("\0") if value.strip()
        }
        files: dict[str, str] = {}
        for relative in sorted(paths):
            target = Path(normalized) / relative
            if target.is_symlink():
                files[relative] = f"symlink:{target.lstat().st_mode & 0o7777:o}:" + os.readlink(target)
            elif target.is_file():
                digest = hashlib.sha256()
                with target.open("rb") as handle:
                    for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(chunk)
                files[relative] = f"file:{target.stat().st_mode & 0o7777:o}:{digest.hexdigest()}"
            elif target.exists():
                staged = self.git(normalized, "ls-files", "--stage", "--", relative)
                entry = staged.stdout.strip().split()
                if len(entry) >= 2 and entry[0] == "160000":
                    current = self.git(str(target), "rev-parse", "HEAD")
                    files[relative] = f"gitlink:{current.stdout.strip() or entry[1]}"
                else:
                    files[relative] = "other"
            else:
                files[relative] = "deleted"
        fingerprint = hashlib.sha256(
            json.dumps(files, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        return {
            "available": True,
            "project": normalized,
            "revision": revision,
            "files": files,
            "fingerprint": fingerprint,
        }

    def workspace_diff(
        self, project: str | None, baseline_revision: str, paths: list[str],
        max_chars: int = 80000,
    ) -> dict[str, Any]:
        """Capture one bounded delivery diff so later stages do not rediscover it."""
        normalized = self.normalize_project(project)
        revision = self.project_revision(normalized)
        if not revision:
            return {"available": False, "reason": "git_repository_unavailable"}
        normalized_paths = sorted({self.normalize_target_file(path) for path in paths})
        if not normalized_paths:
            return {"available": False, "reason": "no_changed_paths"}
        base = str(baseline_revision or revision).strip()
        diff = self.git(
            normalized, "diff", "--no-ext-diff", "--unified=40", base,
            "--", *normalized_paths,
        )
        if diff.returncode != 0:
            return {
                "available": False, "reason": "git_diff_failed",
                "base_revision": base, "revision": revision,
            }
        parts = [diff.stdout]
        untracked = self.git(normalized, "ls-files", "--others", "--exclude-standard", "-z", "--", *normalized_paths)
        untracked_paths = set(untracked.stdout.split("\0")) if untracked.returncode == 0 else set()
        for relative in normalized_paths:
            if relative not in untracked_paths:
                continue
            target = Path(normalized) / relative
            if not target.is_file() or target.is_symlink():
                continue
            content = target.read_bytes()
            digest = hashlib.sha256(content).hexdigest()
            if b"\0" in content:
                rendered = f"<binary {len(content)} bytes sha256={digest}>"
            else:
                rendered = content.decode("utf-8", errors="replace")
            parts.append(
                f"\ndiff --git a/{relative} b/{relative}\nnew file mode\n"
                f"--- /dev/null\n+++ b/{relative}\n{rendered}\n"
            )
        complete = "".join(parts)
        digest = hashlib.sha256(complete.encode("utf-8")).hexdigest()
        limit = max(1000, int(max_chars))
        return {
            "available": True,
            "base_revision": base,
            "revision": revision,
            "paths": normalized_paths,
            "sha256": digest,
            "content": complete[:limit],
            "truncated": len(complete) > limit,
            "full_chars": len(complete),
        }
