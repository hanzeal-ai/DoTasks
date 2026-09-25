from pathlib import Path
import os
import subprocess
import sys
import tempfile
import unittest


class AlibabaReleaseTest(unittest.TestCase):
    def test_retired_entrypoint_never_calls_cloud_or_generates_executable_plan(self):
        script = Path(__file__).resolve().parents[1] / 'scripts/deploy-aliyun-cli.py'
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / 'called'
            for name in ('aliyun', 'gh', 'docker'):
                executable = Path(directory) / name
                executable.write_text('#!/bin/sh\ntouch "' + str(marker) + '"\n')
                executable.chmod(0o755)
            for arguments in ([], ['--execute', '--region', 'cn-hangzhou'], ['--help']):
                result = subprocess.run([sys.executable, str(script), *arguments], capture_output=True,
                                        text=True, env={**os.environ, 'PATH': directory})
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, '')
                self.assertIn('retired', result.stderr)
                self.assertFalse(marker.exists())
