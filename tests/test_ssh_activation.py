import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('activation', Path(__file__).resolve().parents[1] / 'deployment/activate.py')
activation = importlib.util.module_from_spec(spec)
spec.loader.exec_module(activation)
IMAGE = 'sha256:' + 'a' * 64
COMMIT = 'b' * 40


class ActivationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = self.root / '.env'
        self.original = '# keep settings\nDOTASKS_IMAGE=old:release\nDOTASKS_ACCOUNT_MODE=multi\nDOTASKS_PUBLIC_URL=https://dotasks.hanzeal.com\nCUSTOM_SETTING=keep\n'
        self.env.write_text(self.original)
        self.compose = self.root / 'compose.yaml'
        self.compose.write_text('operator-managed configuration')
        for name, value in [('BASE', self.root), ('ENV', self.env), ('COMPOSE', self.compose)]:
            patcher = patch.object(activation, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.calls = []

    def run_activation(self, failure=''):
        def run(args, **kwargs):
            self.calls.append(args)
            if failure == 'active' and 'exec' in args:
                raise subprocess.CalledProcessError(1, args)
            if failure == 'backup' and 'cp' in args:
                raise subprocess.CalledProcessError(1, args)
            if failure == 'start' and '--wait' in args:
                raise subprocess.CalledProcessError(1, args)
            return subprocess.CompletedProcess(args, 0)
        with patch.object(activation.subprocess, 'run', side_effect=run), patch.object(activation.subprocess, 'check_output', return_value=IMAGE), patch.object(activation.urllib.request, 'build_opener') as opener:
            opener.return_value.open.return_value = io.BytesIO(json.dumps({'authenticated': False}).encode())
            activation.main(IMAGE, COMMIT)

    def test_success_preserves_config_and_backs_up_before_activation(self):
        self.run_activation()
        self.assertIn('CUSTOM_SETTING=keep', self.env.read_text())
        self.assertIn('DOTASKS_ACCOUNT_MODE=multi', self.env.read_text())
        self.assertIn('DOTASKS_IMAGE=' + IMAGE, self.env.read_text())
        self.assertEqual((self.root / 'source.sha').read_text().strip(), COMMIT)
        self.assertEqual(next((self.root / 'backups').glob('*/env')).read_text(), self.original)
        backup = next(i for i, c in enumerate(self.calls) if 'cp' in c)
        stop = next(i for i, c in enumerate(self.calls) if 'stop' in c)
        up = next(i for i, c in enumerate(self.calls) if 'up' in c)
        self.assertLess(stop, backup)
        self.assertLess(backup, up)
        self.assertIn('--pull', self.calls[up])
        self.assertIn('never', self.calls[up])

    def test_active_work_refuses_to_stop(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_activation('active')
        self.assertFalse(any('stop' in c for c in self.calls))
        self.assertEqual(self.env.read_text(), self.original)

    def test_backup_failure_restarts_only_previous_release(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_activation('backup')
        self.assertIn('up', self.calls[-1])
        self.assertEqual(self.env.read_text(), self.original)

    def test_new_release_failure_stops_without_unsafe_data_rollback(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_activation('start')
        self.assertEqual(self.calls[-1][-2:], ('stop', 'dotasks'))
        self.assertFalse((self.root / 'source.sha').exists())
        self.assertEqual(next((self.root / 'backups').glob('*/env')).read_text(), self.original)


if __name__ == '__main__':
    unittest.main()
