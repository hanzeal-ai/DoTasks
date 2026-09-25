"""Run a relocated package with no external Python; no real user data is changed."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from taskboard.cli_update import extract_release
from taskboard.cli_install import validate_runtime

def isolated_environment(base, inherited):
    environment = {key: value for key, value in inherited.items()
                   if not key.startswith('DOTASKS_') and key not in ('PYTHONPATH', 'PYTHONHOME')}
    environment.update(PATH='/usr/bin:/bin', DOTASKS_HOME=str(base / 'data'),
                       DOTASKS_AGENT_CONFIG=str(base / 'data/agent.json'),
                       DOTASKS_REMOTE_SERVICE='0', PYTHONDONTWRITEBYTECODE='1')
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('archive', type=Path)
    parser.add_argument('--codex', action='store_true', help='Also handshake with the installed local Codex; no task/model turn')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='DoTasks portable ') as temporary:
        base = Path(temporary)
        runtime = extract_release(args.archive, base / 'relocated package')
        release = validate_runtime(runtime)
        environment = isolated_environment(base, os.environ)
        def run(argv, **kwargs):
            return subprocess.run(argv, env=environment, cwd=runtime, check=True, capture_output=True, text=True, timeout=40, **kwargs)
        assert 'team-bind' in run([str(runtime.parent / 'dotasks'), '--help']).stdout
        run([str(runtime / 'python/bin/python3'), '-B', '-c', 'import sqlite3, ssl, ctypes; assert ssl.create_default_context().get_ca_certs()'])
        response = run([str(runtime / 'scripts/mcp-server'), '--call-tool', 'set_dispatcher_enabled'], input='{"enabled":false}')
        assert json.loads(response.stdout)['enabled'] is False
        if args.codex:
            run([str(runtime / 'python/bin/python3'), '-B', '-c', '''from taskboard.app_server import CodexAppServerClient
from taskboard.runtime_paths import default_data_home
from pathlib import Path
client=CodexAppServerClient(default_data_home(),Path.cwd())
try:
 client.start()
 assert client.connected
finally:
 client.stop()
'''])
        print('Portable CLI, TLS trust and MCP verified:', release['version'])
        if args.codex:
            print('Local Codex app-server handshake verified; no model turn was started.')

        # Exercise real copying/launcher creation with launchctl isolated. No user jobs
        # are installed, but the emitted paths and copied interpreter are real.
        install_environment = dict(environment, HOME=str(base / 'isolated home'))
        install_environment.pop('DOTASKS_HOME', None)
        install_environment.pop('DOTASKS_AGENT_CONFIG', None)
        (base / 'isolated home').mkdir()
        subprocess.run([str(runtime / 'python/bin/python3'), '-B', '-c', """from pathlib import Path
from unittest.mock import patch
from taskboard.cli_install import install
from taskboard.cli_service import BackgroundService
with patch.object(BackgroundService,'state',return_value='stopped'), patch.object(BackgroundService,'run'):
 install(Path.cwd(), configure_path=False)
"""], env=install_environment, cwd=runtime, check=True, capture_output=True, text=True, timeout=60)
        import shutil
        shutil.rmtree(runtime.parent)
        launcher = base / 'isolated home/.local/bin/dotasks'
        installed = subprocess.run([str(launcher), '--help'], env=install_environment, check=True, capture_output=True, text=True, timeout=30)
        assert 'team-bind' in installed.stdout
        print('Installed CLI still runs after deleting the original download; launchctl was isolated.')


if __name__ == '__main__':
    main()
