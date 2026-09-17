from __future__ import annotations

import tempfile
import threading
import unittest
from unittest.mock import patch
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

from core.service import TaskboardService
from taskboard.mobile_worker import execute_message, rollout_usage, claim_message


class MobileTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.service = TaskboardService(self.temp.name)
        self.service.set_dispatcher_enabled(True)
        with self.service.db.transaction() as db:
            for suffix in ('a', 'b'):
                db.execute("INSERT INTO tasks(id,title,status,project) VALUES(?,?,'blocked','/project')", ('task-' + suffix, suffix))
                db.execute("INSERT INTO task_runs(id,task_id,run_type,attempt,status,lease_token,lease_expires_at) VALUES(?,?,'execution',1,'completed','lease','2099-01-01')", ('run-' + suffix, 'task-' + suffix))
                db.execute("INSERT INTO task_run_conversations(run_id,task_id,role,thread_id) VALUES(?,?,'execution',?)", ('run-' + suffix, 'task-' + suffix, 'thread-' + suffix))
                db.execute("""INSERT INTO native_dispatches(run_id,entity_type,entity_id,role,status,worker_id,dispatch_title,dispatch_prompt,thread_id,host_id,codex_project_id,dispatch_attempt_id)
                   VALUES(?,'task',?,'execution','completed','worker','title','prompt',?,'mac','codex-cli-app-server','attempt')""",
                           ('run-' + suffix, 'task-' + suffix, 'thread-' + suffix))

    def tearDown(self):
        self.temp.cleanup()

    def queue(self, ident='message_0000000001', task='task-a', thread='thread-a'):
        return self.service.enqueue_mobile_message(ident, task, thread, 'continue')

    def test_exact_thread_and_idempotency(self):
        self.queue()
        self.assertEqual(self.queue()['status'], 'queued')
        with self.assertRaises(ValueError): self.queue(thread='thread-b')
        with self.assertRaises(ValueError): self.queue(task='task-b', thread='thread-b')
        with self.assertRaises(ValueError): self.service.enqueue_mobile_message('bad', 'task-a', 'thread-a', 'x')
        with self.assertRaises(ValueError): self.service.enqueue_mobile_message('message_0000000002', 'task-a', 'thread-a', '')

    def test_concurrent_claims_project_lock_and_owner(self):
        self.queue()
        self.queue('message_0000000002', 'task-b', 'thread-b')
        with ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda worker: self.service.claim_mobile_message(worker, 'mac'), ['one', 'two']))
        claimed = [r for r in results if r]
        self.assertEqual(len(claimed), 1)
        first = claimed[0]
        self.assertEqual(first['id'], 'message_0000000001')
        with self.assertRaises(ValueError): self.service.update_mobile_message(first['id'], 'not-owner', 'failed')
        self.service.update_mobile_message(first['id'], first['worker_id'], 'running', 'turn-1')
        self.service.update_mobile_message(first['id'], first['worker_id'], 'uncertain', 'turn-1')
        self.assertIsNone(self.service.claim_mobile_message('three', 'mac'))
        reopened = TaskboardService(self.temp.name)
        self.assertIsNone(reopened.claim_mobile_message('three', 'mac'))

    def test_paused_dispatcher_wrong_host_and_active_native_queue(self):
        self.queue()
        self.assertIsNone(self.service.claim_mobile_message('worker', 'other-mac'))
        self.service.set_dispatcher_enabled(False)
        self.assertIsNone(self.service.claim_mobile_message('worker', 'mac'))
        self.service.set_dispatcher_enabled(True)
        with self.service.db.transaction() as db:
            db.execute("UPDATE native_dispatches SET status='bound' WHERE run_id='run-b'")
        self.assertIsNone(self.service.claim_mobile_message('worker', 'mac'))

    def test_completion_releases_reservation_and_notifies_correct_thread(self):
        self.queue()
        cursor = self.service.mobile_events()['cursor']
        self.service.claim_mobile_message('worker', 'mac')
        with self.service.db.transaction() as db:
            db.execute("UPDATE tasks SET status='ready' WHERE id='task-b'")
            blockers = self.service._development_dispatch_blockers(db, dict(db.execute("SELECT * FROM tasks WHERE id='task-b'").fetchone()), dispatcher_enabled=True)
            self.assertEqual(blockers[0]['code'], 'mobile_conversation_active')
        self.service.update_mobile_message('message_0000000001', 'worker', 'running', 'turn')
        self.service.update_mobile_message('message_0000000001', 'worker', 'completed', 'turn', 'answer')
        events = self.service.mobile_events(cursor)['items']
        self.assertEqual(events[-1]['thread_id'], 'thread-a')
        self.assertEqual(events[-1]['summary'], 'answer')
        self.assertEqual(events[-1]['task_id'], 'task-a')
        with self.service.db.connection() as db:
            self.assertFalse(self.service._mobile_task_locked(db, 'task-b'))

    def test_since_recovers_events_after_subscription(self):
        self.queue()
        self.assertTrue(self.service.mobile_events(since='2020-01-01T00:00:00Z')['items'])
        with self.assertRaises(ValueError): self.service.mobile_events(after=-1)
        with self.assertRaises(ValueError): self.service.mobile_events(since='invalid')

    def test_restart_marks_claimed_work_uncertain_without_replaying(self):
        self.queue()
        self.service.claim_mobile_message('worker', 'mac')
        self.assertEqual(self.service.recover_mobile_messages('worker'), {'uncertain': 1})
        self.assertEqual(self.service.recover_mobile_messages('worker'), {'uncertain': 0})
        self.assertIsNone(self.service.claim_mobile_message('worker', 'mac'))

    def test_operator_resolution_requires_paused_dispatcher_exact_turn_and_evidence(self):
        self.queue()
        self.service.claim_mobile_message('worker', 'mac')
        self.service.update_mobile_message('message_0000000001', 'worker', 'uncertain')
        args = ('message_0000000001', 'thread-a', '', 'Verified original turn stopped in executor log')
        with self.assertRaises(ValueError):
            self.service.resolve_mobile_message(*args)
        self.service.set_dispatcher_enabled(False)
        with self.assertRaises(ValueError):
            self.service.resolve_mobile_message(args[0], 'thread-b', '', args[3])
        with self.assertRaises(ValueError):
            self.service.resolve_mobile_message(*args[:3], '')
        self.service.resolve_mobile_message(*args)
        self.service.set_dispatcher_enabled(True)
        self.assertIsNone(self.service.claim_mobile_message('worker', 'mac'))
        with self.service.db.connection() as db:
            self.assertFalse(self.service._mobile_task_locked(db, 'task-a'))

    def test_lost_claim_response_recovers_after_connectivity_returns(self):
        self.queue()
        owner = self.service
        class LostReply:
            lost = False
            def call(self, action, arguments):
                if action == 'recover':
                    return owner.recover_mobile_messages(**arguments)
                result = owner.claim_mobile_message(**arguments)
                if not self.lost:
                    self.lost = True
                    raise ConnectionError('response lost after commit')
                return result
        remote = LostReply()
        with self.assertRaises(ConnectionError):
            claim_message(remote, 'worker', 'mac')
        self.assertIsNone(claim_message(remote, 'worker', 'mac'))
        with self.service.db.connection() as db:
            self.assertEqual(db.execute('SELECT status FROM mobile_messages').fetchone()[0], 'uncertain')

    def test_mobile_usage_is_idempotent_and_exhausts_original_task_budget(self):
        with self.service.db.transaction() as db:
            db.execute("UPDATE tasks SET token_budget=5 WHERE id='task-a'")
        self.queue()
        self.service.claim_mobile_message('worker', 'mac')
        self.service.update_mobile_message('message_0000000001', 'worker', 'running', 'turn')
        usage = {'token_used': 6, 'input_tokens': 4, 'cached_input_tokens': 0, 'output_tokens': 2, 'reasoning_output_tokens': 0}
        with self.assertRaises(ValueError):
            self.service.record_mobile_usage('message_0000000001', 'other', 'turn', usage)
        for _ in range(2):
            self.assertTrue(self.service.record_mobile_usage('message_0000000001', 'worker', 'turn', usage)['exhausted'])
        self.assertEqual(self.service.get_task('task-a')['token_used'], 6)
        self.assertEqual(self.queue('message_0000000002')['status'], 'budget_exceeded')

    def test_uncertain_recovery_settles_unreported_usage_before_unlock(self):
        self.queue()
        self.service.claim_mobile_message('worker', 'mac')
        baseline = {'token_used':100, 'input_tokens':80, 'cached_input_tokens':0, 'output_tokens':20, 'reasoning_output_tokens':0}
        self.service.prepare_mobile_turn('message_0000000001', 'worker', baseline)
        self.service.update_mobile_message('message_0000000001', 'worker', 'running', 'turn')
        self.service.update_mobile_message('message_0000000001', 'worker', 'uncertain', 'turn')
        self.service.set_dispatcher_enabled(False)
        args = ('message_0000000001', 'thread-a', 'turn', 'Verified original turn stopped in executor log')
        with self.assertRaises(ValueError):
            self.service.resolve_mobile_message(*args)
        final = {**baseline, 'token_used':130, 'input_tokens':100, 'output_tokens':30}
        self.service.resolve_mobile_message(*args, final_usage=final)
        self.assertEqual(self.service.get_task('task-a')['token_used'], 30)

    def test_delivery_and_review_events_keep_original_run_results(self):
        with self.service.db.transaction() as db:
            db.execute("UPDATE task_runs SET delivery_summary='original delivery',verification_result='verified' WHERE id='run-a'")
            self.service._event(db, 'run', 'run-a', 'delivery_submitted', {'task_id':'task-a'})
            self.service._event(db, 'task', 'task-a', 'code_reviewed', {'run_id':'run-a','verdict':'pass','next_stage':'done','reasons':[]})
        events = self.service.mobile_events(0)['items']
        self.assertEqual(events[-2]['summary'], 'original delivery\nverified')
        self.assertEqual(events[-2]['thread_id'], 'thread-a')
        self.assertEqual(events[-1]['thread_id'], 'thread-a')
        self.assertIn('done', events[-1]['summary'])

    def test_additive_v22_migration_preserves_business_rows(self):
        with self.service.db.transaction() as db:
            before = [tuple(row) for row in db.execute('SELECT * FROM tasks ORDER BY id')]
            db.execute('DROP TABLE mobile_messages')
            db.execute('PRAGMA user_version=22')
        upgraded = TaskboardService(self.temp.name)
        with upgraded.db.connection() as db:
            self.assertEqual(before, [tuple(row) for row in db.execute('SELECT * FROM tasks ORDER BY id')])
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 23)
        self.assertEqual(upgraded.enqueue_mobile_message('migration_test_001', 'task-a', 'thread-a', 'hi')['status'], 'queued')


class FakeService:
    def __init__(self): self.updates = []
    def call(self, action, arguments):
        self.updates.append((action, arguments))
        return {'exhausted': False}


class FakeClient:
    connected = True
    def __init__(self, wrong=False, timeout=False):
        self.requests = []; self.wrong = wrong; self.timeout = timeout
        self.events = [
            {'method': 'thread/tokenUsage/updated', 'params': {'threadId': 'thread-a', 'turnId': 'turn', 'tokenUsage': {'total': {'totalTokens': 3, 'inputTokens': 2, 'cachedInputTokens': 0, 'outputTokens': 1, 'reasoningOutputTokens': 0}}}},
            {'method': 'item/completed', 'params': {'threadId': 'other', 'turnId': 'turn', 'item': {'id': 'bad', 'type': 'agentMessage', 'text': 'wrong task'}}},
            {'method': 'item/completed', 'params': {'threadId': 'thread-a', 'turnId': 'turn', 'item': {'id': 'good', 'type': 'agentMessage', 'text': 'correct answer'}}},
            {'method': 'turn/completed', 'params': {'threadId': 'thread-a', 'turn': {'id': 'turn', 'status': 'completed'}}},
        ]
        usage = self.events[0]['params']['tokenUsage']
        usage['last'] = dict(usage['total'])
    def start(self): pass
    def stop(self): pass
    def request(self, method, arguments):
        self.requests.append((method, arguments))
        if method == 'thread/resume': return {'thread': {'id': 'wrong' if self.wrong else 'thread-a'}}
        if self.timeout: raise TimeoutError()
        return {'turn': {'id': 'turn'}}
    def wait_notification(self, timeout): return self.events.pop(0)
    def interrupt_turn(self, *args): self.requests.append(('interrupt', args))


class WorkerTest(unittest.TestCase):
    def setUp(self):
        self.baseline = patch('taskboard.mobile_worker.rollout_usage', return_value={key: 0 for key in ('token_used','input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens')})
        self.baseline.start()
        self.addCleanup(self.baseline.stop)

    message = {'id': 'msg', 'worker_id': 'worker', 'thread_id': 'thread-a', 'body': 'continue'}
    def test_resumes_exact_thread_without_permission_or_cwd_overrides(self):
        service, client = FakeService(), FakeClient()
        execute_message(service, self.message, client, threading.Event())
        self.assertEqual(client.requests[0], ('thread/resume', {'threadId': 'thread-a'}))
        self.assertEqual(service.updates[-1][1]['result'], 'correct answer')
        self.assertEqual(service.updates[-1][1]['status'], 'completed')
        self.assertNotIn('cwd', client.requests[1][1])
        self.assertNotIn('approvalPolicy', client.requests[1][1])
    def test_resumed_counter_reset_counts_current_inference_only(self):
        service, client = FakeService(), FakeClient()
        with patch('taskboard.mobile_worker.rollout_usage', return_value={key: 5000000 for key in ('token_used','input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens')}):
            execute_message(service, self.message, client, threading.Event())
        usage = [args['usage'] for action,args in service.updates if action == 'usage']
        self.assertEqual(usage[0]['token_used'], 3)
        self.assertEqual(service.updates[-1][1]['status'], 'completed')

    def test_cumulative_notifications_are_idempotent_with_existing_session_total(self):
        service, client = FakeService(), FakeClient()
        usage = client.events[0]['params']['tokenUsage']
        usage['total'] = {key: value + 1000 for key,value in usage['total'].items()}
        import copy
        client.events.insert(1, copy.deepcopy(client.events[0]))
        later = copy.deepcopy(client.events[0])
        later['params']['tokenUsage']['total']['totalTokens'] += 3
        client.events.insert(2, later)
        with patch('taskboard.mobile_worker.rollout_usage', return_value={key:1000 for key in ('token_used','input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens')}):
            execute_message(service, self.message, client, threading.Event())
        self.assertEqual([args['usage']['token_used'] for action,args in service.updates if action == 'usage'], [3,3,6])
        self.assertEqual(service.updates[-1][1]['status'], 'completed')

    def test_rate_limit_snapshot_does_not_charge_previous_turn(self):
        import copy
        service, client = FakeService(), FakeClient()
        old = copy.deepcopy(client.events[0])
        old['params']['tokenUsage']['total'] = {key:100 for key in old['params']['tokenUsage']['total']}
        old['params']['tokenUsage']['last'] = {key:20 for key in old['params']['tokenUsage']['last']}
        for key in client.events[0]['params']['tokenUsage']['total']:
            client.events[0]['params']['tokenUsage']['total'][key] += 100
        client.events.insert(0, old)
        with patch('taskboard.mobile_worker.rollout_usage', return_value={key:100 for key in ('token_used','input_tokens','cached_input_tokens','output_tokens','reasoning_output_tokens')}):
            execute_message(service, self.message, client, threading.Event())
        self.assertEqual([args['usage']['token_used'] for action,args in service.updates if action == 'usage'], [3])
        self.assertEqual(service.updates[-1][1]['status'], 'completed')

    def test_budget_exhaustion_interrupts_and_reports_pause(self):
        service, client = FakeService(), FakeClient()
        original = service.call
        def call(action, arguments):
            original(action, arguments)
            return {'exhausted': True}
        service.call = call
        execute_message(service, self.message, client, threading.Event())
        self.assertIn(('interrupt', ('thread-a', 'turn')), client.requests)
        self.assertIn('预算已耗尽', service.updates[-1][1]['result'])

    def test_missing_usage_does_not_release_uncertain_budget(self):
        service, client = FakeService(), FakeClient()
        client.events = client.events[1:]
        execute_message(service, self.message, client, threading.Event())
        self.assertEqual(service.updates[-1][1]['status'], 'uncertain')

    def test_failed_resume_never_creates_another_thread(self):
        service, client = FakeService(), FakeClient(wrong=True)
        execute_message(service, self.message, client, threading.Event())
        self.assertEqual(len(client.requests), 1)
        self.assertEqual(service.updates[-1][1]['status'], 'failed')
    def test_uncertain_start_is_not_retried(self):
        service, client = FakeService(), FakeClient(timeout=True)
        execute_message(service, self.message, client, threading.Event())
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(service.updates[-1][1]['status'], 'uncertain')


class RolloutUsageTest(unittest.TestCase):
    def test_reads_latest_authoritative_usage_and_rejects_missing_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'rollout.jsonl'
            path.write_text(json.dumps({'type':'event_msg','payload':{'type':'token_count','info':{'total_token_usage':{'total_tokens':9,'input_tokens':6,'cached_input_tokens':2,'output_tokens':3,'reasoning_output_tokens':0}}}}) + '\n')
            self.assertEqual(rollout_usage(str(path))['token_used'], 9)
            with self.assertRaises(ValueError):
                rollout_usage(None)


if __name__ == '__main__': unittest.main()
