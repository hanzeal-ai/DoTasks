from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock

from tests import test_workflow as workflow
from taskboard.account_recovery import recover_account
from taskboard.cloud.accounts import AccountStore
from taskboard.execution_usage import TurnUsage, UsageOutbox
from taskboard.web_auth import WebSessions


def counters(value):
    return dict(token_used=value * 2, input_tokens=value, cached_input_tokens=0,
                output_tokens=value, reasoning_output_tokens=0)


def provider(value):
    from taskboard.execution_usage import FIELDS
    return {field: counters(value)[key] for key, field in FIELDS.items()}


class ManagerReliabilityTest(unittest.TestCase):
    def service_case(self):
        case = workflow.WorkflowTest()
        case.setUp()
        self.addCleanup(case.tearDown)
        return case

    def test_concurrent_review_retry_records_one_decision(self):
        case = self.service_case()
        task = case.task('concurrent callback')
        case.deliver(task)
        claim = case.service.claim_next_code_review_task('reviewer')
        run_id = claim['run']['id']
        case.service.bind_conversation(task['id'], 'code_review', 'dev', run_id)
        barrier = threading.Barrier(2)
        def submit(_):
            barrier.wait(timeout=5)
            return case.service.review_code(task['id'], run_id, 'fail',
                reasons=['Concrete defect'], failed_criteria=['project-rules'])
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(submit, range(2)))
        self.assertEqual(['rework', 'rework'], [result['status'] for result in results])
        self.assertEqual(1, len(case.service.list_reviews(task['id'])))
        self.assertEqual(1, case.service.get_task(task['id'])['review_rework_count'])
        with self.assertRaisesRegex(ValueError, 'different result'):
            case.service.review_code(task['id'], run_id, 'pass', passed_items=['project-rules'])

    def test_usage_is_turn_scoped_and_duplicate_callbacks_do_not_double_count(self):
        case = self.service_case()
        task = case.task('usage tracking')
        claim = case.service.claim_next_task('worker')
        run_id = claim['run']['id']
        case.service.bind_conversation(task['id'], 'execution', 'dev', run_id)
        report = lambda turn, value: case.service.record_execution_usage(run_id, 'dev', turn, value)
        report('turn1', counters(10))
        report('turn1', counters(10))
        report('turn1', counters(20))
        report('turn2', counters(5))
        self.assertEqual(50, case.service.get_task(task['id'])['token_used'])
        with self.assertRaises(ValueError):
            case.service.record_execution_usage(run_id, 'someone-else', 'turn1', counters(100))
        with self.assertRaises(ValueError):
            report('turn1', counters(1))
        self.assertEqual('reported', case.service.list_tasks()[0]['token_usage_status'])
        report('turn3', None)
        self.assertEqual('partial', case.service.list_tasks()[0]['token_usage_status'])
        self.assertEqual(50, case.service.get_task(task['id'])['token_used'])

    def test_resumed_provider_usage_excludes_previous_conversation(self):
        observer = TurnUsage(counters(100))
        self.assertIsNone(observer.observe({'total': provider(100), 'last': provider(20)}))
        self.assertEqual(counters(10), observer.observe({'total': provider(110), 'last': provider(10)}))
        self.assertIsNone(observer.observe({'total': provider(110), 'last': provider(10)}))
        self.assertEqual(counters(20), observer.observe({'total': provider(120), 'last': provider(10)}))
        with self.assertRaises(ValueError):
            observer.observe({'total': provider(110), 'last': provider(10)})
        with self.assertRaises(ValueError):
            TurnUsage().observe({'total': provider(110), 'last': provider(10)})
        self.assertEqual(counters(10), TurnUsage(counters(100)).observe({'total': provider(10), 'last': provider(10)}))

    def test_usage_upload_survives_disconnect_and_restart(self):
        with tempfile.TemporaryDirectory() as home:
            service = Mock()
            service.record_execution_usage.side_effect = ConnectionError()
            outbox = UsageOutbox(home, service)
            outbox.record('RUN-1', 'thread', 'turn', counters(10))
            outbox.record('RUN-1', 'thread', 'turn', None)
            outbox.flush()
            self.assertEqual(1, len(list(Path(home).rglob('*.json'))))
            service.record_execution_usage.side_effect = None
            UsageOutbox(home, service).flush()
            service.record_execution_usage.assert_called_with(run_id='RUN-1', thread_id='thread', turn_id='turn', usage=counters(10))
            self.assertEqual([], list(Path(home).rglob('*.json')))

    def test_slow_upload_does_not_block_recording(self):
        with tempfile.TemporaryDirectory() as home:
            entered, release, recorded = threading.Event(), threading.Event(), threading.Event()
            service = Mock()
            def upload(**payload):
                entered.set()
                release.wait(5)
            service.record_execution_usage.side_effect = upload
            outbox = UsageOutbox(home, service)
            outbox.record('RUN-1', 'thread', 'turn', counters(10))
            outbox.flush_async()
            self.assertTrue(entered.wait(5))
            worker = threading.Thread(target=lambda: (outbox.record('RUN-2', 'other', 'turn', counters(20)), recorded.set()))
            worker.start()
            try:
                self.assertTrue(recorded.wait(1), 'receipt lock was held during network I/O')
            finally:
                release.set()
                worker.join(5)
                self.assertTrue(outbox.upload_lock.acquire(timeout=5))
                outbox.upload_lock.release()

    def test_login_limit_does_not_block_an_unrelated_account(self):
        limiter = WebSessions()
        for _ in range(20):
            self.assertTrue(limiter.allow_login('proxy', 'alice'))
        self.assertFalse(limiter.allow_login('proxy', 'ALICE'))
        self.assertTrue(limiter.allow_login('proxy', 'bob'))
        self.assertTrue(limiter.allow_login('other-source', 'alice'))
        for i in range(179):
            self.assertTrue(limiter.allow_login('proxy', str(i)))
        self.assertFalse(limiter.allow_login('proxy', 'spray'))


class AccountRecoveryTest(unittest.TestCase):
    def test_offline_recovery_preserves_identity_and_rebinds_once(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            store = AccountStore(home)
            account, token = store.initialize('alice', 'old-password-long', 'a' * 32, 'a' * 48)
            session = store.create_session(account['id'])
            result = recover_account(home, 'alice', 'new-password-long', reset_device=True)
            self.assertEqual(account['id'], result['account_id'])
            self.assertIsNone(store.authenticate('alice', 'old-password-long'))
            self.assertIsNone(store.agent(token))
            self.assertIsNone(store.session(session))
            rebound, new_token = store.initialize('alice', 'new-password-long', 'b' * 32, 'b' * 48)
            self.assertEqual(account['id'], rebound['id'])
            self.assertEqual(account['id'], store.agent(new_token)['id'])
            self.assertEqual(rebound, store.initialize('alice', 'new-password-long', 'b' * 32, 'b' * 48)[0])
            with self.assertRaises(Exception):
                store.initialize('alice', 'new-password-long', 'c' * 32, 'c' * 48)

    def test_recovery_refuses_active_work_and_wrong_home(self):
        case = workflow.WorkflowTest()
        case.setUp()
        try:
            store = AccountStore(Path(case.temp.name))
            store.initialize('alice', 'old-password-long', 'a' * 32, 'a' * 48)
            case.task('active-work')
            case.service.claim_next_task('worker')
            with self.assertRaisesRegex(ValueError, 'Active work'):
                recover_account(Path(case.temp.name), 'alice', 'new-password-long')
            self.assertIsNotNone(store.authenticate('alice', 'old-password-long'))
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                recover_account(Path(case.temp.name) / 'wrong', 'alice', 'new-password-long')
        finally:
            case.tearDown()
