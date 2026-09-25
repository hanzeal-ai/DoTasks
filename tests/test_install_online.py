"""Execute the no-Python bootstrap through a real pipe and controlling terminal."""
import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(shutil.which('unzip') and shutil.which('shasum'), 'system ZIP/checksum tools')
class OnlineInstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.archive = self.root / 'release.zip'
        self.version = '0.4.1-' + 'a' * 12
        self.environment = dict(os.environ, HOME=str(self.root), PATH=str(self.bin) + ':/usr/bin:/bin', FIXTURE=str(self.root))
        self.script('uname', 'import sys; print("Darwin" if sys.argv[1]=="-s" else "arm64")')
        self.script('plutil', 'import json,sys; print(json.load(open(sys.argv[-1]))[sys.argv[2]])')
        self.script('curl', '''import os,sys,pathlib,shutil
root=pathlib.Path(os.environ['FIXTURE']); args=sys.argv[1:]
url=next(a for a in args if a.startswith('https://'))
assert url.startswith('https://dotasks.hanzeal.com/downloads/cli/')
shutil.copyfile(root/('manifest.json' if url.endswith('.json') else 'release.zip'),args[args.index('-o')+1])
''')
        self.make_archive()

    def script(self, name, code):
        path = self.bin / name
        path.write_text('#!' + sys.executable + '\n' + code + '\n')
        path.chmod(0o755)

    def make_archive(self, *, unsafe=False, install_failure=False):
        with zipfile.ZipFile(self.archive, 'w') as package:
            package.writestr('DoTasksCLI/runtime/release.json', json.dumps({'version': self.version, 'platform': 'macos-arm64'}))
            script = '''#!/bin/sh
set -eu
mkdir -p "$HOME/.local/bin"
cat > "$HOME/.local/bin/dotasks" <<'INIT'
#!/bin/sh
[ "$1" = init ] || exit 2
echo INITIALIZE_INPUT
read name
[ "$name" = alice ] || exit 3
echo INITIALIZE_OK
INIT
chmod +x "$HOME/.local/bin/dotasks"
'''
            package.writestr('DoTasksCLI/install-cli', 'exit 17\n' if install_failure else script)
            if unsafe:
                package.writestr('DoTasksCLI/../../escaped', 'bad')
        data = self.archive.read_bytes()
        self.manifest = {'version': self.version, 'platform': 'macos-arm64', 'url': f'/downloads/cli/{self.version}/DoTasksCLI-macos-arm64.zip', 'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
        self.save_manifest()

    def save_manifest(self):
        (self.root / 'manifest.json').write_text(json.dumps(self.manifest))

    def execute(self):
        result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--pty-child'],
                                env=self.environment, capture_output=True, text=True, timeout=25, check=True)
        return json.loads(result.stdout)

    def test_pipe_installs_then_initializes_without_python_in_path(self):
        code, output = self.execute()
        self.assertEqual(0, code, output)
        self.assertIn('INITIALIZE_OK', output)
        for number in range(1, 6):
            self.assertIn(f'[{number}/5]', output)

    def test_failed_install_does_not_initialize(self):
        self.make_archive(install_failure=True)
        code, output = self.execute()
        self.assertEqual(17, code, output)
        self.assertNotIn('INITIALIZE_INPUT', output)

    def test_checksum_and_path_rejection_do_not_install(self):
        self.manifest['sha256'] = '0' * 64
        self.save_manifest()
        code, output = self.execute()
        self.assertNotEqual(0, code)
        self.assertIn('摘要不匹配', output)
        self.make_archive(unsafe=True)
        code, output = self.execute()
        self.assertNotEqual(0, code)
        self.assertIn('不安全的路径', output)
        self.assertFalse((self.root / '.local/bin/dotasks').exists())

    def test_cross_origin_manifest_does_not_download_package(self):
        self.manifest['url'] = 'https://other.test/code.zip'
        self.save_manifest()
        code, output = self.execute()
        self.assertNotEqual(0, code)
        self.assertIn('下载地址无效', output)

    def test_no_controlling_terminal_stops_before_download(self):
        result = subprocess.run(['/bin/sh', str(ROOT / 'scripts/install-online.sh')], env=self.environment, start_new_session=True, capture_output=True, text=True)
        self.assertNotEqual(0, result.returncode)
        self.assertIn('交互式终端', result.stderr)
        self.assertFalse((self.root / '.local').exists())


def run_pty():
    pid, descriptor = pty.fork()
    if pid == 0:
        # The bootstrap is actually supplied on stdin, as with curl | sh.
        os.execl('/bin/sh', 'sh', '-c', 'cat "$1" | sh', 'fixture', str(ROOT / 'scripts/install-online.sh'))
    output = b''
    deadline = time.monotonic() + 15
    sent = False
    status = None
    try:
        while time.monotonic() < deadline:
            if select.select([descriptor], [], [], 0.1)[0]:
                try:
                    chunk = os.read(descriptor, 65536)
                except OSError:
                    break
                if not chunk:
                    break
                output += chunk
                if b'INITIALIZE_INPUT' in output and not sent:
                    os.write(descriptor, b'alice\n')
                    sent = True
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
                break
        if status is None:
            done, code = os.waitpid(pid, os.WNOHANG)
            if done:
                status = code
            else:
                os.kill(pid, 15)
                _, status = os.waitpid(pid, 0)
    finally:
        os.close(descriptor)
    return os.waitstatus_to_exitcode(status), output.decode()


if __name__ == '__main__':
    if '--pty-child' in sys.argv:
        print(json.dumps(run_pty()))
    else:
        unittest.main()
