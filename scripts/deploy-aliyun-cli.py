"""Prepare or execute an immutable DoTasks release through Alibaba Cloud CLI."""
from __future__ import annotations
import argparse
import base64
import hashlib
import json
import re
import shlex
import subprocess
import time
import tempfile
import urllib.request
import urllib.error
from urllib.parse import urlparse
import zipfile


def remote_script(image, root, artifact=None):
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
    if artifact:
        url, digest = artifact
        if urlparse(url).scheme != 'https' or not re.fullmatch(r'[0-9a-f]{64}', digest):
            raise ValueError('Invalid image artifact')
        transfer = '''python3 - <<'IMAGE_TRANSFER'
import hashlib, pathlib, shutil, subprocess, tempfile, urllib.request, zipfile
with tempfile.TemporaryDirectory(prefix='dotasks-image-') as temporary:
    archive=pathlib.Path(temporary)/'image.zip'
    digest=hashlib.sha256()
    total=0
    with urllib.request.urlopen(__URL__,timeout=60) as source, archive.open('wb') as target:
        while block:=source.read(1024*1024):
            total+=len(block)
            if total>1024*1024*1024: raise ValueError('Image artifact too large')
            digest.update(block)
            target.write(block)
    if digest.hexdigest()!=__DIGEST__: raise ValueError('Image artifact checksum mismatch')
    with zipfile.ZipFile(archive) as package:
        if package.namelist()!=['dotasks-cloud.tar'] or package.infolist()[0].file_size>2*1024*1024*1024:
            raise ValueError('Unexpected image artifact')
        with package.open('dotasks-cloud.tar') as source, (pathlib.Path(temporary)/'image.tar').open('wb') as target:
            shutil.copyfileobj(source,target)
    subprocess.run(['docker','load','-i',str(pathlib.Path(temporary)/'image.tar')],check=True)
IMAGE_TRANSFER
docker image inspect "$IMAGE" >/dev/null'''
        script = script.replace('docker pull "$IMAGE"', transfer.replace('__URL__', repr(url)).replace('__DIGEST__', repr(digest)))
        script = script.replace('--reuse-env --no-print-secrets', '--reuse-env --no-print-secrets --loaded-image')
    return script.replace('__ROOT__', shlex.quote(root)).replace('__IMAGE__', shlex.quote(image))


def github_artifact(artifact_id, image):
    """Authorize locally; send only a short-lived artifact URL, never a GitHub token."""
    endpoint = f'/repos/hanzeal-ai/DoTasks/actions/artifacts/{artifact_id}'
    metadata = json.loads(subprocess.run(['gh', 'api', endpoint], capture_output=True, text=True, check=True).stdout)
    revision = image.rsplit(':', 1)[1]
    if metadata['name'] != 'dotasks-image-' + revision or metadata.get('expired') or metadata.get('workflow_run', {}).get('head_sha') != revision:
        raise ValueError('Artifact does not match the requested immutable release')
    token = subprocess.run(['gh', 'auth', 'token'], capture_output=True, text=True, check=True).stdout.strip()
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs): return None
    def location():
        request = urllib.request.Request('https://api.github.com' + endpoint + '/zip', headers={'Authorization': 'Bearer ' + token})
        try:
            urllib.request.build_opener(NoRedirect()).open(request, timeout=30)
        except urllib.error.HTTPError as exc:
            if exc.code != 302: raise RuntimeError('Unable to obtain artifact download') from None
            url = exc.headers['Location']
            if urlparse(url).scheme != 'https': raise ValueError('Invalid artifact download URL')
            return url
        raise RuntimeError('Expected a signed artifact download URL')
    digest = hashlib.sha256()
    total = 0
    with tempfile.TemporaryFile() as archive:
        with urllib.request.urlopen(location(), timeout=60) as response:
            while block := response.read(1024 * 1024):
                total += len(block)
                if total > 1024 * 1024 * 1024: raise ValueError('Image artifact too large')
                digest.update(block)
                archive.write(block)
        archive.seek(0)
        with zipfile.ZipFile(archive) as package:
            if package.namelist() != ['dotasks-cloud.tar']:
                raise ValueError('Unexpected image artifact contents')
    # Refresh after hashing so the server gets the full signed-URL validity window.
    return location(), digest.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--instance-id', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('--deploy-root', default='/home/admin/dotasks')
    parser.add_argument('--working-user', default='admin')
    parser.add_argument('--github-artifact-id', type=int, help='Transfer a matching Actions image artifact instead of pulling GHCR')
    parser.add_argument('--execute', action='store_true', help='Execute only after independent release review')
    args = parser.parse_args()
    content = remote_script(args.image, args.deploy_root)
    if not args.execute:
        print(content)
        return
    if args.github_artifact_id:
        content = remote_script(args.image, args.deploy_root, github_artifact(args.github_artifact_id, args.image))
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
