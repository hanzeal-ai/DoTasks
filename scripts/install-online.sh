#!/bin/sh
set -eu
printf '%s\n' '[1/5] 检查 Python 3.14+……'
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
exec "$PYTHON_BIN" -u - "$@" <<'PY'
import hashlib, json, os, pathlib, stat, subprocess, sys, tempfile, urllib.request, urllib.parse, zipfile
# The script itself occupies stdin (also for curl | sh). Prompts need the terminal.
try:
    terminal = open('/dev/tty', 'r')
except OSError:
    raise SystemExit('请在交互式终端运行安装命令；初始化需要输入账号密码并完成授权。')
origin = 'https://dotasks.hanzeal.com'
print('[2/5] 获取生产版本并下载安装包……', flush=True)
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
    print('[3/5] 校验安装包并解压……', flush=True)
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
    print('[4/5] 安装 DoTasks CLI……', flush=True)
    subprocess.run(['/bin/sh', str(runtime.parent / 'install-cli'), *sys.argv[1:]], env=environment, check=True)
launcher = pathlib.Path.home() / '.local/bin/dotasks'
print('[5/5] 初始化账号、授权并启动服务……', flush=True)
try:
    subprocess.run([str(launcher), 'init'], stdin=terminal, check=True)
except subprocess.CalledProcessError as exc:
    print(f'CLI 已安装，初始化尚未完成。请运行 {launcher} init 继续。', flush=True)
    raise SystemExit(exc.returncode)
finally:
    terminal.close()
PY
