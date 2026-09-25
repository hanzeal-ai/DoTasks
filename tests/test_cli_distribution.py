from contextlib import redirect_stdout
import hashlib
import io
import json
import os
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from taskboard import cli_distribution as distribution, cli_install, cli_update
from taskboard.cli_service import BackgroundService, LABELS


class DistributionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.enterContext(patch('pathlib.Path.home', return_value=self.home))
        self.enterContext(patch.dict(os.environ, {'SHELL': '/bin/zsh'}, clear=True))
        self.enterContext(patch('sys.platform', 'darwin'))
        self.enterContext(patch.object(BackgroundService, 'state', return_value='stopped'))
        self.enterContext(patch.object(BackgroundService, 'legacy_running', return_value=False))
        self.launch = self.enterContext(patch.object(BackgroundService, 'run'))
        self.run = self.enterContext(patch('taskboard.cli_install.subprocess.run', return_value=subprocess.CompletedProcess([], 0)))
        self.enterContext(redirect_stdout(io.StringIO()))

    def runtime(self, name='download/runtime', bundled=False):
        root = self.home / name
        files = ['taskboard/cli.py', 'taskboard/cli_service.py', 'taskboard/cli_onboarding.py', 'taskboard/server.py', 'taskboard/agent.py', 'scripts/mcp-server']
        if bundled:
            files.append('python/bin/python3')
        for name in files:
            path = root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n')
            path.chmod(0o755)
        (root / 'release.json').write_text(json.dumps({'version': 'test-v1', 'files': {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}}))
        return root

    def test_portable_install_uses_copied_interpreter_after_download_removed(self):
        source = self.runtime(bundled=True)
        launcher = cli_install.install(source)
        shutil.rmtree(source.parent)
        for name in LABELS:
            data = plistlib.loads(BackgroundService().plist(name).read_bytes())
            python = Path(data['ProgramArguments'][0])
            self.assertTrue(python.is_file())
            self.assertIn('/cli/current/python/bin/python3', str(python))
        self.assertIn('/cli/current/python/bin/python3', launcher.read_text())
        self.assertNotIn(str(source), launcher.read_text())

    def test_brew_install_preserves_opt_paths_and_removes_only_owned_old_launcher(self):
        runtime = self.runtime('Cellar/dotasks/v1/libexec/runtime')
        opt = self.home / 'opt/dotasks'
        opt.parent.mkdir()
        opt.symlink_to(runtime.parents[1])
        stable = opt / 'libexec/runtime'
        old = self.home / '.local/bin/dotasks'
        old.parent.mkdir(parents=True)
        old.write_text('#!/bin/sh\n# DoTasks CLI\n')
        before = {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob('*') if p.is_file()}
        cli_install.install(stable, homebrew=True)
        self.assertFalse(old.exists())
        self.assertFalse((self.home / '.zshrc').exists())
        for name in LABELS:
            data = plistlib.loads(BackgroundService().plist(name).read_bytes())
            self.assertEqual(str(stable), data['WorkingDirectory'])
        self.assertEqual(before, {p.relative_to(runtime): p.read_bytes() for p in runtime.rglob('*') if p.is_file()})

    def test_mixed_background_installations_are_rejected(self):
        runtime = self.runtime()
        cli_install.install(runtime)
        service = BackgroundService()
        path = service.plist('agent')
        payload = plistlib.loads(path.read_bytes())
        payload['WorkingDirectory'] = str(runtime)
        path.write_bytes(plistlib.dumps(payload))
        with self.assertRaisesRegex(RuntimeError, '不同安装'):
            service.validate_installation()

    def test_brew_cannot_silently_initialize_an_existing_standalone_install(self):
        source = self.runtime()
        cli_install.install(source)
        brew = self.runtime('brew/runtime')
        with patch.object(distribution, 'runtime_root', return_value=brew), patch.object(distribution, 'homebrew_runtime', return_value=brew):
            with self.assertRaisesRegex(RuntimeError, '另一种安装'):
                distribution.prepare_initialization()

    def test_homebrew_update_does_not_download_or_mutate_installation(self):
        with patch.object(distribution, 'homebrew_runtime', return_value=self.home), patch.object(cli_update, 'download_release') as download:
            cli_update.update()
        download.assert_not_called()
        self.launch.assert_not_called()

    def test_wrong_brew_python_is_rejected_before_using_it(self):
        with patch.dict(os.environ, {'DOTASKS_BREW_PYTHON': '/wrong/python'}):
            with self.assertRaisesRegex(RuntimeError, '解释器不一致'):
                distribution.runtime_python(self.home)

    def test_portable_manifest_with_wrong_architecture_is_rejected(self):
        runtime = self.runtime(bundled=True)
        manifest = json.loads((runtime / 'release.json').read_text())
        manifest.update(platform='macos-x86_64', python='python/bin/python3')
        (runtime / 'release.json').write_text(json.dumps(manifest))
        with patch.object(distribution, 'platform_key', return_value='macos-arm64'):
            with self.assertRaisesRegex(ValueError, '架构不匹配'):
                cli_install.validate_runtime(runtime)


class VerificationIsolationTest(unittest.TestCase):
    def test_inherited_remote_credentials_cannot_escape_sandbox(self):
        import runpy
        module = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/verify-portable-cli.py'))
        environment = module['isolated_environment'](Path('/temporary/smoke'), {
            'DOTASKS_AGENT_CONFIG': '/real/account.json',
            'DOTASKS_REMOTE_SERVICE': '1', 'DOTASKS_CLOUD_URL': 'https://real.invalid',
            'DOTASKS_AGENT_TOKEN': 'secret', 'DOTASKS_AGENT_ID': 'real',
            'DOTASKS_HOME': '/real/data', 'PYTHONPATH': '/real/modules',
            'PYTHONHOME': '/real/python', 'CODEX_HOME': '/local/codex',
        })
        self.assertEqual(environment['DOTASKS_REMOTE_SERVICE'], '0')
        self.assertEqual(environment['DOTASKS_AGENT_CONFIG'], '/temporary/smoke/data/agent.json')
        self.assertEqual(environment['DOTASKS_HOME'], '/temporary/smoke/data')
        self.assertNotIn('DOTASKS_AGENT_TOKEN', environment)
        self.assertNotIn('DOTASKS_CLOUD_URL', environment)
        self.assertNotIn('DOTASKS_AGENT_ID', environment)
        self.assertNotIn('PYTHONPATH', environment)
        self.assertNotIn('PYTHONHOME', environment)
        self.assertEqual(environment['CODEX_HOME'], '/local/codex')
