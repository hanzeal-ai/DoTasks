#!/bin/sh
set -eu
PYTHON_BIN=${DOTASKS_PYTHON_BIN:-}
if [ -z "$PYTHON_BIN" ] && [ -x /opt/homebrew/bin/python3 ]; then
  PYTHON_BIN=/opt/homebrew/bin/python3
fi
if [ -z "$PYTHON_BIN" ]; then
  PYTHON_BIN=$(command -v python3 || true)
fi
if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,14) else 1)'; then
  echo "Install Python 3.14+ before installing DoTasks CLI." >&2
  exit 1
fi
exec "$PYTHON_BIN" - "$@" <<'PY'
import hashlib, json, os, pathlib, stat, subprocess, sys, tempfile, urllib.request, urllib.parse, zipfile
origin = 'https://dotasks.hanzeal.com'
class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None
opener = urllib.request.build_opener(NoRedirect())
with opener.open(origin + '/downloads/cli/latest.json', timeout=30) as response:
    manifest = json.load(response)
url = urllib.parse.urljoin(origin, manifest['url'])
if urllib.parse.urlparse(url).scheme != 'https' or urllib.parse.urlparse(url).netloc != urllib.parse.urlparse(origin).netloc:
    raise SystemExit('Invalid release origin')
if type(manifest['size']) is not int or not 0 < manifest['size'] <= 50 * 1024 * 1024:
    raise SystemExit('Invalid archive size')
with tempfile.TemporaryDirectory(prefix='DoTasksCLI-') as temporary:
    archive = pathlib.Path(temporary) / 'release.zip'
    digest = hashlib.sha256()
    total = 0
    with opener.open(url, timeout=30) as response, archive.open('wb') as output:
        while block := response.read(1024 * 1024):
            total += len(block)
            if total > manifest['size']: raise SystemExit('Release size exceeded')
            digest.update(block)
            output.write(block)
    if total != manifest['size'] or digest.hexdigest() != manifest['sha256']:
        raise SystemExit('Release checksum failed')
    with zipfile.ZipFile(archive) as package:
        entries = package.infolist()
        if len(entries) > 10000 or sum(x.file_size for x in entries) > 150 * 1024 * 1024:
            raise SystemExit('Expanded release too large')
        seen = set()
        for entry in entries:
            path = pathlib.PurePosixPath(entry.filename)
            if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or not path.parts or path.parts[0] != 'DoTasksCLI' or stat.S_ISLNK(entry.external_attr >> 16) or entry.filename in seen:
                raise SystemExit('Unsafe release archive')
            seen.add(entry.filename)
        package.extractall(temporary)
    runtime = pathlib.Path(temporary) / 'DoTasksCLI/runtime'
    if json.loads((runtime / 'release.json').read_text())['version'] != manifest['version']:
        raise SystemExit('Release version mismatch')
    environment = dict(os.environ, DOTASKS_PYTHON_BIN=sys.executable)
    subprocess.run(['/bin/sh', str(runtime.parent / 'install-cli'), *sys.argv[1:]], env=environment, check=True)
PY
