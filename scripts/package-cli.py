"""Build the platform-independent source runtime used by the macOS CLI."""
from pathlib import Path
import argparse
import platform
import subprocess
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from taskboard.version import VERSION

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--python-runtime', type=Path, help='Relocatable python-build-standalone install directory')
args = parser.parse_args()
portable = args.python_runtime is not None
if portable:
    if sys.platform != 'darwin':
        raise SystemExit('Build portable macOS packages on macOS.')
    from taskboard.cli_distribution import platform_key
    target_platform = platform_key()
    interpreter = args.python_runtime / 'bin/python3'
    subprocess.run([str(interpreter), '-c', 'import sys; assert sys.version_info >= (3,14)'], check=True)

dist = root / 'dist'
dist.mkdir(exist_ok=True)
with tempfile.TemporaryDirectory(prefix='.cli-package-', dir=dist) as temporary:
    package = Path(temporary) / 'DoTasksCLI'
    runtime = package / 'runtime'
    runtime.mkdir(parents=True)
    for name in ('core', 'taskboard', 'skills', 'static'):
        ignored = ('__pycache__', '*.pyc', '.DS_Store', 'cloud') if name == 'taskboard' else ('__pycache__', '*.pyc', '.DS_Store')
        shutil.copytree(root / name, runtime / name, ignore=shutil.ignore_patterns(*ignored))
    (runtime / 'scripts').mkdir()
    shutil.copy2(root / 'scripts/mcp-server', runtime / 'scripts/mcp-server')
    if portable:
        shutil.copytree(args.python_runtime, runtime / 'python', symlinks=False, ignore=shutil.ignore_patterns('__pycache__', '*.pyc', '.DS_Store'))
    files = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(runtime.rglob('*')) if p.is_file()}
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    version = f'{VERSION}-{fingerprint}'
    release = {'version': version, 'files': files}
    if portable:
        release['platform'] = target_platform
        release['python'] = 'python/bin/python3'
    (runtime / 'release.json').write_text(json.dumps(release, sort_keys=True))
    shutil.copy2(root / 'scripts/install-cli', package / 'install-cli')
    if portable:
        shutil.copy2(root / 'scripts/portable-dotasks', package / 'dotasks')
        shutil.copy2(root / 'scripts/portable-dotasks', package / 'DoTasks.command')
    archive = Path(temporary) / ('DoTasksCLI-' + target_platform + '.zip' if portable else 'DoTasksCLI.zip')
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(package.parent))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    target = dist / 'cli' / version
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, target / archive.name)
    shutil.copy2(archive, dist / archive.name)
    manifest = {'version': version, 'url': f'/downloads/cli/{version}/{archive.name}',
                'sha256': digest, 'size': archive.stat().st_size}
    if portable:
        manifest['platform'] = target_platform
    (dist / ('cli/latest-' + target_platform + '.json' if portable else 'cli/latest.json')).write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built {version}: {dist / archive.name}')
