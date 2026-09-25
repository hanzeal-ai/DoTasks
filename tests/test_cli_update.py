from __future__ import annotations

from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import zipfile

from taskboard import cli_update
from taskboard.agent import AgentConfig
from taskboard.cli_install import activate
from taskboard.cli_service import BackgroundService, LABELS


class CLIUpdateTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.enterContext(patch('pathlib.Path.home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {}, clear=True))
        self.enterContext(redirect_stdout(io.StringIO()))

    def test_rejects_unsafe_archive_and_symlinks(self):
        for name in ('../escaped', '/absolute', 'DoTasksCLI/../../escaped', 'DoTasksCLI\\evil'):
            archive = self.home / 'bad.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr(name, 'bad')
            with self.assertRaises(ValueError):
                cli_update.extract_release(archive, self.home / 'output')
        with zipfile.ZipFile(archive, 'w') as z:
            entry = zipfile.ZipInfo('DoTasksCLI/link')
            entry.external_attr = (0o120777 << 16)
            z.writestr(entry, '/etc')
        with self.assertRaises(ValueError):
            cli_update.extract_release(archive, self.home / 'output')

    def test_hash_mismatch_does_not_extract_or_activate(self):
        manifest = {'version': 'v2', 'url': '/downloads/v2.zip', 'sha256': '0' * 64, 'size': 3}
        opener = Mock()
        opener.open.return_value = io.BytesIO(b'bad')
        with patch('taskboard.cli_update.read_json', return_value=manifest), patch('urllib.request.build_opener', return_value=opener), patch.object(cli_update, 'extract_release') as extract:
            with self.assertRaisesRegex(ValueError, '校验失败'):
                cli_update.download_release('https://dotasks.test', self.home)
            extract.assert_not_called()

    def test_cross_origin_release_is_rejected_before_download(self):
        manifest = {'version': 'v2', 'url': 'https://other.test/code.zip', 'sha256': '0' * 64, 'size': 3}
        with patch('taskboard.cli_update.read_json', return_value=manifest), patch('urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError):
                cli_update.download_release('https://dotasks.test', self.home)
            opener.assert_not_called()

    def test_failed_start_restores_old_runtime_plists_and_running_state(self):
        root = cli_update.default_data_home() / 'cli'
        old, new = root / 'releases/old', root / 'releases/new'
        old.mkdir(parents=True)
        new.mkdir()
        activate(root / 'current', old)
        launcher = self.home / '.local/bin/dotasks'
        launcher.parent.mkdir(parents=True)
        launcher.write_text('old launcher')
        service = Mock()
        service.state.return_value = 'running'
        paths = {n: self.home / (n + '.plist') for n in LABELS}
        for path in paths.values():
            path.write_text('old plist')
        service.plist.side_effect = lambda name: paths[name]
        config = AgentConfig('https://dotasks.test', 'alice', 'x' * 32)

        def install(*args, **kwargs):
            activate(root / 'current', new)
            for path in paths.values():
                path.write_text('new plist')
            launcher.write_text('new launcher')

        with patch.object(cli_update, 'load_agent_config', return_value=config), patch.object(cli_update, 'ensure_idle'), patch.object(cli_update, 'install', side_effect=install), patch.object(cli_update, 'wait_ready', side_effect=[RuntimeError('new service failed'), None]):
            with self.assertRaisesRegex(RuntimeError, '已恢复旧版本'):
                cli_update.apply_update(new, service)
        self.assertEqual(old.resolve(), (root / 'current').resolve())
        self.assertEqual('old launcher', launcher.read_text())
        self.assertTrue(all(p.read_text() == 'old plist' for p in paths.values()))
        self.assertEqual(2, service.start.call_count)

    def test_busy_requirement_or_task_blocks_update(self):
        config = AgentConfig('https://dotasks.test', 'alice', 'x' * 32)
        for board in ({'tasks': [{'active_run_status': 'running'}], 'requirements': []},
                      {'tasks': [], 'requirements': [{'status': 'decomposing'}]}):
            with patch('taskboard.cli_update.read_json', return_value={'result': board}):
                with self.assertRaisesRegex(RuntimeError, '活动任务'):
                    cli_update.ensure_idle(config)

    def test_running_or_uncertain_team_work_blocks_update(self):
        home = cli_update.default_data_home()
        home.mkdir(parents=True)
        (home / 'onboarding.json').write_text('{}')
        config = AgentConfig('https://dotasks.test', 'alice', 'x' * 32)
        for board in (
            {'actor_id': 'alice', 'jobs': [{'actor_id': 'alice', 'status': 'uncertain'}], 'tasks': []},
            {'actor_id': 'alice', 'jobs': [], 'tasks': [{'owner_account_id': 'alice', 'active_run_id': 'run1'}]},
        ):
            replies = [{'result': {'tasks': [], 'requirements': []}}, {'teams': [{'id': 'team1'}]}, board]
            with patch.object(cli_update, 'read_json', side_effect=replies):
                with self.assertRaisesRegex(RuntimeError, '团队'):
                    cli_update.ensure_idle(config)

    def test_partial_launch_failure_rolls_back_only_new_jobs(self):
        with patch('sys.platform', 'darwin'):
            service = BackgroundService()
        calls = []

        def run(*args, **kwargs):
            calls.append(args)
            if args[0] == 'bootstrap' and args[-1].endswith('cli.agent.plist'):
                raise RuntimeError('agent bootstrap failed')

        with patch.object(service, 'validate_installation'), patch('taskboard.cli_service.load_agent_config'), patch.object(service, 'states', return_value={'server': 'stopped', 'agent': 'stopped'}), patch.object(service, 'run', side_effect=run):
            with self.assertRaises(RuntimeError):
                service.start()
        self.assertIn(('bootout', service.target('server')), calls)
        self.assertNotIn(('bootout', f'{service.domain}/local.sanmws.dotasks-helper'), calls)


if __name__ == '__main__':
    unittest.main()
