#!/usr/bin/python3.11
"""Apply only host resource/log/loopback settings; keep exact running images."""
from contextlib import ExitStack
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

DT = Path('/usr/local/libexec/dotasks-deploy/compose.yaml')
MF = Path('/usr/local/libexec/markfix-deploy/compose.preview.yaml')
CURRENT = Path('/home/admin/markfix/current').resolve()
ENV = Path('/home/admin/dotasks/.env')
ROOT = Path('/var/lib/hanzeal-ops')
POLICY = {'dotasks': ('256m', '64m', 1024, 128), 'postgres': ('384m', '96m', 1024, 128),
          'api': ('384m', '128m', 1024, 256), 'dashboard': ('64m', '16m', 512, 64)}
NAMES = {'dotasks': 'dotasks-cloud-dotasks-1', 'api': 'markfix-preview-api-1',
         'postgres': 'markfix-preview-postgres-1', 'dashboard': 'markfix-preview-dashboard-1'}


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=180, **kwargs)


def out(*args):
    return run(*args, capture_output=True, text=True).stdout


def configure(source, services):
    for name in services:
        start = source.index('  ' + name + ':\n')
        # Find the next service rather than an indented property.
        lines = source[start:].splitlines(keepends=True)
        block = []
        for line in lines:
            if block and line.startswith('  ') and not line.startswith('   ') and line.strip():
                break
            if block and line and not line[0].isspace() and line.strip():
                break
            block.append(line)
        old = ''.join(block)
        if any('    ' + key + ':' in old for key in ('mem_limit', 'mem_reservation', 'cpu_shares', 'pids_limit', 'logging')):
            raise ValueError('Existing resource policy requires review: ' + name)
        mem, reserve, shares, pids = POLICY[name]
        insertion = f'''    mem_limit: {mem}
    mem_reservation: {reserve}
    cpu_shares: {shares}
    pids_limit: {pids}
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: '3'
'''
        updated = old.replace('    restart: unless-stopped\n', '    restart: unless-stopped\n' + insertion, 1)
        if updated == old:
            raise ValueError('Unexpected service layout')
        if name == 'dashboard':
            if "- '8766:80'" not in updated:
                raise ValueError('Unexpected dashboard binding')
            updated = updated.replace("- '8766:80'", "- '127.0.0.1:8766:80'")
        source = source[:start] + updated + source[start + len(old):]
    return source


def commands():
    dt = ['docker', 'compose', '--project-name', 'dotasks-cloud', '--env-file', str(ENV), '-f', str(DT)]
    mf = ['docker', 'compose', '--project-name', 'markfix-preview', '--env-file', '/home/admin/markfix/app.env',
          '--env-file', str(CURRENT / 'images.env'), '-f', str(CURRENT / 'compose.yaml')]
    return dt, mf


def image_ids():
    return {name: out('docker', 'inspect', '--format', '{{.Image}}', container).strip() for name, container in NAMES.items()}


def verify(dt, mf, expected):
    for command, services in [(dt, ['dotasks']), (mf, ['postgres', 'api', 'dashboard'])]:
        config = json.loads(out(*command, 'config', '--format', 'json'))
        for name in services:
            identity = out('docker', 'image', 'inspect', '--format', '{{.Id}}', config['services'][name]['image']).strip()
            if identity != expected[name]:
                raise RuntimeError('Configured image differs from running image: ' + name)


def health():
    for url in ['https://dotasks.hanzeal.com/api/auth/status', 'https://markfix.hanzeal.com/v1/health',
                'https://markfix.hanzeal.com/', 'https://carryon.hanzeal.com/healthz']:
        run('curl', '--fail', '--silent', '--show-error', '--max-time', '15', url, stdout=subprocess.DEVNULL)


def main():
    os.umask(0o077)
    os.environ['MARKFIX_ENV_FILE'] = '/home/admin/markfix/app.env'
    os.environ['MARKFIX_DOWNLOAD_ROOT'] = '/home/admin/markfix/downloads'
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with ExitStack() as stack:
        for filename in ['/home/admin/dotasks/deploy.lock', '/home/admin/markfix/deploy.lock']:
            lock = stack.enter_context(open(filename, 'a'))
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        dt, mf = commands()
        expected = image_ids()
        verify(dt, mf, expected)
        health()
        # Existing authoritative deployment preflight; no schema or business changes.
        run('docker', 'exec', '-i', NAMES['dotasks'], 'python', '-', input='''from pathlib import Path
import sqlite3
for p in [Path('/data/dotasks/data/taskboard.db'), *Path('/data/dotasks/tenants').glob('*/data/taskboard.db')]:
 if not p.exists(): continue
 with sqlite3.connect(p.as_uri()+'?mode=ro',uri=True) as db:
  for table, condition in [('task_runs', "status IN ('running','awaiting_thread')"), ('requirement_decomposition_runs', "status='running'")]:
   if db.execute('SELECT count(*) FROM '+table+' WHERE '+condition).fetchone()[0]:
    raise SystemExit('Active DoTasks work: maintenance postponed')
''', text=True)
        files = [DT, MF, CURRENT / 'compose.yaml', ENV]
        changes = {DT: configure(DT.read_text(), ['dotasks']), MF: configure(MF.read_text(), ['postgres', 'api', 'dashboard']),
                   CURRENT / 'compose.yaml': configure((CURRENT / 'compose.yaml').read_text(), ['postgres', 'api', 'dashboard'])}
        env = [line for line in ENV.read_text().splitlines() if line.split('=', 1)[0] != 'DOTASKS_BIND_ADDRESS']
        changes[ENV] = '\n'.join(env + ['DOTASKS_BIND_ADDRESS=127.0.0.1']) + '\n'
        backup = Path(tempfile.mkdtemp(prefix='config-', dir=ROOT))
        for index, path in enumerate(files):
            shutil.copy2(path, backup / str(index))
        (backup / 'manifest.json').write_text(json.dumps({'files': [str(p) for p in files], 'images': expected}))
        print('Configuration recovery copies: ' + str(backup), flush=True)
        try:
            for path, content in changes.items():
                path.write_text(content)
            verify(dt, mf, expected)
            # No build, pull, migrations, or application release. DB is restarted first.
            for command, services in [(mf, ['postgres']), (mf, ['api', 'dashboard']), (dt, ['dotasks'])]:
                run(*command, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', *services)
            if image_ids() != expected:
                raise RuntimeError('Image identity changed')
            health()
        except BaseException as original:
            errors = []
            for index, path in enumerate(files):
                try:
                    shutil.copy2(backup / str(index), path)
                except Exception as exc:
                    errors.append('restore file ' + str(path) + ': ' + type(exc).__name__)
            # Same images and no migration: restoring only old config is safe here.
            for command, services in [(mf, ['postgres']), (mf, ['api', 'dashboard']), (dt, ['dotasks'])]:
                try:
                    run(*command, 'up', '-d', '--no-deps', '--no-build', '--pull', 'never', '--wait', '--wait-timeout', '120', *services)
                except Exception as exc:
                    errors.append('restore services ' + ','.join(services) + ': ' + type(exc).__name__)
            try:
                if image_ids() != expected:
                    errors.append('restored images do not match')
                health()
            except Exception as exc:
                errors.append('restored health: ' + type(exc).__name__)
            if errors:
                raise RuntimeError('RECOVERY INCOMPLETE: ' + '; '.join(errors) + '; config copies: ' + str(backup)) from original
            raise RuntimeError('Change failed; original configuration, images and HTTPS restored') from original
        print(json.dumps({'same_images_verified': expected, 'https_health': 'passed'}))


if __name__ == '__main__':
    main()
