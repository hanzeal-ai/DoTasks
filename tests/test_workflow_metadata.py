from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from core.db import Database
from core.service.domain import JSON_FIELDS, decode_row
from core.workflow import (
    AUTO_DISPATCH_ATTENTION_STATUSES,
    RUN_TYPES,
    STATUS_LABELS,
    TASK_STATUSES,
    TOKEN_STAGES,
    task_requires_attention,
    workflow_metadata,
)


class WorkflowMetadataTest(unittest.TestCase):
    def test_row_decoder_treats_equivalent_default_field_sets_consistently(self) -> None:
        decoded = decode_row(
            {"modules": "[]", "location_context": '{"targets": []}'},
            set(JSON_FIELDS),
        )
        self.assertEqual([], decoded["modules"])
        self.assertEqual({"targets": []}, decoded["location_context"])

    def test_frontend_metadata_matches_domain_vocabulary(self) -> None:
        metadata = workflow_metadata()
        self.assertEqual(TASK_STATUSES, frozenset(STATUS_LABELS))
        self.assertTrue({status for column in metadata["columns"] for status in column["statuses"]} <= TASK_STATUSES)
        self.assertEqual(RUN_TYPES, frozenset(stage["key"] for stage in TOKEN_STAGES))
        self.assertEqual(
            AUTO_DISPATCH_ATTENTION_STATUSES,
            frozenset(metadata["attention_auto_dispatch_statuses"]),
        )

    def test_attention_includes_paused_automatic_stages(self) -> None:
        self.assertTrue(task_requires_attention({"status": "code_review", "auto_dispatch": 0}))
        self.assertTrue(task_requires_attention({"status": "failed", "auto_dispatch": 1}))
        self.assertFalse(task_requires_attention({"status": "code_review", "auto_dispatch": 1}))
        self.assertFalse(task_requires_attention({"status": "done", "auto_dispatch": 0}))
        self.assertFalse(task_requires_attention({"status": "cancelled", "auto_dispatch": 0}))

    def test_current_healthy_database_skips_full_migration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "taskboard.db"
            Database(path)
            with mock.patch.object(Database, "_migrate") as migrate:
                Database(path)
            migrate.assert_not_called()

    def test_database_triggers_use_canonical_workflow_values(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            database = Database(Path(temporary) / "taskboard.db")
            with database.connection() as connection:
                task_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='validate_task_update'"
                ).fetchone()["sql"]
                run_sql = connection.execute(
                    "SELECT sql FROM sqlite_master WHERE type='trigger' AND name='validate_run_update'"
                ).fetchone()["sql"]
            for status in TASK_STATUSES:
                self.assertIn(f"'{status}'", task_sql)
            for run_type in RUN_TYPES:
                self.assertIn(f"'{run_type}'", run_sql)


if __name__ == "__main__":
    unittest.main()
