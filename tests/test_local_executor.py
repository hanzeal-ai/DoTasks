from __future__ import annotations

import base64
import hashlib
import tempfile
import subprocess
import unittest
from pathlib import Path

from taskboard.local_executor import LocalCodexExecutor, WORKER_ID


class FakeService:
    _MISSING_LIFECYCLE_CALLBACK_REASON = "missing callback"

    def __init__(self, dispatch: dict | None):
        self.dispatch = dispatch
        self.status = "claimed"
        self.completed_cycles: list[tuple[str, int]] = []
        self.bindings: list[tuple[str, str]] = []
        self.failures: list[tuple[str, str]] = []
        self.visual_artifacts: dict[str, dict] = {}

    def claim_schedule_cycle(self, worker_id, lease_seconds, force=False):
        self.assert_worker = (worker_id, lease_seconds, force)
        dispatches = [self.dispatch] if self.dispatch else []
        self.dispatch = None
        return {
            "status": "claimed",
            "cycle_generation": 7,
            "code_review": {"dispatches": []},
            "development": {"dispatches": dispatches},
        }

    def complete_schedule_cycle(self, worker_id, generation):
        self.completed_cycles.append((worker_id, generation))

    def bind_native_dispatch(self, run_id, thread_id, **kwargs):
        self.status = "bound"
        self.bindings.append((run_id, thread_id))

    def get_native_dispatch(self, run_id):
        return {"run_id": run_id, "status": self.status}

    def fail_native_dispatch(self, run_id, reason):
        self.status = "failed"
        self.failures.append((run_id, reason))

    def renew_native_dispatch(self, run_id, lease_seconds):
        raise AssertionError("short test must not renew its lease")

    def read_visual_artifact(self, artifact_id):
        return self.visual_artifacts[artifact_id]


class FakeClient:
    def __init__(self, service: FakeService, close_lifecycle: bool, **_kwargs):
        self.service = service
        self.close_lifecycle = close_lifecycle
        self.connected = True
        self.last_error = ""
        self.started_prompt = ""

    def start(self):
        pass

    def stop(self):
        self.connected = False

    def start_thread(self, project_path, title):
        self.created = (project_path, title)
        return "cli-thread-1"

    def resume_thread(self, thread_id):
        self.resumed = thread_id
        return thread_id

    def start_turn(self, thread_id, prompt):
        self.started_prompt = prompt
        if self.close_lifecycle:
            self.service.status = "completed"
        return "turn-1"

    def wait_notification(self, timeout):
        return {
            "method": "turn/completed",
            "params": {
                "threadId": "cli-thread-1",
                "turn": {"id": "turn-1", "status": "completed"},
            },
        }


def make_dispatch(**overrides):
    dispatch = {
        "run_id": "RUN-0001",
        "dispatch_attempt_id": "attempt-1",
        "project_path": "/tmp/project",
        "dispatch_title": "[DoTasks] TASK-0001 开发",
        "dispatch_prompt": "$dotasks-lifecycle\nBuild it",
        "thread_id": "",
        "resume_thread_id": "",
    }
    dispatch.update(overrides)
    return dispatch


class LocalCodexExecutorTest(unittest.TestCase):
    def build_executor(self, service, close_lifecycle):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        executor = LocalCodexExecutor(
            temporary.name,
            runtime_home=Path(__file__).resolve().parents[1],
            client_factory=lambda **kwargs: FakeClient(
                service, close_lifecycle, **kwargs
            ),
        )
        executor.service = service
        return executor

    def test_dispatch_cycle_launches_app_server_and_completes_scheduler_lease(self):
        service = FakeService(make_dispatch())
        executor = self.build_executor(service, close_lifecycle=True)

        self.assertEqual(1, executor.dispatch_once())
        executor.wait_for_workers()

        self.assertEqual((WORKER_ID, 7), service.completed_cycles[0])
        self.assertEqual([("RUN-0001", "cli-thread-1")], service.bindings)
        self.assertEqual([], service.failures)

    def test_dispatch_rewrites_cloud_lifecycle_paths_to_local_runtime(self):
        service = FakeService(None)
        executor = self.build_executor(service, close_lifecycle=True)
        dispatch = make_dispatch(
            dispatch_prompt=(
                "[$dotasks:dotasks-lifecycle](/app/skills/dotasks-lifecycle/SKILL.md)\n\n"
                "$dotasks-lifecycle\nBuild it\n\n"
                "DoTasks lifecycle CLI fallback: `/app/scripts/mcp-server`"
            )
        )

        localized = executor._localize_dispatch_prompt(dispatch)

        self.assertNotIn("/app/", localized["dispatch_prompt"])
        self.assertIn(
            str(executor.runtime_home / "skills/dotasks-lifecycle/SKILL.md"),
            localized["dispatch_prompt"],
        )
        self.assertIn(
            str(executor.runtime_home / "scripts/mcp-server"),
            localized["dispatch_prompt"],
        )
        self.assertIn("/app/", dispatch["dispatch_prompt"])

    def test_dispatch_downloads_server_visual_and_rewrites_prompt_path(self):
        service = FakeService(None)
        content = b"\x89PNG\r\n\x1a\nremote-visual"
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"artifact://visuals/{digest}.png"
        service.visual_artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "content_type": "image/png",
            "sha256": digest,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        executor = self.build_executor(service, close_lifecycle=True)
        dispatch = make_dispatch(
            dispatch_prompt=(
                "$dotasks-lifecycle\n"
                "RUN_CONTEXT_JSON={\"visual_references\":[{\"artifact_id\":\""
                + artifact_id
                + "\",\"path\":\"/app/data/missing.png\",\"sha256\":\""
                + digest
                + "\"}]}\n\n完成后上报"
            )
        )

        localized = executor._localize_dispatch_prompt(dispatch)

        self.assertNotIn("/app/data/missing.png", localized["dispatch_prompt"])
        self.assertEqual(1, len(localized["local_visual_paths"]))
        local_path = Path(localized["local_visual_paths"][0])
        self.assertEqual(content, local_path.read_bytes())
        executor._cleanup_dispatch_visuals(localized)
        self.assertFalse(local_path.exists())

    def test_completed_turn_without_callback_is_failed_and_wakes_next_cycle(self):
        service = FakeService(make_dispatch())
        executor = self.build_executor(service, close_lifecycle=False)

        executor.dispatch_once()
        executor.wait_for_workers()

        self.assertEqual([("RUN-0001", "missing callback")], service.failures)
        self.assertTrue(executor._wake_event.is_set())

    def test_existing_cli_thread_is_resumed_before_turn(self):
        service = FakeService(make_dispatch(thread_id="cli-thread-1"))
        clients: list[FakeClient] = []
        executor = self.build_executor(service, close_lifecycle=True)

        def factory(**kwargs):
            client = FakeClient(service, True, **kwargs)
            clients.append(client)
            return client

        executor.client_factory = factory
        executor.dispatch_once()
        executor.wait_for_workers()

        self.assertEqual("cli-thread-1", clients[0].resumed)
        self.assertEqual([("RUN-0001", "cli-thread-1")], service.bindings)

    def test_worktree_dispatch_uses_pinned_isolated_checkout(self):
        project_home = tempfile.TemporaryDirectory()
        self.addCleanup(project_home.cleanup)
        project = Path(project_home.name)
        subprocess.run(["git", "init", "-q", str(project)], check=True)
        subprocess.run(
            ["git", "-C", str(project), "config", "user.email", "test@example.com"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(project), "config", "user.name", "Test"], check=True
        )
        (project / "README.md").write_text("base\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(project), "add", "README.md"], check=True)
        subprocess.run(["git", "-C", str(project), "commit", "-qm", "base"], check=True)
        revision = subprocess.run(
            ["git", "-C", str(project), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        service = FakeService(None)
        executor = self.build_executor(service, close_lifecycle=True)

        checkout = executor._execution_path(
            make_dispatch(
                project_path=str(project),
                execution_environment="worktree",
                base_ref="HEAD",
                base_revision=revision,
            )
        )

        self.assertNotEqual(str(project), checkout)
        self.assertEqual(
            revision,
            subprocess.run(
                ["git", "-C", checkout, "rev-parse", "HEAD"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
        )

    def test_projectless_dispatch_uses_managed_non_project_workspace(self):
        service = FakeService(None)
        executor = self.build_executor(service, close_lifecycle=True)

        workspace = executor._execution_path(
            make_dispatch(
                entity_id="TASK-0042",
                project_path="",
                execution_environment="projectless",
            )
        )

        self.assertEqual(
            executor.data_home / "projectless-workspaces" / "TASK-0042",
            Path(workspace),
        )
        self.assertTrue(Path(workspace).is_dir())


if __name__ == "__main__":
    unittest.main()
