"""State-machine tests for the opt-in project controller demo."""
import importlib.util
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest

SPEC = importlib.util.spec_from_file_location('project_controller_demo', Path(__file__).parents[1] / 'scripts/demo-project-controller.py')
demo = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(demo)


class Backend:
    def __init__(self):
        self.events = []
        self.states = {}
        self.outcome = {'status': 'completed', 'child_id': 'child-1'}

    def create_controller(self, project, name, created):
        tid = f'controller-{len(self.states) + 1}'
        self.events.append(('create', project['id'], name))
        self.states[tid] = 'idle'
        created(tid)

    def inspect(self, tid):
        state = self.states[tid]
        if isinstance(state, Exception):
            raise state
        return state

    def archive(self, tid):
        self.events.append(('archive', tid))
        self.states[tid] = 'archived'

    def dispatch(self, project, record):
        self.events.append(('dispatch', record['controller_id']))
        return 'turn-1'

    def result(self, record, wait):
        return self.outcome


class ProjectControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.project = {'id': 'project-a', 'path': '/tmp/a', 'name': 'A'}
        self.backend = Backend()

    def store(self, project=None):
        return demo.Store(self.root, project or self.project)

    def test_persistent_reuse_and_project_isolation(self):
        with self.store().locked() as store:
            first, action = demo.Manager(store, self.backend).ensure()
            self.assertEqual(action, 'created')
        with self.store().locked() as store:
            second, action = demo.Manager(store, self.backend).ensure()
            self.assertEqual((action, second['thread_id']), ('reused', first['thread_id']))
        with self.store({'id': 'project-b', 'path': '/tmp/b', 'name': 'B'}).locked() as store:
            other, _ = demo.Manager(store, self.backend).ensure()
            self.assertNotEqual(first['thread_id'], other['thread_id'])

    def test_inactive_archived_before_replacement(self):
        with self.store().locked() as store:
            manager = demo.Manager(store, self.backend)
            old, _ = manager.ensure()
            self.backend.states[old['thread_id']] = 'inactive'
            new, _ = manager.ensure()
            self.assertEqual(self.backend.events[-2][0], 'archive')
            self.assertEqual(self.backend.events[-1][0], 'create')
            self.assertNotEqual(old['thread_id'], new['thread_id'])
            self.assertEqual(store.data['generation'], 2)
            self.assertIn('002', new['name'])

    def test_archived_or_missing_is_replaced_without_double_archive(self):
        for state in ('archived', 'missing'):
            with self.subTest(state=state), self.store().locked() as store:
                manager = demo.Manager(store, self.backend)
                old, _ = manager.ensure()
                self.backend.states[old['thread_id']] = state
                before = len(self.backend.events)
                manager.ensure()
                self.assertEqual([e[0] for e in self.backend.events[before:]], ['create'])

    def test_busy_waiting_unknown_and_disconnect_do_not_retire(self):
        with self.store().locked() as store:
            manager = demo.Manager(store, self.backend)
            old, _ = manager.ensure()
            for state in ('active', 'waiting', 'unknown', RuntimeError('disconnected')):
                self.backend.states[old['thread_id']] = state
                with self.assertRaises(RuntimeError):
                    manager.ensure()
                self.assertEqual(store.data['controller']['thread_id'], old['thread_id'])
            self.assertEqual(len(self.backend.events), 1)

    def test_repeated_request_reconciles_without_resending(self):
        with self.store().locked() as store:
            manager = demo.Manager(store, self.backend)
            self.backend.outcome = {'status': 'waiting_approval', 'message': 'waiting'}
            manager.submit('request-1', 'Hello', 'Say hello', 0)
            with self.assertRaises(demo.Blocked):
                manager.submit('request-2', 'Hello', 'Say hello', 0)
            self.backend.outcome = {'status': 'completed', 'child_id': 'child-1'}
            result = manager.submit('request-1', 'Hello', 'Say hello', 0)
            self.assertEqual(result['child_id'], 'child-1')
            self.assertNotIn('message', result)
            self.assertEqual(sum(e[0] == 'dispatch' for e in self.backend.events), 1)
            with self.assertRaises(demo.Blocked):
                manager.submit('request-1', 'Changed', 'Say hello', 0)

    def test_uncertain_send_is_durable_and_never_retried(self):
        def fail(project, record):
            raise TimeoutError('sent but response lost')
        self.backend.dispatch = fail
        with self.store().locked() as store:
            result = demo.Manager(store, self.backend).submit('request-1', 'Hi', 'Hi', 0)
            self.assertEqual(result['status'], 'uncertain')
        with self.store().locked() as store:
            result = demo.Manager(store, self.backend).submit('request-1', 'Hi', 'Hi', 0)
            self.assertEqual(result['status'], 'uncertain')
            self.assertEqual(len(self.backend.events), 1)

    def test_archive_failure_does_not_create_replacement(self):
        def fail(tid):
            raise RuntimeError('archive failed')
        with self.store().locked() as store:
            manager = demo.Manager(store, self.backend)
            old, _ = manager.ensure()
            self.backend.states[old['thread_id']] = 'inactive'
            self.backend.archive = fail
            with self.assertRaises(RuntimeError):
                manager.ensure()
            self.assertEqual(len(self.backend.events), 1)
            self.assertEqual(store.data['controller']['phase'], 'retiring')
            self.backend.states[old['thread_id']] = 'archived'
            new, _ = manager.ensure()
            self.assertNotEqual(new['thread_id'], old['thread_id'])

    def test_project_lock_and_identity_mismatch(self):
        with self.store().locked():
            with self.assertRaises(demo.Blocked), self.store().locked():
                pass
        with self.store().locked() as store:
            store.save()
        with self.assertRaises(demo.Blocked), self.store({**self.project, 'path': '/different'}).locked():
            pass


class NativeResultTest(unittest.TestCase):
    def setUp(self):
        self.backend = demo.Desktop.__new__(demo.Desktop)
        self.record = {'controller_id': 'parent', 'turn_id': 'turn', 'project_path': '/tmp/project',
                       'title': 'Task', 'prompt': 'Hello', 'target': {'type': 'project', 'projectId': 'a'}}
        self.call = {'type': 'mcpToolCall', 'server': 'codex_app', 'tool': 'create_thread',
                     'status': 'completed', 'arguments': {k: self.record[k] for k in ('title', 'prompt', 'target')},
                     'result': {'content': [{'type': 'text', 'text': '{"threadId":"child"}'}]}}
        self.state = {'turns': [{'turnId': 'turn', 'status': 'completed', 'items': [self.call]}]}
        self.backend.ipc = SimpleNamespace(current=lambda tid: self.state)
        self.backend.turns = lambda s: s['turns']
        self.backend.snapshot = lambda tid: ('owner', {'cwd': '/tmp/project'})
        self.backend.catalog = SimpleNamespace(get=lambda tid: {'id': tid, 'cwd': ''})

    def test_native_snapshot_over_empty_database_cwd(self):
        result = self.backend.result(self.record, 0)
        self.assertEqual(result['child_id'], 'child')

    def test_wrong_target_or_model_only_id_is_not_success(self):
        self.call['arguments']['target'] = {'type': 'projectless'}
        self.assertEqual(self.backend.result(self.record, 0)['status'], 'uncertain')
        self.state['turns'][0]['items'] = [{'type': 'agentMessage', 'text': 'child'}]
        self.assertEqual(self.backend.result(self.record, 0)['status'], 'uncertain')

    def test_actual_child_wrong_project_is_rejected(self):
        self.backend.snapshot = lambda tid: ('owner', {'cwd': '/wrong'})
        with self.assertRaises(demo.Blocked):
            self.backend.result(self.record, 0)


if __name__ == '__main__':
    unittest.main()
