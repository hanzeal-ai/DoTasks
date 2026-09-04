from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import call, patch

from taskboard.app_server import (
    AppServerError,
    CodexAppServerClient,
    LIFECYCLE_TOOLS,
    sync_worker_thread_to_shared_home,
)


class WorkerSessionVisibilityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.worker = root / "worker"
        self.shared = root / "shared"
        self.thread_id = "01a0660b-0723-7562-9eb1-210793a69ce3"
        self.session = (
            self.worker
            / "sessions"
            / "2026"
            / "09"
            / "03"
            / f"rollout-2026-09-03T14-53-13-{self.thread_id}.jsonl"
        )
        self.session.parent.mkdir(parents=True)
        self.session.write_text('{"type":"session_meta"}\n', encoding="utf-8")
        self.worker.mkdir(exist_ok=True)
        (self.worker / "session_index.jsonl").write_text(
            json.dumps(
                {
                    "id": self.thread_id,
                    "thread_name": "[DoTasks] REQ-0003 需求拆解",
                    "updated_at": "2026-09-03T06:53:14Z",
                },
                ensure_ascii=False,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_completed_worker_session_is_copied_and_indexed(self) -> None:
        destination = sync_worker_thread_to_shared_home(
            self.worker, self.shared, self.thread_id
        )

        self.assertEqual(self.session.read_bytes(), destination.read_bytes())
        record = json.loads((self.shared / "session_index.jsonl").read_text())
        self.assertEqual(self.thread_id, record["id"])
        self.assertEqual("[DoTasks] REQ-0003 需求拆解", record["thread_name"])

    def test_title_is_indexed_before_session_becomes_visible(self) -> None:
        original_replace = os.replace
        published_after_index = []

        def observed_replace(source, destination):
            destination_path = Path(destination)
            if destination_path.name.endswith(f"-{self.thread_id}.jsonl"):
                index = self.shared / "session_index.jsonl"
                published_after_index.append(
                    index.is_file() and self.thread_id in index.read_text(encoding="utf-8")
                )
            return original_replace(source, destination)

        with patch("taskboard.app_server.os.replace", side_effect=observed_replace):
            sync_worker_thread_to_shared_home(
                self.worker, self.shared, self.thread_id
            )

        self.assertEqual([True], published_after_index)

    def test_sync_refreshes_snapshot_without_duplicating_unchanged_index(self) -> None:
        sync_worker_thread_to_shared_home(
            self.worker, self.shared, self.thread_id
        )
        self.session.write_text(
            '{"type":"session_meta"}\n{"type":"task_complete"}\n',
            encoding="utf-8",
        )

        destination = sync_worker_thread_to_shared_home(
            self.worker, self.shared, self.thread_id
        )

        self.assertIn("task_complete", destination.read_text(encoding="utf-8"))
        records = (self.shared / "session_index.jsonl").read_text().splitlines()
        self.assertEqual(1, len(records))

    def test_invalid_thread_id_is_rejected(self) -> None:
        with self.assertRaisesRegex(AppServerError, "invalid Codex thread id"):
            sync_worker_thread_to_shared_home(
                self.worker, self.shared, "../outside"
            )

    def test_app_server_client_syncs_every_tracked_thread(self) -> None:
        client = CodexAppServerClient(
            self.temporary.name,
            Path(__file__).resolve().parents[1],
            executable="codex",
        )
        client.worker_codex_home = self.worker
        client.shared_codex_home = self.shared
        client._threads_to_sync[self.thread_id] = "[DoTasks] REQ-0003 需求拆解"

        client._sync_completed_threads()

        synchronized = list((self.shared / "sessions").glob("**/*.jsonl"))
        self.assertEqual(1, len(synchronized))
        self.assertEqual(self.session.read_bytes(), synchronized[0].read_bytes())

    def test_resume_thread_refreshes_visible_title(self) -> None:
        client = CodexAppServerClient(
            self.temporary.name,
            Path(__file__).resolve().parents[1],
            executable="codex",
        )
        title = "[DoTasks] TASK-0006 开发"
        with patch.object(
            client,
            "request",
            side_effect=[{"thread": {"id": self.thread_id}}, {}],
        ) as request:
            resumed_id = client.resume_thread(self.thread_id, title)

        self.assertEqual(self.thread_id, resumed_id)
        self.assertEqual(
            [
                call("thread/resume", {"threadId": self.thread_id}),
                call(
                    "thread/name/set",
                    {"threadId": self.thread_id, "name": title},
                ),
            ],
            request.call_args_list,
        )
        self.assertEqual(title, client._threads_to_sync[self.thread_id])


class LifecycleApprovalTest(unittest.TestCase):
    def test_location_analysis_tools_are_approved_for_dotasks_workers(self) -> None:
        self.assertTrue(
            {
                "prepare_task_location",
                "report_location_status",
                "complete_location_analysis",
            }.issubset(LIFECYCLE_TOOLS)
        )
        client = CodexAppServerClient(
            tempfile.gettempdir(),
            Path(__file__).resolve().parents[1],
            executable="codex",
        )
        responses: list[dict] = []
        client._write = responses.append  # type: ignore[method-assign]

        client._respond_to_server_request(
            {
                "id": 9,
                "method": "mcpServer/elicitation/request",
                "params": {
                    "serverName": "dotasks",
                    "message": 'Allow MCP server "dotasks" to run tool "prepare_task_location"?',
                    "_meta": {"codex_approval_kind": "mcp_tool_call"},
                },
            }
        )

        self.assertEqual(
            {"id": 9, "result": {"action": "accept", "content": {}}},
            responses[0],
        )

    def test_turn_start_attaches_local_images_as_user_input(self) -> None:
        root = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: root.rmdir())
        image = root / "reference.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\nreference")
        self.addCleanup(image.unlink)
        client = CodexAppServerClient(
            tempfile.gettempdir(),
            Path(__file__).resolve().parents[1],
            executable="codex",
        )
        requests: list[tuple[str, dict]] = []

        def request(method: str, params: dict) -> dict:
            requests.append((method, params))
            return {"turn": {"id": "turn-1"}}

        client.request = request  # type: ignore[method-assign]

        self.assertEqual(
            "turn-1",
            client.start_turn("thread-1", "处理这张图片", [str(image)]),
        )

        self.assertEqual("turn/start", requests[0][0])
        self.assertEqual(
            [
                {"type": "text", "text": "处理这张图片"},
                {"type": "localImage", "path": str(image.resolve())},
            ],
            requests[0][1]["input"],
        )


if __name__ == "__main__":
    unittest.main()
