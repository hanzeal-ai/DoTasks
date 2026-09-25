import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from core.service import TaskboardService
from core.service.domain import insert_relation


class ReviewRegressions(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'DOTASKS_OBSIDIAN_VAULT': str(self.root / 'vault')})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.service = TaskboardService(self.root)

    def test_all_relation_entry_points_reject_mixed_cycles(self):
        for first, second in [('continues_from', 'depends_on'), ('depends_on', 'continues_from')]:
            for public in (True, False):
                with self.subTest(first=first, public=public):
                    with self.service.db.transaction() as db:
                        db.execute('DELETE FROM task_relations')
                        for task_id in ('A', 'B'):
                            db.execute("INSERT OR IGNORE INTO tasks(id,title,status) VALUES(?,?,'ready')", (task_id, task_id))
                    self.service.add_relation('A', 'B', first)
                    with self.assertRaisesRegex(ValueError, 'cycle'):
                        if public:
                            self.service.add_relation('B', 'A', second)
                        else:
                            with self.service.db.transaction() as db:
                                insert_relation(db, 'B', 'A', second)

    def test_late_enqueue_remains_pending_after_cycle_completes(self):
        self.service.set_dispatcher_enabled(True)
        original = self.service._claim_native_dispatch_batch
        def enqueue():
            with self.service.db.transaction() as db:
                db.execute("INSERT INTO tasks(id,title,status) VALUES('LATE','late','ready')")
        def batch(worker, project, lease, stage):
            result = original(worker, project, lease, stage)
            if stage == 'development':
                with ThreadPoolExecutor(max_workers=1) as pool:
                    pool.submit(enqueue).result(timeout=3)
            return result
        with patch.object(self.service, '_claim_native_dispatch_batch', side_effect=batch):
            cycle = self.service.claim_schedule_cycle('test')
        current = self.service.complete_schedule_cycle('test', cycle['cycle_generation'])
        self.assertTrue(current['pending'])
        self.assertLess(current['handled_generation'], current['generation'])
        with patch.object(self.service, '_claim_native_dispatch_batch', return_value={'dispatches': []}):
            self.assertEqual(self.service.claim_schedule_cycle('test')['status'], 'claimed')

    def test_outbox_dispatches_only_after_outer_commit(self):
        for rollback in (True, False):
            with self.subTest(rollback=rollback):
                try:
                    with self.service.db.atomic() as db:
                        db.execute("INSERT INTO experiences(id,title,content) VALUES('EXP-TEST','saved','content')")
                        self.service._queue_obsidian_sync(db, 'experience', 'EXP-TEST')
                        self.service.flush_integration_outbox()
                        self.assertFalse(list(self.root.rglob('EXP-TEST*')))
                        if rollback:
                            raise RuntimeError('rollback')
                except RuntimeError:
                    if not rollback:
                        raise
                self.assertEqual(bool(list(self.root.rglob('EXP-TEST*'))), not rollback)

    def test_post_commit_failure_keeps_success_and_pending_outbox(self):
        with self.assertLogs('core.db', level='ERROR'):
            with self.service.db.atomic() as db:
                db.execute("INSERT INTO experiences(id,title,content) VALUES('EXP-FAIL','saved','content')")
                self.service._queue_obsidian_sync(db, 'experience', 'EXP-FAIL')
                self.service.flush_integration_outbox()
                original = self.service.db.connection
                failing = patch.object(self.service.db, 'connection', side_effect=RuntimeError('unavailable after commit'))
                failing.start()
        failing.stop()
        with original() as db:
            self.assertIsNotNone(db.execute("SELECT id FROM experiences WHERE id='EXP-FAIL'").fetchone())
            self.assertEqual(db.execute("SELECT status FROM integration_outbox WHERE entity_id='EXP-FAIL'").fetchone()[0], 'pending')
        self.service.flush_integration_outbox()
        self.assertTrue(list(self.root.rglob('EXP-FAIL*')))
