from __future__ import annotations

import base64
import hashlib
import json
import tempfile
import subprocess
import unittest
from pathlib import Path

from taskboard.local_executor import (
    MAX_PERMISSION_RECOVERY_ATTEMPTS,
    PERMISSION_RECOVERY_FAILED,
    LocalCodexExecutor,
    WORKER_ID,
)


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
        self.started_image_paths = []

    def start(self):
        pass

    def stop(self):
        self.connected = False

    def start_thread(self, project_path, title):
        self.created = (project_path, title)
        return "cli-thread-1"

    def resume_thread(self, thread_id, title=""):
        self.resumed = (thread_id, title)
        return thread_id

    def start_turn(self, thread_id, prompt, image_paths=None):
        self.started_prompt = prompt
        self.started_image_paths = list(image_paths or [])
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
        "entity_id": "TASK-0001",
        "role": "execution",
        "dispatch_attempt_id": "attempt-1",
        "project_path": "/tmp/project",
        "dispatch_title": "[DoTaks] TASK-0001 开发",
        "dispatch_prompt": "Build it",
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

    def test_dispatch_removes_legacy_skill_and_localizes_callback_path(self):
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
        self.assertNotIn("dotasks-lifecycle", localized["dispatch_prompt"])
        self.assertIn(
            str(executor.runtime_home / "scripts/mcp-server"),
            localized["dispatch_prompt"],
        )
        self.assertEqual("[DoTasks] TASK-0001 开发", localized["dispatch_title"])
        self.assertIn("/app/", dispatch["dispatch_prompt"])

    def test_review_dispatch_uses_visible_review_title_contract(self):
        service = FakeService(None)
        executor = self.build_executor(service, close_lifecycle=True)

        localized = executor._localize_dispatch_prompt(
            make_dispatch(
                entity_id="TASK-0005",
                role="code_review",
                dispatch_title="[DoTaks] TASK-0005 Code Review",
                dispatch_prompt="$dotasks-lifecycle\nReview it",
            )
        )

        self.assertEqual("[DoTasks] TASK-0005 Review", localized["dispatch_title"])
        self.assertNotIn("$dotasks-lifecycle", localized["dispatch_prompt"])

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
        self.assertEqual(localized["local_visual_paths"], localized["input_image_paths"])
        local_path = Path(localized["local_visual_paths"][0])
        self.assertEqual(content, local_path.read_bytes())
        executor._cleanup_dispatch_visuals(localized)
        self.assertFalse(local_path.exists())

    def test_files_and_audio_are_materialized_without_image_inputs(self):
        for marker, filename, content_type, content in [
            ("RUN_CONTEXT_JSON=", "scope.md", "text/markdown", b"# Scope"),
            ("REQUIREMENT_VISUAL_REFERENCES_JSON=", "voice.webm", "audio/webm", b"audio bytes"),
        ]:
            with self.subTest(filename=filename):
                service = FakeService(None)
                digest = hashlib.sha256(content).hexdigest()
                artifact_id = f"artifact://attachments/{digest}{Path(filename).suffix}"
                service.visual_artifacts[artifact_id] = {"artifact_id": artifact_id, "content_type": content_type,
                    "sha256": digest, "content_base64": base64.b64encode(content).decode()}
                references = [{"artifact_id": artifact_id, "path": "/missing/" + filename, "sha256": digest}]
                payload = {"visual_references": references} if marker == "RUN_CONTEXT_JSON=" else references
                executor = self.build_executor(service, close_lifecycle=True)
                dispatch = make_dispatch(dispatch_prompt=marker + json.dumps(payload))
                localized = executor._localize_dispatch_prompt(dispatch)
                self.assertEqual([], localized["input_image_paths"])
                path = Path(localized["local_visual_paths"][0])
                self.assertEqual(content, path.read_bytes())
                self.assertIn(str(path), localized["dispatch_prompt"])
                # The same bytes must remain a file input when already local.
                again = executor._localize_dispatch_prompt(localized)
                self.assertEqual([], again["input_image_paths"])
                executor._cleanup_dispatch_visuals(localized)
                self.assertFalse(path.exists())
                service.visual_artifacts[artifact_id]["sha256"] = "bad-checksum"
                with self.assertRaises(ValueError):
                    executor._localize_dispatch_prompt(dispatch)

    def test_duplicate_active_dispatch_preserves_worker_visual(self):
        content = b"\x89PNG\r\n\x1a\nactive-worker-visual"
        digest = hashlib.sha256(content).hexdigest()
        service = FakeService(None)
        executor = self.build_executor(service, close_lifecycle=True)
        local_path = (
            executor.data_home
            / "artifacts"
            / "dispatch-visuals"
            / "RUN-0001"
            / f"{digest}.png"
        )
        local_path.parent.mkdir(parents=True)
        local_path.write_bytes(content)
        service.dispatch = make_dispatch(
            dispatch_prompt=(
                "$dotasks-lifecycle\n"
                "RUN_CONTEXT_JSON={\"visual_references\":[{\"path\":\""
                + str(local_path)
                + "\",\"sha256\":\""
                + digest
                + "\"}]}\n\n完成后上报"
            )
        )
        executor._active_runs.add("RUN-0001")

        self.assertEqual(0, executor.dispatch_once())

        self.assertEqual(content, local_path.read_bytes())
        executor._cleanup_dispatch_visuals({"local_visual_paths": [str(local_path)]})

    def test_dispatch_attaches_server_visual_to_codex_turn(self):
        content = b"\x89PNG\r\n\x1a\nturn-input-visual"
        digest = hashlib.sha256(content).hexdigest()
        artifact_id = f"artifact://visuals/{digest}.png"
        service = FakeService(
            make_dispatch(
                dispatch_prompt=(
                    "$dotasks-lifecycle\n"
                    "RUN_CONTEXT_JSON={\"visual_references\":[{\"artifact_id\":\""
                    + artifact_id
                    + "\",\"path\":\"/app/data/missing.png\",\"sha256\":\""
                    + digest
                    + "\"}]}\n\n完成后上报"
                )
            )
        )
        service.visual_artifacts[artifact_id] = {
            "artifact_id": artifact_id,
            "content_type": "image/png",
            "sha256": digest,
            "content_base64": base64.b64encode(content).decode("ascii"),
        }
        clients: list[FakeClient] = []
        executor = self.build_executor(service, close_lifecycle=True)

        def factory(**kwargs):
            client = FakeClient(service, True, **kwargs)
            clients.append(client)
            return client

        executor.client_factory = factory

        self.assertEqual(1, executor.dispatch_once())
        executor.wait_for_workers()

        self.assertEqual(1, len(clients[0].started_image_paths))
        self.assertTrue(clients[0].started_image_paths[0].endswith(f"{digest}.png"))

    def test_completed_turn_without_callback_is_failed_and_wakes_next_cycle(self):
        service = FakeService(make_dispatch())
        executor = self.build_executor(service, close_lifecycle=False)

        executor.dispatch_once()
        executor.wait_for_workers()

        self.assertEqual([("RUN-0001", "missing callback")], service.failures)
        self.assertTrue(executor._wake_event.is_set())

    def test_permission_drift_recovers_same_thread_and_preserves_images(self):
        service = FakeService(None)
        executor = self.build_executor(service, close_lifecycle=False)
        image = (
            executor.data_home
            / "artifacts"
            / "dispatch-visuals"
            / "RUN-0001"
            / "ref.png"
        )
        image.parent.mkdir(parents=True)
        image.write_bytes(b"reference")

        class RecoveringClient(FakeClient):
            def __init__(self, **kwargs):
                super().__init__(service, False, **kwargs)
                self.turns: list[tuple[str, str, list[str], list[bool]]] = []
                self.interruptions: list[tuple[str, str]] = []

            def start_turn(self, thread_id, prompt, image_paths=None):
                paths = list(image_paths or [])
                self.turns.append(
                    (thread_id, prompt, paths, [Path(path).is_file() for path in paths])
                )
                return f"turn-{len(self.turns)}"

            def wait_notification(self, timeout):
                if len(self.turns) == 1:
                    return {
                        "method": "thread/settings/updated",
                        "params": {
                            "threadId": "cli-thread-1",
                            "threadSettings": {
                                "sandboxPolicy": {
                                    "type": "workspaceWrite",
                                    "writableRoots": ["/tmp/project"],
                                    "networkAccess": False,
                                }
                            },
                        },
                    }
                service.status = "completed"
                return {
                    "method": "turn/completed",
                    "params": {
                        "threadId": "cli-thread-1",
                        "turn": {"id": "turn-2", "status": "completed"},
                    },
                }

            def interrupt_turn(self, thread_id, turn_id):
                self.interruptions.append((thread_id, turn_id))

        clients: list[RecoveringClient] = []

        def factory(**kwargs):
            client = RecoveringClient(**kwargs)
            clients.append(client)
            return client

        executor.client_factory = factory
        self.assertTrue(
            executor._launch(
                make_dispatch(
                    input_image_paths=[str(image)],
                    local_visual_paths=[str(image)],
                )
            )
        )
        executor.wait_for_workers()

        client = clients[0]
        self.assertEqual(2, len(client.turns))
        self.assertEqual("cli-thread-1", client.turns[0][0])
        self.assertEqual("cli-thread-1", client.turns[1][0])
        self.assertEqual([str(image)], client.turns[0][2])
        self.assertEqual([str(image)], client.turns[1][2])
        self.assertEqual([True], client.turns[0][3])
        self.assertEqual([True], client.turns[1][3])
        self.assertEqual([("cli-thread-1", "turn-1")], client.interruptions)
        self.assertEqual([], service.failures)
        self.assertFalse(image.exists())

    def test_repeated_permission_drift_fails_with_structured_reason(self):
        service = FakeService(make_dispatch())

        class DriftingClient(FakeClient):
            def __init__(self, **kwargs):
                super().__init__(service, False, **kwargs)
                self.turn_count = 0

            def start_turn(self, thread_id, prompt, image_paths=None):
                self.turn_count += 1
                return f"turn-{self.turn_count}"

            def wait_notification(self, timeout):
                return {
                    "method": "thread/settings/updated",
                    "params": {
                        "threadId": "cli-thread-1",
                        "threadSettings": {
                            "sandboxPolicy": {
                                "type": "workspaceWrite",
                                "writableRoots": ["/tmp/project"],
                                "networkAccess": False,
                            }
                        },
                    },
                }

            def interrupt_turn(self, thread_id, turn_id):
                pass

        clients: list[DriftingClient] = []
        executor = self.build_executor(service, close_lifecycle=False)

        def factory(**kwargs):
            client = DriftingClient(**kwargs)
            clients.append(client)
            return client

        executor.client_factory = factory

        executor.dispatch_once()
        executor.wait_for_workers()

        self.assertEqual(MAX_PERMISSION_RECOVERY_ATTEMPTS + 1, clients[0].turn_count)
        self.assertIn(PERMISSION_RECOVERY_FAILED, service.failures[0][1])

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

        self.assertEqual(
            ("cli-thread-1", "[DoTasks] TASK-0001 开发"), clients[0].resumed
        )
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
