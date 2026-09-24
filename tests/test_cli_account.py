from contextlib import redirect_stdout
import ctypes as C
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from taskboard import cli, cli_account
from taskboard.agent import AgentConfig


class AccountCommandTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.enterContext(patch.object(cli_account, 'default_data_home', return_value=self.home))
        self.config = AgentConfig('https://dotasks.test', 'a' * 32, 'device-token-' * 4)
        self.enterContext(patch.object(cli_account, 'load_agent_config', return_value=self.config))
        self.store = self.enterContext(patch.object(cli_account, 'PasswordStore')).return_value
        self.receipt = {'username': 'alice', 'cloud_url': self.config.cloud_url,
                        'agent_id': self.config.agent_id, 'device_token': self.config.agent_token}
        self.path = self.home / 'onboarding.json'
        self.path.write_text(json.dumps(self.receipt))
        self.output = self.enterContext(redirect_stdout(io.StringIO()))

    def test_account_reads_bound_password_without_network_or_device_token_output(self):
        self.store.read.return_value = 'saved password 123'
        with patch('taskboard.cli.read_json') as network:
            self.assertEqual(0, cli.main(['account']))
        network.assert_not_called()
        self.store.read.assert_called_once_with(self.config.cloud_url, 'alice', self.config.agent_id)
        self.assertIn('账号：alice', self.output.getvalue())
        self.assertIn('密码：saved password 123', self.output.getvalue())
        self.assertNotIn(self.config.agent_token, self.output.getvalue())

    def test_old_account_does_not_invent_missing_password(self):
        self.store.read.return_value = None
        cli_account.show_account()
        self.assertIn('本机未保存', self.output.getvalue())

    def test_wrong_binding_is_rejected_before_keychain_read(self):
        for field in ('cloud_url', 'agent_id', 'device_token'):
            self.path.write_text(json.dumps({**self.receipt, field: 'other'}))
            with self.assertRaisesRegex(RuntimeError, '不一致'):
                cli_account.show_account()
        self.store.read.assert_not_called()
        self.assertEqual('', self.output.getvalue())

    def test_browser_failure_does_not_fail_initialization_or_add_credentials_to_url(self):
        with patch.object(cli_account.subprocess, 'run', side_effect=OSError('no browser')) as run:
            cli_account.open_cloud(self.config.cloud_url)
        self.assertEqual(['/usr/bin/open', self.config.cloud_url], run.call_args.args[0])
        self.assertIn('手动访问', self.output.getvalue())
        with self.assertRaises(ValueError):
            cli_account.open_cloud('https://alice:secret@dotasks.test')


@unittest.skipUnless(sys.platform == 'darwin', 'macOS framework bridge')
class KeychainBridgeTest(unittest.TestCase):
    def test_native_corefoundation_encoding_and_update_insert_without_accessing_keychain(self):
        store = cli_account.PasswordStore()
        with patch.object(store.sec, 'SecItemUpdate', return_value=-25300), patch.object(store.sec, 'SecItemAdd', return_value=0) as add:
            store.save('https://dotasks.test', 'alice', 'id1', '密码 with spaces')
        add.assert_called_once()
        with patch.object(store.sec, 'SecItemUpdate', return_value=0), patch.object(store.sec, 'SecItemAdd') as add:
            store.save('https://dotasks.test', 'alice', 'id1', 'updated password')
        add.assert_not_called()

    def test_native_buffer_decoding_missing_and_denied_are_distinct(self):
        store = cli_account.PasswordStore()
        secret = '密码 with spaces'.encode()
        def read(query, output):
            output._obj.value = store.cf.CFDataCreate(None, secret, len(secret))
            return 0
        with patch.object(store.sec, 'SecItemCopyMatching', side_effect=read):
            self.assertEqual(secret.decode(), store.read('https://dotasks.test', 'alice', 'id1'))
        with patch.object(store.sec, 'SecItemCopyMatching', return_value=-25300):
            self.assertIsNone(store.read('https://dotasks.test', 'alice', 'id1'))
        with patch.object(store.sec, 'SecItemCopyMatching', return_value=-25293):
            with self.assertRaisesRegex(RuntimeError, '-25293'):
                store.read('https://dotasks.test', 'alice', 'id1')
