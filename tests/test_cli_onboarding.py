from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
from pathlib import Path
import shutil
import hashlib
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from taskboard import cli, cli_install, cli_onboarding


class OnboardingTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(patch('pathlib.Path.home', return_value=self.home))
        self.enterContext(patch('taskboard.cli.sys.platform', 'darwin'))
        self.enterContext(patch.object(cli.BackgroundService, 'validate_installation'))
        self.start = self.enterContext(patch.object(cli.BackgroundService, 'start'))
        self.login = self.enterContext(patch('taskboard.cli_onboarding.ensure_codex_login'))
        self.output = self.enterContext(contextlib.redirect_stdout(io.StringIO()))
        self.errors = self.enterContext(contextlib.redirect_stderr(io.StringIO()))
        self.args = ['init', '--username', 'alice']
        self.enterContext(patch('taskboard.cli_onboarding.secrets.token_urlsafe', return_value='secret-token-' * 4))
        self.password = 'correct horse battery staple'
        self.store = self.enterContext(patch('taskboard.cli_onboarding.PasswordStore')).return_value
        self.store.read.return_value = self.password
        self.enterContext(patch('taskboard.cli_account.PasswordStore', return_value=self.store))
        self.open_cloud = self.enterContext(patch('taskboard.cli_onboarding.open_cloud'))
        self.response = {'username': 'alice', 'cloud_url': cli_onboarding.DEFAULT_CLOUD_URL,
                         'agent_id': 'a' * 32, 'agent_token': 'secret-token-' * 4}

    def responses(self):
        return [self.response, {'ok': True}, {'agent_id': 'a' * 32, 'connected': True, 'dispatcher_enabled': True}]

    def test_init_registers_saves_only_device_credentials_and_starts_automatically(self):
        with patch('taskboard.cli_onboarding.getpass.getpass', return_value=self.password), patch('taskboard.cli_onboarding.read_json', side_effect=self.responses()) as http:
            self.assertEqual(0, cli.main(self.args))
        self.assertEqual(cli_onboarding.DEFAULT_CLOUD_URL + '/api/cli/init', http.call_args_list[0].args[0])
        self.start.assert_called_once()
        config = cli.default_config_path()
        self.assertEqual(0o600, config.stat().st_mode & 0o777)
        self.assertEqual(self.response['agent_token'], json.loads(config.read_text())['agent_token'])
        for file in self.home.rglob('*.json'):
            self.assertNotIn(self.password, file.read_text())
        self.assertIn("账号：alice", self.output.getvalue())
        self.assertIn("密码：" + self.password, self.output.getvalue())
        self.store.save.assert_called_once_with(cli_onboarding.DEFAULT_CLOUD_URL, "alice", self.response["agent_id"], self.password)
        self.open_cloud.assert_called_once_with(cli_onboarding.DEFAULT_CLOUD_URL)
        self.assertNotIn(self.response['agent_token'], self.output.getvalue())

    def test_retry_after_lost_response_reuses_device_id(self):
        with patch('taskboard.cli_onboarding.getpass.getpass', return_value=self.password), patch('taskboard.cli_onboarding.read_json', side_effect=OSError('offline')) as http:
            self.assertEqual(1, cli.main(self.args))
        initial_id = http.call_args.args[1]['device_id']
        self.start.assert_not_called()
        with patch('taskboard.cli_onboarding.getpass.getpass', return_value=self.password), patch('taskboard.cli_onboarding.read_json', side_effect=self.responses()) as http:
            self.assertEqual(0, cli.main(self.args))
        self.assertEqual(initial_id, http.call_args_list[0].args[1]['device_id'])

    def test_completed_init_does_not_register_again_or_prompt_for_password(self):
        with patch('taskboard.cli_onboarding.getpass.getpass', return_value=self.password), patch('taskboard.cli_onboarding.read_json', side_effect=self.responses()):
            self.assertEqual(0, cli.main(self.args))
        with patch('taskboard.cli_onboarding.getpass.getpass') as prompt, patch('taskboard.cli_onboarding.read_json', side_effect=self.responses()[1:]) as http:
            self.assertEqual(0, cli.main(self.args))
        prompt.assert_not_called()
        self.assertFalse(any('/api/cli/init' in call.args[0] for call in http.call_args_list))

    def test_http_and_existing_bindings_are_rejected_before_network_or_login(self):
        self.assertEqual(1, cli.main(['init', '--username', 'alice', '--cloud-url', 'http://example.test']))
        cli.default_config_path().parent.mkdir(parents=True)
        cli.default_config_path().write_text('{"existing":"binding"}')
        self.assertEqual(1, cli.main(self.args))
        self.login.assert_not_called()
        self.start.assert_not_called()

    def test_password_mismatch_and_unconnected_agent_do_not_claim_ready(self):
        with patch('taskboard.cli_onboarding.getpass.getpass', side_effect=[self.password, 'different']), patch('taskboard.cli_onboarding.read_json') as http:
            self.assertEqual(1, cli.main(self.args))
            http.assert_not_called()
        with patch('taskboard.cli_onboarding.getpass.getpass', return_value=self.password), patch('taskboard.cli_onboarding.read_json', return_value=self.response), patch('taskboard.cli_onboarding.time.monotonic', side_effect=[0, 31]):
            self.assertEqual(1, cli.main(self.args))
        self.assertNotIn('初始化完成', self.output.getvalue())
        self.assertIn('凭证已保存', self.errors.getvalue())
        self.assertNotIn(self.password, self.output.getvalue())
        self.open_cloud.assert_not_called()

    def test_keychain_failure_keeps_registration_and_prints_password_on_success(self):
        self.store.save.side_effect = RuntimeError('locked')
        with patch('taskboard.cli_onboarding.getpass.getpass', return_value=self.password), patch('taskboard.cli_onboarding.read_json', side_effect=self.responses()):
            self.assertEqual(0, cli.main(self.args))
        self.assertIn('密码未能保存到钥匙串', self.output.getvalue())
        self.assertIn('密码：' + self.password, self.output.getvalue())
        self.start.assert_called_once()



class InstallerTest(unittest.TestCase):
    def test_install_configures_path_once_without_launching_or_touching_credentials(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary) / 'home'
            home.mkdir()
            runtime = Path(temporary) / 'source/runtime'
            for file in ('taskboard/cli.py', 'taskboard/cli_service.py', 'taskboard/cli_onboarding.py', 'taskboard/server.py', 'taskboard/agent.py', 'scripts/mcp-server'):
                target = runtime / file
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text('fixture')
            files = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in runtime.rglob('*') if p.is_file()}
            (runtime / 'release.json').write_text(json.dumps({'version': 'test-v1', 'files': files}))
            data = home / 'Library/Application Support/DoTasks'
            data.mkdir(parents=True)
            credentials = data / 'cloud-agent.json'
            credentials.write_text('preserve existing credentials')
            calls = []

            def run(argv, **kwargs):
                calls.append(argv)
                if argv[0] == '/usr/bin/ditto':
                    shutil.copytree(argv[1], argv[2])
                return subprocess.CompletedProcess(argv, 0, '', '')

            with patch.dict(os.environ, {'SHELL': '/bin/zsh'}, clear=True), patch('pathlib.Path.home', return_value=home), patch('sys.platform', 'darwin'), patch.object(cli.BackgroundService, 'state', return_value='stopped'), patch.object(cli.BackgroundService, 'legacy_running', return_value=False), patch.object(cli.BackgroundService, 'run') as launch, patch('taskboard.cli_install.subprocess.run', side_effect=run), contextlib.redirect_stdout(io.StringIO()):
                for _ in range(2):
                    launcher = cli_install.install(runtime)
                self.assertEqual(1, (home / '.zshrc').read_text().count(cli_install.PATH_LINE))
                self.assertIn('-m taskboard.cli', launcher.read_text())
                self.assertTrue(os.access(launcher, os.X_OK))
                self.assertTrue(all(call.args[0] == 'disable' for call in launch.call_args_list))
            self.assertEqual('preserve existing credentials', credentials.read_text())


if __name__ == '__main__':
    unittest.main()
