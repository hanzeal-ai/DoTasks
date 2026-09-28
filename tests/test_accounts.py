from __future__ import annotations

import base64
from contextlib import closing
import http.client
import json
from pathlib import Path
import tempfile
import threading
import socket
import time
from dataclasses import replace
import unittest
from unittest.mock import Mock, patch

from taskboard.agent import AgentConfig, RelayAgent
from taskboard.cloud.accounts import AccountStore
from taskboard.cloud.server import RelayConfig, build_relay_server
from taskboard.config import ServerConfig


class AccountHTTPTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = RelayConfig(ServerConfig(mode='cloud', account_mode='multi', host='127.0.0.1', port=0,
            home=self.temp.name, public_url='https://dotasks.test'), '', '')
        self.server = build_relay_server(self.config)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, method, path, payload=None, **headers):
        connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
        connection.request(method, path, json.dumps(payload) if payload is not None else None,
                           {'Host': 'dotasks.test', 'Content-Type': 'application/json', **headers})
        response = connection.getresponse()
        raw = response.read()
        result = response.status, dict(response.headers), json.loads(raw) if raw else {}
        connection.close()
        return result

    def register(self, name='alice', device='a' * 32):
        status, _, account = self.request('POST', '/api/cli/init', {'username': name,
            'password': 'correct horse battery staple', 'device_id': device, 'device_token': device + 'token-secret-1234'})
        self.assertEqual(200, status, account)
        return account

    def login(self, name):
        status, headers, body = self.request('POST', '/api/auth/login', {
            'username': name, 'password': 'correct horse battery staple'}, Origin='https://dotasks.test')
        self.assertEqual(200, status, body)
        return headers['Set-Cookie'].split(';')[0]

    def test_browser_task_dispatches_directly_with_tenant_isolation(self):
        alice, bob = self.register(), self.register('bob', 'b' * 32)
        status, _, intake = self.request('POST', '/api/task-intakes/enqueue', {
            'title': 'Implement feature', 'goal': 'Add the requested feature',
            'project': '/Users/alice/project', 'auto_dispatch': True,
        }, Cookie=self.login('alice'), Origin='https://dotasks.test')
        self.assertEqual(201, status, intake)
        self.assertEqual('ready', intake['task_status'])
        self.assertIsNone(intake['requirement_id'])
        status, _, cycle = self.request('POST', '/_agent/v1/tools/call', {
            'agent_id': alice['agent_id'], 'name': 'claim_schedule_cycle',
            'arguments': {'worker_id': 'agent'},
        }, Authorization='Bearer ' + alice['agent_token'])
        self.assertEqual(200, status, cycle)
        dispatch = cycle['result']['development']['dispatches'][0]
        self.assertEqual(intake['task_id'], dispatch['entity_id'])
        self.assertEqual('execution', dispatch['role'])
        self.assertEqual('/Users/alice/project', dispatch['project_path'])
        status, _, board = self.request('POST', '/_agent/v1/tools/call', {
            'agent_id': bob['agent_id'], 'name': 'list_board', 'arguments': {},
        }, Authorization='Bearer ' + bob['agent_token'])
        self.assertEqual(200, status)
        self.assertEqual([], board['result']['tasks'])

    def test_init_is_idempotent_and_does_not_unpause_or_store_plaintext_secrets(self):
        first = self.register()
        _, service = self.server.runtime(first['agent_id'])
        self.assertTrue(service.dispatcher_enabled())
        service.set_dispatcher_enabled(False)
        self.assertEqual(first, self.register())
        self.assertFalse(service.dispatcher_enabled())
        with closing(self.server.accounts.connect()) as db:
            row = dict(db.execute('SELECT * FROM accounts').fetchone())
        self.assertNotIn(first['agent_token'], repr(row))
        self.assertNotIn('correct horse battery staple', repr(row))
        self.assertEqual(0o600, self.server.accounts.path.stat().st_mode & 0o777)

    def test_image_preview_is_tenant_scoped(self):
        alice, bob = self.register(), self.register('bob', 'b' * 32)
        ca, cb = self.login('alice'), self.login('bob')
        _, service = self.server.runtime(alice['agent_id'])
        content = b"\x89PNG\r\n\x1a\nprivate-image"
        artifact = service.upload_visual_artifact({'filename': 'private.png',
            'content_base64': base64.b64encode(content).decode()})
        from urllib.parse import quote
        path = '/api/visual-artifacts/content?artifact_id=' + quote(artifact['artifact_id'], safe='')
        for cookie, expected in [(ca, 200), (cb, 400), ('', 401)]:
            connection = http.client.HTTPConnection('127.0.0.1', self.server.server_port, timeout=5)
            connection.request('GET', path, headers={'Host': 'dotasks.test', 'Cookie': cookie})
            response = connection.getresponse()
            body = response.read()
            self.assertEqual(expected, response.status)
            if expected == 200:
                self.assertEqual(content, body)
            else:
                self.assertNotIn(b'private-image', body)
            connection.close()

    def test_browser_and_agent_data_are_isolated_and_identity_cannot_be_supplied(self):
        alice, bob = self.register(), self.register('bob', 'b' * 32)
        ca, cb = self.login('alice'), self.login('bob')
        _, sa = self.server.runtime(alice['agent_id'])
        _, sb = self.server.runtime(bob['agent_id'])
        self.assertNotEqual(sa.db.path, sb.db.path)
        status, _, intake = self.request('POST', '/api/task-intakes/finalize', {
            'intake_kind': 'requirement', 'title': 'Alice private requirement', 'goal': 'Only Alice can see this',
            'project': '/Users/alice/project', 'priority': 'P2', 'auto_dispatch': True,
        }, Cookie=ca, Origin='https://dotasks.test')
        self.assertEqual(200, status, intake)
        self.assertEqual('Alice private requirement', self.request('GET', '/api/board', Cookie=ca)[2]['requirements'][0]['title'])
        self.assertEqual([], self.request('GET', '/api/board', Cookie=cb)[2]['requirements'])
        status, _, _ = self.request('POST', '/_agent/v1/tools/call', {
            'agent_id': bob['agent_id'], 'name': 'get_requirement', 'arguments': {'requirement_id': intake['requirement_id']}}, Authorization='Bearer ' + bob['agent_token'])
        self.assertEqual(404, status)
        sa.set_dispatcher_enabled(False)
        for cookie, expected in [(ca, False), (cb, True)]:
            status, _, board = self.request('GET', '/api/board?account_id=' + alice['agent_id'], Cookie=cookie)
            self.assertEqual(200, status)
            self.assertEqual(expected, board['dispatcher']['enabled'])
        status, _, _ = self.request('POST', '/_agent/v1/tools/call', {
            'agent_id': bob['agent_id'], 'name': 'list_board', 'arguments': {}}, Authorization='Bearer ' + alice['agent_token'])
        self.assertEqual(403, status)
        status, _, _ = self.request('GET', '/api/board', Authorization='Bearer ' + alice['agent_token'])
        self.assertEqual(401, status)
        status, _, _ = self.request('GET', '/_agent/v1/status', Cookie=ca)
        self.assertEqual(401, status)
        # Changing an account in payload cannot make an authenticated tool use its service.
        status, _, result = self.request('POST', '/_agent/v1/tools/call', {
            'agent_id': bob['agent_id'], 'name': 'list_board', 'arguments': {'account_id': alice['agent_id']}}, Authorization='Bearer ' + bob['agent_token'])
        self.assertEqual(200, status)
        self.assertTrue(result['result']['dispatcher']['enabled'])

    def test_sessions_are_revocable_and_legacy_credentials_do_not_bypass_tenants(self):
        self.register()
        cookie = self.login('alice')
        status, _, body = self.request('GET', '/api/auth/status', Cookie=cookie)
        self.assertEqual('alice', body['username'])
        self.request('POST', '/api/auth/logout', {}, Cookie=cookie, Origin='https://dotasks.test')
        self.assertEqual(401, self.request('GET', '/api/board', Cookie=cookie)[0])
        basic = base64.b64encode(b'alice:correct horse battery staple').decode()
        self.assertEqual(401, self.request('GET', '/api/board', Authorization='Basic ' + basic)[0])
        self.assertEqual(401, self.request('GET', '/api/board')[0])

    def test_registration_rejects_other_devices_bad_credentials_and_untrusted_origins(self):
        self.register()
        for payload, expected in [
            ({'username': 'alice', 'password': 'wrong password 123', 'device_id': 'a' * 32}, 401),
            ({'username': 'alice', 'password': 'correct horse battery staple', 'device_id': 'b' * 32}, 409),
            ({'username': '../bob', 'password': 'correct horse battery staple', 'device_id': 'b' * 32}, 400),
            ({'username': 'bob', 'password': 'short', 'device_id': 'b' * 32}, 400),
        ]:
            self.assertEqual(expected, self.request('POST', '/api/cli/init', {**payload, 'device_token': 'a' * 32 + 'token-secret-1234'})[0])
        self.assertEqual(403, self.request('POST', '/api/cli/init', {}, Origin='https://evil.test')[0])
        self.assertEqual(403, self.request('POST', '/api/cli/init', {}, Host='evil.test')[0])

    def test_agent_cannot_import_db_or_read_server_files_as_artifacts(self):
        account = self.register()
        auth = {'Authorization': 'Bearer ' + account['agent_token']}
        self.assertEqual({'accept_snapshot': False}, self.request('POST', '/_agent/v1/bootstrap/status', {'agent_id': account['agent_id']}, **auth)[2])
        self.assertEqual(403, self.request('POST', '/_agent/v1/bootstrap', {}, **auth)[0])
        status, _, _ = self.request('POST', '/_agent/v1/tools/call', {'agent_id': account['agent_id'],
            'name': 'upload_visual_artifact', 'arguments': {'path': str(self.server.accounts.path)}}, **auth)
        self.assertEqual(400, status)

    def test_notifications_and_connection_status_are_scoped_to_account(self):
        a, b = self.register(), self.register('bob', 'b' * 32)
        ca, cb = Mock(), Mock()
        self.server.register_agent_socket(a['agent_id'], ca)
        self.server.register_agent_socket(b['agent_id'], cb)
        self.server.notify_agent(a['agent_id'], 'state_changed')
        ca.send.assert_called_once()
        cb.send.assert_not_called()
        status, _, body = self.request('GET', '/_agent/v1/status', Authorization='Bearer ' + a['agent_token'])
        self.assertEqual(200, status)
        self.assertTrue(body['connected'])
        self.assertEqual(a['agent_id'], body['agent_id'])

    def test_real_agent_connects_to_its_own_runtime(self):
        account = self.register()
        origin = f'127.0.0.1:{self.server.server_port}'
        self.server.base_config = replace(self.config, server=replace(self.config.server, extra_trusted_hosts=(origin,)))
        executor = Mock()
        agent = RelayAgent(AgentConfig('http://' + origin, account['agent_id'], account['agent_token'],
            data_home=str(Path(self.temp.name) / 'client')), executor=executor)
        failures = []

        def connect():
            try:
                agent.run_event_stream_once()
            except (ConnectionError, OSError):
                pass  # The test closes its own socket below.
            except Exception as exc:
                failures.append(exc)

        with patch.object(agent, 'board_hash', return_value=''), patch.object(agent, 'metadata', return_value={}):
            worker = threading.Thread(target=connect, daemon=True)
            worker.start()
            deadline = time.monotonic() + 4
            while not executor.wake.called and time.monotonic() < deadline:
                time.sleep(.02)
            self.assertEqual([], failures)
            self.assertTrue(executor.wake.called)
            self.assertTrue(self.server.agent_connected(account['agent_id']))
            with self.server._agent_sockets_lock:
                connections = list(self.server._agent_sockets[account['agent_id']].values())
            for connection in connections:
                connection.socket.shutdown(socket.SHUT_RDWR)
            worker.join(3)
        self.assertFalse(worker.is_alive())

    def test_verification_runs_on_local_agent_and_completes_relay(self):
        account = self.register()
        store, service = self.server.runtime(account['agent_id'])
        output = []
        worker = threading.Thread(target=lambda: output.append(service._run_project_command('printf verified', self.temp.name, 2)))
        worker.start()
        import time
        deadline = time.monotonic() + 3
        command = None
        while not command and time.monotonic() < deadline:
            command = store.claim(account['agent_id'])
            time.sleep(.01)
        self.assertIsNotNone(command)
        config = AgentConfig('https://dotasks.test', account['agent_id'], account['agent_token'], data_home=self.temp.name)
        agent = RelayAgent(config, executor=Mock())

        def complete(path, result):
            store.complete(account['agent_id'], result['command_id'], result['status'], result['headers'], base64.b64decode(result['body']))
            return {}

        with patch.object(agent, '_cloud_request', side_effect=complete):
            agent.execute_command({**command, 'body': base64.b64encode(command['body']).decode()})
        worker.join(5)
        self.assertEqual('passed', output[0][0])
        self.assertEqual('verified', output[0][2])


class AccountStoreTest(unittest.TestCase):
    def test_concurrent_registration_produces_one_identity_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AccountStore(Path(temporary))
            results = []
            workers = [threading.Thread(target=lambda: results.append(store.initialize('alice', 'long password 123', 'a' * 32, 'secret-' * 8))) for _ in range(4)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join()
            self.assertEqual(4, len(results))
            self.assertEqual(1, len({result[0]['id'] for result in results}))
            again = AccountStore(Path(temporary)).initialize('alice', 'long password 123', 'a' * 32, 'secret-' * 8)
            self.assertEqual(results[0], again)


if __name__ == '__main__':
    unittest.main()
