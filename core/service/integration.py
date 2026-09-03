from __future__ import annotations

import fcntl
import hashlib
import json
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


MAX_DELIVERY_PATCH_BYTES = 20 * 1024 * 1024


class TaskIntegrationMixin:
    """Keep parallel development off the user's checkout and serialize integration."""

    @contextmanager
    def _project_integration_lock(self, project: str) -> Iterator[None]:
        lock_directory = self.data_home / "integration-locks"
        lock_directory.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(project.encode("utf-8")).hexdigest()
        with (lock_directory / f"{digest}.lock").open("a+") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _integration_branch(project: str) -> tuple[str, str]:
        digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:12]
        branch = f"codex/dotasks-integration-{digest}"
        return branch, f"refs/heads/{branch}"

    def _create_run_base_ref(
        self, project: str, run_id: str, base_revision: str,
    ) -> str:
        digest = hashlib.sha256(project.encode("utf-8")).hexdigest()[:8]
        branch = f"codex/dotasks-run-{run_id.lower()}-{digest}"
        ref = f"refs/heads/{branch}"
        created = self._git(project, "update-ref", ref, base_revision)
        if created.returncode != 0:
            raise ValueError(
                "Cannot create the immutable task base branch: "
                + (created.stderr.strip() or created.stdout.strip())
            )
        return ref

    def _ensure_project_integration_state(
        self, project: str, workspace_state: dict[str, Any], connection: Any | None = None,
    ) -> dict[str, Any]:
        branch, integration_ref = self._integration_branch(project)

        def ensure(active_connection: Any) -> dict[str, Any]:
            row = active_connection.execute(
                "SELECT * FROM project_integration_states WHERE project=?", (project,)
            ).fetchone()
            previous = dict(row) if row else {}
            seed_revision = str(
                previous.get("revision") or workspace_state.get("revision") or ""
            )
            resolved = self._git(project, "rev-parse", "--verify", integration_ref)
            if resolved.returncode != 0:
                if not seed_revision:
                    raise ValueError("Cannot establish a Git revision for parallel development")
                created = self._git(project, "update-ref", integration_ref, seed_revision)
                if created.returncode != 0:
                    raise ValueError(
                        "Cannot create the DoTasks integration branch: "
                        + (created.stderr.strip() or created.stdout.strip())
                    )
                resolved = self._git(project, "rev-parse", "--verify", integration_ref)
            revision = resolved.stdout.strip()
            if not revision:
                raise ValueError("Cannot resolve the DoTasks integration branch")
            try:
                managed_files = json.loads(
                    str(previous.get("managed_workspace_files") or "{}")
                )
            except (TypeError, json.JSONDecodeError):
                managed_files = {}
            if not isinstance(managed_files, dict):
                managed_files = {}
            workspace_sync_revision = str(
                previous.get("workspace_sync_revision") or seed_revision or revision
            )
            active_connection.execute(
                """INSERT INTO project_integration_states(
                       project, integration_ref, revision, workspace_fingerprint,
                       managed_workspace_files, workspace_sync_revision, last_run_id,
                       updated_at
                   ) VALUES(?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
                   ON CONFLICT(project) DO UPDATE SET
                     integration_ref=excluded.integration_ref,
                     revision=excluded.revision,
                     managed_workspace_files=excluded.managed_workspace_files,
                     workspace_sync_revision=CASE
                       WHEN project_integration_states.workspace_sync_revision=''
                       THEN excluded.workspace_sync_revision
                       ELSE project_integration_states.workspace_sync_revision END,
                     updated_at=CURRENT_TIMESTAMP""",
                (
                    project,
                    integration_ref,
                    revision,
                    str(previous.get("workspace_fingerprint") or workspace_state.get("fingerprint") or ""),
                    json.dumps(managed_files, ensure_ascii=False, sort_keys=True),
                    workspace_sync_revision,
                    str(previous.get("last_run_id") or ""),
                ),
            )
            return {
                **previous,
                "project": project,
                "integration_ref": integration_ref,
                "integration_branch": branch,
                "revision": revision,
                "managed_workspace_files": managed_files,
                "workspace_sync_revision": workspace_sync_revision,
            }

        if connection is not None:
            return ensure(connection)
        with self.db.transaction() as active_connection:
            return ensure(active_connection)

    @staticmethod
    def _unmanaged_workspace_files(
        workspace_state: dict[str, Any], integration_state: dict[str, Any]
    ) -> set[str]:
        current = workspace_state.get("files") or {}
        managed = integration_state.get("managed_workspace_files") or {}
        return {
            str(path)
            for path in set(current) | set(managed)
            if current.get(path) != managed.get(path)
        }

    def _project_execution_policy(
        self,
        project: str,
        connection: Any | None = None,
        *,
        initialize_integration: bool = True,
    ) -> dict[str, Any]:
        """Return capacity and an immutable Git baseline independent of checkout dirt."""
        if not str(project or "").strip():
            return {
                "capacity": 1,
                "execution_environment": "projectless",
                "fallback_reason": "projectless_task",
                "baseline": {
                    "available": False,
                    "reason": "projectless_task",
                    "project": "",
                    "files": {},
                },
                "unmanaged_files": [],
            }
        state = self._workspace_state(project)
        if not self.parallel_development_enabled():
            return {
                "capacity": 1,
                "execution_environment": "local",
                "fallback_reason": "parallel_development_disabled",
                "baseline": state,
                "unmanaged_files": sorted((state.get("files") or {}).keys()),
            }
        if not state.get("available"):
            return {
                "capacity": 1,
                "execution_environment": "local",
                "fallback_reason": str(state.get("reason") or "git_workspace_unavailable"),
                "baseline": state,
                "unmanaged_files": [],
            }
        if initialize_integration:
            integration = self._ensure_project_integration_state(
                project, state, connection
            )
        else:
            if connection is None:
                raise ValueError("Read-only execution policy requires a connection")
            row = connection.execute(
                "SELECT * FROM project_integration_states WHERE project=?", (project,)
            ).fetchone()
            integration = dict(row) if row else {"managed_workspace_files": {}}
            if row:
                try:
                    managed_files = json.loads(
                        str(integration.get("managed_workspace_files") or "{}")
                    )
                except (TypeError, json.JSONDecodeError):
                    managed_files = {}
                integration["managed_workspace_files"] = (
                    managed_files if isinstance(managed_files, dict) else {}
                )
        unmanaged = self._unmanaged_workspace_files(state, integration)
        baseline = {
            "available": True,
            "project": project,
            "revision": str(integration.get("revision") or state.get("revision") or ""),
            "integration_ref": str(integration.get("integration_ref") or ""),
            "integration_branch": str(integration.get("integration_branch") or ""),
            "files": {},
            "fingerprint": "",
        }
        return {
            "capacity": self.max_parallel_development(),
            "execution_environment": "worktree",
            "fallback_reason": "",
            "baseline": baseline,
            "unmanaged_files": sorted(unmanaged),
        }

    def _task_unmanaged_workspace_conflicts(
        self, task: dict[str, Any], policy: dict[str, Any]
    ) -> list[str]:
        unmanaged = set(policy.get("unmanaged_files") or [])
        if not unmanaged:
            return []
        targets = {
            self._normalize_target_file(target.get("file"))
            for target in (task.get("implementation_contract") or {}).get("targets") or []
            if isinstance(target, dict) and str(target.get("file") or "").strip()
        }
        return sorted(targets & unmanaged)

    def _validate_execution_workspace(
        self, project: str, workspace_path: str, *, require_worktree: bool,
    ) -> str:
        workspace = self._require_project_directory(workspace_path)
        project = self._require_project_directory(project)
        project_common = self._git(project, "rev-parse", "--path-format=absolute", "--git-common-dir")
        workspace_common = self._git(
            workspace, "rev-parse", "--path-format=absolute", "--git-common-dir"
        )
        if project_common.returncode != 0 or workspace_common.returncode != 0:
            raise ValueError("Execution workspace must be a Git worktree of the task project")
        if Path(project_common.stdout.strip()).resolve() != Path(
            workspace_common.stdout.strip()
        ).resolve():
            raise ValueError("Execution workspace belongs to a different Git repository")
        if require_worktree and workspace == project:
            raise ValueError("Parallel development requires an isolated Git worktree")
        return workspace

    def _capture_delivery_patch(
        self, run_id: str, workspace: str, base_revision: str, paths: list[str],
    ) -> tuple[str, str]:
        normalized_paths = sorted({self._normalize_target_file(path) for path in paths})
        if not normalized_paths:
            raise ValueError("Cannot capture a delivery patch without changed paths")
        tracked = self._git(
            workspace, "diff", "--binary", "--no-ext-diff", base_revision,
            "--", *normalized_paths,
        )
        if tracked.returncode != 0:
            raise ValueError("Cannot capture the isolated delivery patch")
        parts = [tracked.stdout]
        untracked = self._git(
            workspace, "ls-files", "--others", "--exclude-standard", "-z",
            "--", *normalized_paths,
        )
        if untracked.returncode != 0:
            raise ValueError("Cannot inspect untracked delivery files")
        for relative in sorted(path for path in untracked.stdout.split("\0") if path):
            diff = self._git(
                workspace, "diff", "--binary", "--no-index", "--", "/dev/null", relative,
            )
            if diff.returncode not in {0, 1}:
                raise ValueError(f"Cannot capture new delivery file: {relative}")
            parts.append(diff.stdout)
        patch = "".join(parts).encode("utf-8")
        if not patch:
            raise ValueError("Isolated delivery produced an empty Git patch")
        if len(patch) > MAX_DELIVERY_PATCH_BYTES:
            raise ValueError("Isolated delivery patch exceeds the 20 MiB limit")
        artifact_directory = self.data_home / "artifacts" / "delivery-patches"
        artifact_directory.mkdir(parents=True, exist_ok=True)
        artifact_path = artifact_directory / f"{run_id}.patch"
        artifact_path.write_bytes(patch)
        return str(artifact_path), hashlib.sha256(patch).hexdigest()

    def _commit_patch_to_integration_ref(
        self, project: str, integration_ref: str, revision: str,
        artifact_path: Path, task_id: str, run_id: str,
    ) -> str:
        worktree_home = self.data_home / "integration-worktrees"
        worktree_home.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="dotasks-", dir=worktree_home) as temporary:
            checkout = Path(temporary) / "checkout"
            added = self._git(
                project, "worktree", "add", "--detach", str(checkout), revision
            )
            if added.returncode != 0:
                raise ValueError(
                    "Cannot create the DoTasks integration worktree: "
                    + (added.stderr.strip() or added.stdout.strip())
                )
            try:
                check = self._git(
                    str(checkout), "apply", "--check", "--binary", str(artifact_path)
                )
                if check.returncode != 0:
                    raise ValueError(
                        "Parallel delivery cannot be integrated cleanly: "
                        + (check.stderr.strip() or check.stdout.strip() or "git apply --check failed")
                    )
                applied = self._git(
                    str(checkout), "apply", "--binary", str(artifact_path)
                )
                if applied.returncode != 0:
                    raise ValueError(
                        "Parallel delivery integration failed: "
                        + (applied.stderr.strip() or applied.stdout.strip() or "git apply failed")
                    )
                staged = self._git(str(checkout), "add", "--all")
                if staged.returncode != 0:
                    raise ValueError("Cannot stage the integrated delivery")
                committed = self._git(
                    str(checkout), "-c", "user.name=DoTasks",
                    "-c", "user.email=dotasks@local.invalid",
                    "commit", "--no-gpg-sign", "-m", f"[DoTasks] {task_id} {run_id}",
                )
                if committed.returncode != 0:
                    raise ValueError(
                        "Cannot commit the integrated delivery: "
                        + (committed.stderr.strip() or committed.stdout.strip())
                    )
                resolved = self._git(str(checkout), "rev-parse", "HEAD")
                if resolved.returncode != 0 or not resolved.stdout.strip():
                    raise ValueError("Cannot resolve the integrated delivery revision")
                integrated_revision = resolved.stdout.strip()
            finally:
                self._git(project, "worktree", "remove", "--force", str(checkout))
                self._git(project, "worktree", "prune")
        updated = self._git(
            project, "update-ref", integration_ref, integrated_revision, revision
        )
        if updated.returncode != 0:
            raise ValueError("The DoTasks integration branch changed concurrently")
        return integrated_revision

    def _sync_integration_to_workspace(
        self, project: str, integration: dict[str, Any], integrated_revision: str,
    ) -> dict[str, Any]:
        sync_revision = str(
            integration.get("workspace_sync_revision") or integration.get("revision") or ""
        )
        if not sync_revision or sync_revision == integrated_revision:
            return {
                "status": "synced",
                "error": "",
                "revision": integrated_revision,
                "workspace": self._workspace_state(project),
                "managed_files": integration.get("managed_workspace_files") or {},
            }
        changed = self._git(
            project, "diff", "--name-only", "-z", sync_revision, integrated_revision, "--"
        )
        if changed.returncode != 0:
            return {"status": "pending", "error": "cannot inspect pending integration files"}
        pending_files = {
            self._normalize_target_file(path)
            for path in changed.stdout.split("\0") if path.strip()
        }
        current = self._workspace_state(project)
        if not current.get("available"):
            return {"status": "pending", "error": "project workspace is unavailable"}
        unmanaged = self._unmanaged_workspace_files(current, integration)
        overlap = sorted(pending_files & unmanaged)
        if overlap:
            return {
                "status": "pending",
                "error": "workspace has unmanaged changes in: " + ", ".join(overlap),
            }
        diff = self._git(
            project, "diff", "--binary", "--no-ext-diff",
            sync_revision, integrated_revision, "--",
        )
        if diff.returncode != 0:
            return {"status": "pending", "error": "cannot build the workspace sync patch"}
        if not diff.stdout:
            return {
                "status": "synced", "error": "", "revision": integrated_revision,
                "workspace": current,
                "managed_files": integration.get("managed_workspace_files") or {},
            }
        sync_home = self.data_home / "artifacts" / "workspace-sync"
        sync_home.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            prefix="dotasks-", suffix=".patch", dir=sync_home, delete=False
        ) as handle:
            sync_patch = Path(handle.name)
            handle.write(diff.stdout.encode("utf-8"))
        try:
            check = self._git(project, "apply", "--check", "--binary", str(sync_patch))
            if check.returncode != 0:
                return {
                    "status": "pending",
                    "error": check.stderr.strip() or check.stdout.strip() or "workspace sync check failed",
                }
            applied = self._git(project, "apply", "--binary", str(sync_patch))
            if applied.returncode != 0:
                return {
                    "status": "pending",
                    "error": applied.stderr.strip() or applied.stdout.strip() or "workspace sync failed",
                }
        finally:
            sync_patch.unlink(missing_ok=True)
        updated = self._workspace_state(project)
        managed_files = dict(integration.get("managed_workspace_files") or {})
        updated_files = updated.get("files") or {}
        for path in pending_files:
            if path in updated_files:
                managed_files[path] = updated_files[path]
            else:
                managed_files.pop(path, None)
        return {
            "status": "synced",
            "error": "",
            "revision": integrated_revision,
            "workspace": updated,
            "managed_files": managed_files,
        }

    def _integrate_delivery_artifact(
        self, task: dict[str, Any], delivery_run_id: str,
        delivery_override: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        delivery = delivery_override or self.get_run(delivery_run_id)
        if delivery.get("execution_environment") != "worktree":
            return {"integration_status": "local", "workspace_sync_status": "local"}
        if delivery.get("integration_status") == "integrated":
            return {
                "integration_status": "integrated",
                "integration_revision": str(delivery.get("integration_revision") or ""),
                "workspace_sync_status": str(delivery.get("workspace_sync_status") or ""),
                "workspace_sync_error": str(delivery.get("workspace_sync_error") or ""),
            }
        project = self._require_project_directory(task.get("project"))
        artifact_path = Path(str(delivery.get("artifact_path") or ""))
        if not artifact_path.is_file():
            raise ValueError("Reviewed worktree delivery is missing its persisted patch")
        patch = artifact_path.read_bytes()
        if hashlib.sha256(patch).hexdigest() != str(delivery.get("artifact_sha256") or ""):
            raise ValueError("Reviewed worktree delivery patch checksum does not match")

        with self._project_integration_lock(project):
            workspace = self._workspace_state(project)
            integration = self._ensure_project_integration_state(project, workspace)
            integration_ref = str(integration["integration_ref"])
            current_revision = str(integration["revision"])
            integrated_revision = self._commit_patch_to_integration_ref(
                project, integration_ref, current_revision, artifact_path,
                str(task["id"]), delivery_run_id,
            )
            sync = self._sync_integration_to_workspace(
                project, integration, integrated_revision
            )
            sync_status = str(sync.get("status") or "pending")
            sync_error = str(sync.get("error") or "")[:2000]
            synced_workspace = sync.get("workspace") or workspace
            managed_files = sync.get("managed_files") or integration.get(
                "managed_workspace_files"
            ) or {}
            sync_revision = (
                str(sync.get("revision") or "")
                if sync_status == "synced"
                else str(integration.get("workspace_sync_revision") or "")
            )
            with self.db.transaction() as connection:
                connection.execute(
                    """UPDATE project_integration_states SET
                         integration_ref=?, revision=?, workspace_fingerprint=?,
                         managed_workspace_files=?, workspace_sync_revision=?,
                         last_run_id=?, updated_at=CURRENT_TIMESTAMP WHERE project=?""",
                    (
                        integration_ref,
                        integrated_revision,
                        str(synced_workspace.get("fingerprint") or ""),
                        json.dumps(managed_files, ensure_ascii=False, sort_keys=True),
                        sync_revision,
                        delivery_run_id,
                        project,
                    ),
                )
                connection.execute(
                    """UPDATE task_runs SET integration_status='integrated',
                       integration_error='', integration_revision=?,
                       workspace_sync_status=?, workspace_sync_error=?,
                       updated_at=CURRENT_TIMESTAMP WHERE id=?""",
                    (
                        integrated_revision, sync_status, sync_error, delivery_run_id,
                    ),
                )
                self._event(
                    connection,
                    "run",
                    delivery_run_id,
                    "delivery_integrated",
                    {
                        "task_id": task["id"],
                        "project": project,
                        "integration_ref": integration_ref,
                        "integration_revision": integrated_revision,
                        "workspace_sync_status": sync_status,
                        "workspace_sync_error": sync_error,
                    },
                )
            return {
                "integration_status": "integrated",
                "integration_revision": integrated_revision,
                "workspace_sync_status": sync_status,
                "workspace_sync_error": sync_error,
            }
