#!/usr/bin/python3.11
"""Host-specific, root-only maintenance for the existing Hanzeal services."""
import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

ROOT = Path('/var/lib/hanzeal-ops')
CONTAINERS = ['dotasks-cloud-dotasks-1', 'markfix-preview-api-1',
              'markfix-preview-dashboard-1', 'markfix-preview-postgres-1']


def run(*args, **kwargs):
    return subprocess.run(args, check=True, timeout=30, **kwargs)


def output(*args):
    return run(*args, capture_output=True, text=True).stdout


def inspect():
    return json.loads(output('docker', 'inspect', *CONTAINERS))


def metrics():
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    mem = {line.split(':')[0]: int(line.split()[1]) * 1024
           for line in Path('/proc/meminfo').read_text().splitlines()}
    disk = shutil.disk_usage('/')
    alerts = []
    try:
        status = inspect()
    except (subprocess.SubprocessError, json.JSONDecodeError):
        status = []
        alerts.append('container inventory failed or expected container missing')
    if mem['MemAvailable'] < 256 * 1024**2:
        alerts.append('available memory below 256 MiB')
    if disk.free / disk.total < .15:
        alerts.append('disk free below 15 percent')
    for item in status:
        state = item['State']
        if not state['Running'] or state.get('Health', {}).get('Status', 'healthy') != 'healthy':
            alerts.append(item['Name'] + ' unavailable')
        if state.get('OOMKilled'):
            alerts.append(item['Name'] + ' OOM')
    carryon = subprocess.run(['systemctl', 'is-active', 'carryon-gateway'], capture_output=True, text=True, timeout=10).stdout.strip()
    if carryon != 'active':
        alerts.append('carryon unavailable')
    try:
        container_stats = output('docker', 'stats', '--no-stream', '--format',
                                 '{{.Name}} CPU={{.CPUPerc}} MEM={{.MemUsage}} PIDS={{.PIDs}}').splitlines()
    except subprocess.SubprocessError:
        container_stats = []
        alerts.append('container stats unavailable')
    now = datetime.now(timezone.utc)
    sample = {'at': now.isoformat(), 'available_bytes': mem['MemAvailable'],
              'load': os.getloadavg(), 'disk_free_bytes': disk.free,
              'carryon': carryon,
              'containers': container_stats,
              'restarts': {x['Name']: x['RestartCount'] for x in status}, 'alerts': alerts}
    folder = ROOT / 'metrics'
    folder.mkdir(mode=0o700, exist_ok=True)
    with (folder / (now.strftime('%Y-%m-%d') + '.jsonl')).open('a') as stream:
        stream.write(json.dumps(sample) + '\n')
    for path in folder.glob('????-??-??.jsonl'):
        if path.is_file() and not path.is_symlink() and time.time() - path.stat().st_mtime > 14 * 86400:
            path.unlink()
    print(json.dumps(sample))
    if alerts:
        raise SystemExit(1)


def inventory():
    details = inspect()
    print(json.dumps({'running_images': {x['Name']: x['Image'] for x in details}}))
    print(output('docker', 'system', 'df'))
    print('Keep running/stopped container images and retained release versions. No automatic image/data deletion.')


if __name__ == '__main__':
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['metrics', 'inventory'])
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error('root is required')
    ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (ROOT / 'metrics.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        globals()[args.action]()
