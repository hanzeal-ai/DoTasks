from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from taskboard.app_server import (
    AppServerError,
    CodexAppServerClient,
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


if __name__ == "__main__":
    unittest.main()
