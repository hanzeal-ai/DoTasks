import importlib.util
from pathlib import Path
import subprocess
import json
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('host_ops', Path(__file__).resolve().parents[1] / 'deployment/host/ops.py')
ops = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ops)


class HostOpsTest(unittest.TestCase):
    def test_inventory_is_read_only_and_excludes_container_credentials(self):
        details = [{'Name': 'app', 'Image': 'sha256:abc', 'Config': {'Env': ['SECRET=hidden']}}]
        with patch.object(ops, 'inspect', return_value=details), patch.object(ops, 'output', return_value='summary') as command, patch('builtins.print') as printer:
            ops.inventory()
        command.assert_called_once_with('docker', 'system', 'df')
        self.assertNotIn('hidden', str(printer.call_args_list))
        self.assertIn('sha256:abc', str(printer.call_args_list))

    def test_missing_container_still_records_host_metrics_and_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            real_read = Path.read_text
            def read(path, *args, **kwargs):
                if str(path) == '/proc/meminfo':
                    return 'MemAvailable: 1048576 kB\n'
                return real_read(path, *args, **kwargs)
            with patch.object(ops, 'ROOT', root), patch.object(ops, 'inspect', side_effect=subprocess.CalledProcessError(1, 'docker')), patch.object(ops, 'output', return_value=''), patch.object(ops.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, stdout='active\n')), patch.object(Path, 'read_text', read), patch('builtins.print'):
                with self.assertRaises(SystemExit) as failure:
                    ops.metrics()
                self.assertEqual(failure.exception.code, 1)
            sample = json.loads(next((root / 'metrics').glob('*.jsonl')).read_text())
            self.assertEqual(sample['available_bytes'], 1024**3)
            self.assertTrue(any('missing' in alert for alert in sample['alerts']))

    def test_command_timeout_is_bounded(self):
        with patch.object(ops.subprocess, 'run', side_effect=subprocess.TimeoutExpired('docker', 30)) as run:
            with self.assertRaises(subprocess.TimeoutExpired):
                ops.output('docker', 'stats')
        self.assertEqual(run.call_args.kwargs['timeout'], 30)
