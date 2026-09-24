"""Prepare or execute an immutable DoTasks release through Alibaba Cloud CLI."""
from __future__ import annotations
import argparse
import base64
import json
import re
import shlex
import subprocess
import time


def remote_script(image, root):
    if not re.fullmatch(r'ghcr.io/[a-z0-9._/-]+:[0-9a-f]{40}', image):
        raise ValueError('Use an immutable GHCR commit tag')
    if not root.startswith('/') or '\n' in root:
        raise ValueError('Deployment root must be an absolute path')
    script = r'''#!/bin/bash
set -Eeuo pipefail
umask 077
cd __ROOT__
IMAGE=__IMAGE__
test -f .env
test -f .dotasks-cloud/compose.yaml
test ! -L .env
test ! -L .dotasks-cloud/compose.yaml
docker pull "$IMAGE"
# Check every existing tenant as well as the preserved single-account database.
docker exec -i dotasks-cloud-dotasks-1 python - <<'IDLE'
from pathlib import Path
import sqlite3
root=Path('/data/dotasks')
paths=[root/'data/taskboard.db', *root.glob('tenants/*/data/taskboard.db')]
for path in paths:
    if not path.exists(): continue
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
        active=connection.execute("SELECT count(*) FROM task_runs WHERE status IN ('running','awaiting_thread')").fetchone()[0]
        active+=connection.execute("SELECT count(*) FROM requirement_decomposition_runs WHERE status='running'").fetchone()[0]
        if active: raise SystemExit('Active work: postpone deployment')
IDLE
BACKUP="backups/cli-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$BACKUP"
cp -p .env "$BACKUP/env"
cp -p .dotasks-cloud/compose.yaml "$BACKUP/compose.yaml"
COMPOSE=(docker compose --project-name dotasks-cloud --env-file .env -f .dotasks-cloud/compose.yaml)
rollback() {
  code=$?
  trap - ERR
  cp -p "$BACKUP/env" .env
  cp -p "$BACKUP/compose.yaml" .dotasks-cloud/compose.yaml
  "${COMPOSE[@]}" up -d --no-build dotasks
  echo "Release failed; previous configuration restored. Backup: $BACKUP" >&2
  exit "$code"
}
trap rollback ERR
"${COMPOSE[@]}" stop dotasks
docker run --rm --user 0:0 --volumes-from dotasks-cloud-dotasks-1:ro -v "$PWD/$BACKUP:/backup" "$IMAGE" python -c 'import os,tarfile; os.umask(0o077); t=tarfile.open("/backup/data.tar.gz","w:gz"); t.add("/data/dotasks",arcname="dotasks"); t.close()'
FILE_CONTAINER=$(docker create "$IMAGE")
docker cp "$FILE_CONTAINER:/app/scripts/deploy-cloud-ip" "$BACKUP/deploy-cloud-ip"
docker rm "$FILE_CONTAINER" >/dev/null
python3 - <<'CONFIG'
from pathlib import Path
p=Path('.env')
lines=p.read_text().splitlines()
values={'DOTASKS_ACCOUNT_MODE':'multi','DOTASKS_PUBLIC_URL':'https://dotasks.hanzeal.com'}
lines=[line for line in lines if line.split('=',1)[0] not in values]
lines.extend(k+'='+v for k,v in values.items())
p.write_text('\n'.join(lines)+'\n')
p.chmod(0o600)
CONFIG
/bin/sh "$BACKUP/deploy-cloud-ip" --public-url https://dotasks.hanzeal.com --image "$IMAGE" --reuse-env --no-print-secrets
python3 - <<'VERIFY'
import json,urllib.request,urllib.error
base='http://127.0.0.1:8765'
def get(path):
    return urllib.request.urlopen(urllib.request.Request(base+path,headers={'Host':'dotasks.hanzeal.com'}),timeout=10)
with get('/downloads/cli/latest.json') as r:
    manifest=json.load(r)
    assert len(manifest['sha256'])==64
with get('/install.sh') as r: assert b'Python 3.14' in r.read()
try: get('/api/board')
except urllib.error.HTTPError as e: assert e.code==401
else: raise AssertionError('Unauthenticated board is accessible')
try: get('/api/cli/init')
except urllib.error.HTTPError as e: assert e.code==405
else: raise AssertionError('Unexpected registration method')
print('Shared account and CLI download checks passed:', manifest['version'])
VERIFY
trap - ERR
echo "Deployment verified; rollback configuration and data backup: $BACKUP"
'''
    return script.replace('__ROOT__', shlex.quote(root)).replace('__IMAGE__', shlex.quote(image))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--deploy-root', default='/home/admin/dotasks')
    parser.add_argument('--working-user', default='admin')
    parser.add_argument('--execute', action='store_true', help='Execute only after independent release review')
    args = parser.parse_args()
    content = remote_script(args.image, args.deploy_root)
    if not args.execute:
        print(content)
        return
    common = ['--region', args.region, '--biz-region-id', args.region, '--instance-id', args.instance_id]
    def call(operation, *options):
        completed = subprocess.run(['aliyun', 'swas-open', operation, *common, *options], capture_output=True, text=True, check=True, timeout=60)
        return json.loads(completed.stdout)
    result = call('run-command', '--name', 'dotasks-standalone-cli-release', '--type', 'RunShellScript',
                  '--working-user', args.working_user, '--timeout', '900', '--command-content', content)
    invocation = result['InvokeId']
    print('Alibaba invocation:', invocation, flush=True)
    deadline = time.monotonic() + 960
    while time.monotonic() < deadline:
        result = call('describe-invocation-result', '--invoke-id', invocation)['InvocationResult']
        if result.get('InvokeRecordStatus') == 'Finished':
            print(base64.b64decode(result.get('Output', '')).decode(errors='replace'))
            if result.get('ExitCode') != 0 or result.get('InvocationStatus') != 'Success':
                raise SystemExit('Deployment failed. Inspect the invocation and rollback result before retrying.')
            return
        time.sleep(5)
    raise SystemExit(f'Invocation still pending: {invocation}; query this invocation rather than deploying again.')


if __name__ == '__main__':
    main()
