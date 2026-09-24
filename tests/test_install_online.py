"""Exercise the shipped online installer without network or user installation."""
from contextlib import redirect_stdout
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch
import zipfile


class OnlineInstallerTest(unittest.TestCase):
    def setUp(self):
        script = (Path(__file__).resolve().parents[1] / 'scripts/install-online.sh').read_text()
        self.program = script.split("<<'PY'\n", 1)[1].rsplit('\nPY', 1)[0]
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('DoTasksCLI/runtime/release.json', json.dumps({'version': 'test-v1'}))
            package.writestr('DoTasksCLI/install-cli', '# fixture')
        self.archive = archive.getvalue()
        self.manifest = {'version': 'test-v1', 'url': '/downloads/cli/test.zip',
                         'size': len(self.archive), 'sha256': hashlib.sha256(self.archive).hexdigest()}
        self.terminal = io.StringIO('alice\n')
        self.output = io.StringIO()
        self.opener = Mock()
        self.enterContext(patch('urllib.request.build_opener', return_value=self.opener))
        real_open = open
        self.open_terminal = self.enterContext(patch('builtins.open', side_effect=lambda path, *args, **kwargs: self.terminal if path == '/dev/tty' else real_open(path, *args, **kwargs)))
        self.run = self.enterContext(patch('subprocess.run'))
        self.enterContext(patch.object(sys, 'argv', ['-', '--replace-helper']))
        self.enterContext(redirect_stdout(self.output))

    def execute(self):
        self.opener.open.side_effect = [io.BytesIO(json.dumps(self.manifest).encode()), io.BytesIO(self.archive)]
        exec(compile(self.program, 'install-online.sh', 'exec'), {})

    def test_install_then_init_reads_terminal_instead_of_script_stdin(self):
        def run(argv, **kwargs):
            if argv[-1] == 'init':
                self.assertIs(kwargs['stdin'], self.terminal)
                self.assertEqual('alice\n', kwargs['stdin'].readline())
            return subprocess.CompletedProcess(argv, 0)
        self.run.side_effect = run
        with patch.object(sys, 'stdin', io.StringIO('script, not username')):
            self.execute()
        install, initialize = self.run.call_args_list
        self.assertEqual('--replace-helper', install.args[0][-1])
        self.assertEqual([str(Path.home() / '.local/bin/dotasks'), 'init'], initialize.args[0])
        self.assertTrue(self.terminal.closed)
        for stage in ('[2/5]', '[3/5]', '[4/5]', '[5/5]'):
            self.assertIn(stage, self.output.getvalue())

    def test_failed_install_does_not_start_initialization(self):
        self.run.side_effect = subprocess.CalledProcessError(1, ['installer'])
        with self.assertRaises(subprocess.CalledProcessError):
            self.execute()
        self.assertEqual(1, self.run.call_count)

    def test_failed_init_reports_installed_state_and_resume_command(self):
        self.run.side_effect = [subprocess.CompletedProcess([], 0), subprocess.CalledProcessError(2, ['init'])]
        with self.assertRaises(SystemExit) as result:
            self.execute()
        self.assertEqual(2, result.exception.code)
        self.assertIn('CLI 已安装，初始化尚未完成', self.output.getvalue())
        self.assertIn('dotasks init', self.output.getvalue())
        self.assertTrue(self.terminal.closed)

    def test_no_terminal_stops_before_downloading_or_installing(self):
        self.open_terminal.side_effect = OSError('no controlling terminal')
        with self.assertRaisesRegex(SystemExit, '交互式终端'):
            self.execute()
        self.opener.open.assert_not_called()
        self.run.assert_not_called()

    def test_bad_checksum_prevents_install_and_initialization(self):
        self.manifest['sha256'] = '0' * 64
        with self.assertRaisesRegex(SystemExit, 'checksum'):
            self.execute()
        self.run.assert_not_called()
