#!/usr/bin/python3.11
"""Activate a preloaded DoTasks image using the operator-installed Compose file."""
import base64
import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

BASE = Path('/home/admin/dotasks')
COMPOSE = Path('/usr/local/libexec/dotasks-deploy/compose.yaml')
ENV = BASE / '.env'
CONTAINER = 'dotasks-cloud-dotasks-1'


def replace_image(text, image):
    lines = [line for line in text.splitlines() if line.split('=', 1)[0] != 'DOTASKS_IMAGE']
    return '\n'.join(lines + ['DOTASKS_IMAGE=' + image]) + '\n'


def write_env(text):
    descriptor, temporary = tempfile.mkstemp(prefix='.env-', dir=BASE)
    try:
        with os.fdopen(descriptor, 'w') as stream:
            stream.write(text)
        os.replace(temporary, ENV)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(image, commit):
    if not re.fullmatch(r'sha256:[a-f0-9]{64}', image) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Invalid image identity or commit')
    os.umask(0o077)
    with (BASE / 'deploy.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if ENV.is_symlink() or COMPOSE.is_symlink() or not ENV.is_file() or not COMPOSE.is_file():
            raise ValueError('Existing deployment configuration required')
        original = ENV.read_text()
        values = dict(line.split('=', 1) for line in original.splitlines() if '=' in line and not line.startswith('#'))
        compose = ['docker', 'compose', '--project-name', 'dotasks-cloud', '--env-file', str(ENV), '-f', str(COMPOSE)]
        def run(*args, **kwargs):
            return subprocess.run(args, check=True, timeout=180, **kwargs)
        # Inspect every existing tenant before stopping active execution.
        idle = '''from pathlib import Path
import sqlite3
root=Path('/data/dotasks')
for path in [root/'data/taskboard.db', *root.glob('tenants/*/data/taskboard.db')]:
    if not path.exists(): continue
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as db:
        active=db.execute("SELECT count(*) FROM task_runs WHERE status IN ('running','awaiting_thread')").fetchone()[0]
        active+=db.execute("SELECT count(*) FROM requirement_decomposition_runs WHERE status='running'").fetchone()[0]
        if active: raise SystemExit('Active work: postpone deployment')
'''
        run('docker', 'exec', '-i', CONTAINER, 'python', '-', input=idle, text=True)
        backup_root = BASE / 'backups'
        backup_root.mkdir(exist_ok=True)
        backup = Path(tempfile.mkdtemp(prefix='ssh-' + commit[:12] + '-', dir=backup_root))
        shutil.copy2(ENV, backup / 'env')
        shutil.copy2(COMPOSE, backup / 'compose.yaml')
        run(*compose, 'stop', 'dotasks')
        try:
            run('docker', 'cp', CONTAINER + ':/data/dotasks', str(backup / 'data'))
        except BaseException:
            # New code has not run: restarting the existing release is safe.
            run(*compose, 'up', '-d', '--no-deps', '--pull', 'never', 'dotasks')
            raise
        try:
            write_env(replace_image(original, image))
            run(*compose, 'up', '-d', '--no-deps', '--pull', 'never', '--wait', '--wait-timeout', '120', 'dotasks')
            actual = subprocess.check_output(['docker', 'inspect', '--format', '{{.Image}}', CONTAINER], text=True).strip()
            if actual != image:
                raise RuntimeError('Active container image does not match the uploaded release')
            port = int(values.get('DOTASKS_BIND_PORT', '8765'))
            path = '/api/auth/status' if values.get('DOTASKS_ACCOUNT_MODE') == 'multi' else '/api/health'
            headers = {'Host': urllib.parse.urlsplit(values['DOTASKS_PUBLIC_URL']).netloc}
            if path == '/api/health':
                auth = values['DOTASKS_HTTP_USER'] + ':' + values['DOTASKS_HTTP_PASSWORD']
                headers['Authorization'] = 'Basic ' + base64.b64encode(auth.encode()).decode()
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            request = urllib.request.Request('http://127.0.0.1:' + str(port) + path, headers=headers)
            with opener.open(request, timeout=10) as response:
                if not isinstance(json.load(response), dict):
                    raise RuntimeError('Invalid health response')
        except BaseException:
            run(*compose, 'stop', 'dotasks')
            # New code may migrate SQLite; never restart old code against changed data.
            print('Release stopped. Preserve current data and repair forward. Backup: ' + str(backup), file=sys.stderr)
            raise
        (BASE / 'source.sha').write_text(commit + '\n')
        print('DoTasks image and HTTP health verified; backup: ' + str(backup))


if __name__ == '__main__':
    main(*sys.argv[1:])
