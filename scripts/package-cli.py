"""Build the platform-independent source runtime used by the macOS CLI."""
from pathlib import Path
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile

root = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(root))
from taskboard.version import VERSION

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
    files = {p.relative_to(runtime).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
             for p in sorted(runtime.rglob('*')) if p.is_file()}
    fingerprint = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()[:12]
    version = f'{VERSION}-{fingerprint}'
    (runtime / 'release.json').write_text(json.dumps({'version': version, 'files': files}, sort_keys=True))
    shutil.copy2(root / 'scripts/install-cli', package / 'install-cli')
    archive = Path(temporary) / 'DoTasksCLI.zip'
    with zipfile.ZipFile(archive, 'w', zipfile.ZIP_DEFLATED) as output:
        for path in sorted(package.rglob('*')):
            if path.is_file():
                output.write(path, path.relative_to(package.parent))
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    target = dist / 'cli' / version
    target.mkdir(parents=True, exist_ok=True)
    shutil.copy2(archive, target / archive.name)
    shutil.copy2(archive, dist / archive.name)
    manifest = {'version': version, 'url': f'/downloads/cli/{version}/DoTasksCLI.zip',
                'sha256': digest, 'size': archive.stat().st_size}
    (dist / 'cli/latest.json').write_text(json.dumps(manifest, indent=2) + '\n')
    print(f'Built {version}: {dist / archive.name}')
