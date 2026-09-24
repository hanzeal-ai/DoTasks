from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
import tempfile
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from taskboard import cli
from taskboard.cli_service import LABELS
import sys


class CLITest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.data = self.home / 'Library/Application Support/DoTasks'
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch('pathlib.Path.home', return_value=self.home))
        self.enterContext(patch('taskboard.cli.sys.platform', 'darwin'))
        self.enterContext(patch.object(cli.BackgroundService, 'legacy_running', return_value=False))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))

    def configure(self):
        return cli.main(['configure', '--cloud-url', 'https://example.test', '--agent-token', 'x' * 24])

    def install(self):
        service = cli.BackgroundService()
        runtime = self.data / 'cli/current'
        (runtime / 'taskboard').mkdir(parents=True)
        (runtime / 'taskboard/cli.py').write_text('fixture')
        for name, label in LABELS.items():
            path = service.plist(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(plistlib.dumps({'Label': label,
                'ProgramArguments': [sys.executable, '-B', '-m', 'taskboard.' + name],
                'WorkingDirectory': str(runtime), 'EnvironmentVariables': {'DOTASKS_HOME': str(self.data)}}))
        return service

    def test_configure_hidden_token_and_partial_update(self):
        with patch('taskboard.cli.getpass.getpass', return_value='x' * 24) as prompt:
            self.assertEqual(0, cli.main(['configure', '--cloud-url', 'https://example.test', '--vault', '/vault']))
            prompt.assert_called_once()
        self.assertEqual(0, cli.main(['configure', '--agent-id', 'mac']))
        data = json.loads(cli.default_config_path().read_text())
        self.assertEqual('/vault', data['vault'])
        self.assertEqual('mac', data['agent_id'])
        self.assertEqual('x' * 24, data['agent_token'])
        self.assertEqual(0o600, cli.default_config_path().stat().st_mode & 0o777)
        self.assertNotIn('x' * 24, self.output.getvalue())

    def test_invalid_config_does_not_replace_saved_config(self):
        self.configure()
        before = cli.default_config_path().read_bytes()
        self.assertEqual(1, cli.main(['configure', '--cloud-url', 'invalid']))
        self.assertEqual(before, cli.default_config_path().read_bytes())

    def test_start_loaded_service_does_not_restart_or_duplicate(self):
        self.install()
        self.configure()
        with patch.object(cli.BackgroundService, 'states', return_value={'server': 'running', 'agent': 'running'}), patch.object(cli.BackgroundService, 'run') as run:
            self.assertEqual(0, cli.main(['start']))
            self.assertEqual([('enable', cli.BackgroundService().target(n)) for n in LABELS], [c.args for c in run.call_args_list])

    def test_start_stopped_service_bootstraps_existing_plist(self):
        service = self.install()
        self.configure()
        with patch.object(cli.BackgroundService, 'states', return_value={'server': 'stopped', 'agent': 'stopped'}), patch.object(cli.BackgroundService, 'run') as run:
            self.assertEqual(0, cli.main(['start']))
        self.assertEqual([args for n in LABELS for args in [('enable', service.target(n)), ('bootstrap', service.domain, str(service.plist(n)))]], [call.args for call in run.call_args_list])

    def test_stop_disables_autostart_and_unloads_only_owned_service(self):
        service = self.install()
        with patch.object(cli.BackgroundService, 'states', return_value={'server': 'running', 'agent': 'running'}), patch.object(cli.BackgroundService, 'run') as run:
            self.assertEqual(0, cli.main(['stop']))
        self.assertEqual([args for n in reversed(LABELS) for args in [('disable', service.target(n)), ('bootout', service.target(n))]], [call.args for call in run.call_args_list])

    def test_missing_installation_and_config_mismatch_do_not_start(self):
        self.configure()
        with patch.object(cli.BackgroundService, 'run') as run:
            self.assertEqual(1, cli.main(['start']))
            run.assert_not_called()
        self.install()
        with patch.dict(os.environ, {'DOTASKS_HOME': str(self.home / 'other')}), patch.object(cli.BackgroundService, 'run') as run:
            self.assertEqual(1, cli.main(['start']))
            run.assert_not_called()

    def test_launchctl_failures_are_not_reported_as_stopped(self):
        result = subprocess.CompletedProcess([], 1, '', 'Permission denied')
        with patch('taskboard.cli.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'Permission denied'):
                cli.BackgroundService().state()
        result.stderr = 'Could not find service "local.sanmws.dotasks-helper" in domain for user gui: 501'
        with patch('taskboard.cli.subprocess.run', return_value=result):
            self.assertEqual('stopped', cli.BackgroundService().state())

    def test_launchctl_running_state_and_failed_mutation(self):
        result = subprocess.CompletedProcess([], 0, 'service = {\n state = running\n pid = 123\n}', '')
        with patch('taskboard.cli.subprocess.run', return_value=result):
            self.assertEqual('running', cli.BackgroundService().state())
        result.returncode, result.stderr = 1, 'Operation not permitted'
        with patch('taskboard.cli.subprocess.run', return_value=result):
            with self.assertRaisesRegex(RuntimeError, 'Operation not permitted'):
                cli.BackgroundService().run('enable', 'test')

    def test_temporary_shell_credentials_are_not_silently_used_for_background(self):
        self.install()
        self.configure()
        with patch.dict(os.environ, {'DOTASKS_AGENT_TOKEN': 'temporary-secret' * 3}), patch.object(cli.BackgroundService, 'run') as run:
            self.assertEqual(1, cli.main(['start']))
            run.assert_not_called()
        self.assertNotIn('temporary-secret', self.errors.getvalue())

    def test_doctor_reports_failed_codex_login(self):
        self.install()
        self.configure()
        responses = [{'ok': True}, {'result': {'dispatcher': {'enabled': True}, 'tasks': []}}]
        with patch.object(cli.BackgroundService, 'state', return_value='running'), patch('taskboard.cli.read_json', side_effect=responses), patch('taskboard.cli.subprocess.run', return_value=subprocess.CompletedProcess([], 1, '', 'secret')):
            self.assertEqual(1, cli.main(['doctor']))
        self.assertIn('不可执行或未登录', self.output.getvalue())
        self.assertNotIn('secret', self.output.getvalue())

    def test_http_probe_uses_existing_authenticated_read_only_tool_contract(self):
        requests = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                requests.append((self.path, self.headers['Authorization'], payload))
                if self.path == '/redirect':
                    self.send_response(302)
                    self.send_header('Location', '/should-not-receive-token')
                    self.end_headers()
                    return
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"result":{"tasks":[]}}')

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            body = {'agent_id': 'mac', 'name': 'list_board', 'arguments': {}}
            url = f'http://127.0.0.1:{server.server_port}/_agent/v1/tools/call'
            self.assertEqual({'result': {'tasks': []}}, cli.read_json(url, body, 'x' * 24))
            self.assertEqual([('/_agent/v1/tools/call', 'Bearer ' + 'x' * 24, body)], requests)
            with self.assertRaises(urllib.error.HTTPError):
                cli.read_json(f'http://127.0.0.1:{server.server_port}/redirect', body, 'x' * 24)
            self.assertEqual(2, len(requests))
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_status_reads_cloud_tasks_without_claiming_work(self):
        self.install()
        self.configure()
        responses = [{'ok': True, 'version': 'test'}, {'result': {'dispatcher': {'enabled': False}, 'tasks': [
            {'id': 'task1', 'title': 'Active task', 'active_run_status': 'running'},
            {'id': 'task2', 'title': 'Done', 'active_run_status': 'completed'},
        ]}}]
        with patch.object(cli.BackgroundService, 'state', return_value='running'), patch('taskboard.cli.read_json', side_effect=responses) as request:
            self.assertEqual(0, cli.main(['status']))
        self.assertEqual('list_board', request.call_args.args[1]['name'])
        self.assertIn('task1', self.output.getvalue())
        self.assertNotIn('task2', self.output.getvalue())
        self.assertIn('不代表后台 WSS 已连接', self.output.getvalue())
        self.assertIn('已暂停', self.output.getvalue())

    def test_unreachable_services_fail_without_leaking_secret(self):
        self.install()
        self.configure()
        with patch.object(cli.BackgroundService, 'state', return_value='running'), patch('taskboard.cli.read_json', side_effect=OSError('x' * 24)):
            self.assertEqual(1, cli.main(['status']))
        self.assertNotIn('x' * 24, self.output.getvalue())

    def test_logs_tail_and_missing_logs(self):
        self.assertEqual(1, cli.main(['logs']))
        logs = self.data / 'logs'
        logs.mkdir(parents=True)
        (logs / 'agent.err.log').write_text('old\nnew\n')
        self.assertEqual(0, cli.main(['logs', '-n', '1']))
        self.assertIn('new', self.output.getvalue())
        self.assertNotIn('old', self.output.getvalue())
        self.assertEqual(1, cli.main(['logs', '-n', '0']))

    def test_legacy_server_and_serve_arguments_are_preserved(self):
        for args, expected in [([], []), (['--port', '9999'], ['--port', '9999']), (['serve', '--port', '9999'], ['--port', '9999'])]:
            with patch('taskboard.server.main', side_effect=lambda: self.assertEqual(expected, cli.sys.argv[1:])) as serve:
                self.assertEqual(0, cli.main(args))
                serve.assert_called_once()


if __name__ == '__main__':
    unittest.main()
