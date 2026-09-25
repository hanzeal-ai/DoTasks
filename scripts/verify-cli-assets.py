"""Check the exact source and macOS archives that will be served by the image."""
import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import stat
import zipfile


def verify(directory, *, source_only=False, portable_only=False):
    names = [] if portable_only else ['latest.json']
    if not source_only:
        names += ['latest-macos-arm64.json', 'latest-macos-x86_64.json']
    for name in names:
        manifest = json.loads((directory / name).read_text())
        version = manifest['version']
        if not re.fullmatch(r'\d+\.\d+\.\d+-[0-9a-f]{12}', version):
            raise ValueError('Invalid version')
        platform = None if name == 'latest.json' else name[7:-5]
        filename = 'DoTasksCLI' + ('-' + platform if platform else '') + '.zip'
        expected = f'/downloads/cli/{version}/{filename}'
        if manifest['url'] != expected or manifest.get('platform') != platform:
            raise ValueError('Manifest URL or platform mismatch')
        archive = directory / version / filename
        if archive.stat().st_size != manifest['size'] or hashlib.sha256(archive.read_bytes()).hexdigest() != manifest['sha256']:
            raise ValueError('Archive checksum mismatch')
        if platform and hashlib.sha256((directory / filename).read_bytes()).hexdigest() != manifest['sha256']:
            raise ValueError('Direct download alias differs from versioned archive')
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if len(entries) > 10000 or sum(e.file_size for e in entries) > 600 * 1024 * 1024:
                raise ValueError('Archive bounds exceeded')
            seen = set()
            for entry in entries:
                path = PurePosixPath(entry.filename)
                if path.is_absolute() or '..' in path.parts or '\\' in entry.filename or not path.parts or path.parts[0] != 'DoTasksCLI' or entry.filename in seen or stat.S_ISLNK(entry.external_attr >> 16):
                    raise ValueError('Unsafe archive')
                seen.add(entry.filename)
            release = json.loads(package.read('DoTasksCLI/runtime/release.json'))
            if release['version'] != version or release.get('platform') != platform:
                raise ValueError('Internal release identity mismatch')
            prefix = 'DoTasksCLI/runtime/'
            actual = {e.filename[len(prefix):]: hashlib.sha256(package.read(e)).hexdigest()
                      for e in entries if e.filename.startswith(prefix) and not e.is_dir() and e.filename != prefix + 'release.json'}
            if actual != release['files']:
                raise ValueError('Internal file manifest mismatch')
            if platform:
                for path in ('dotasks', 'DoTasks.command', 'runtime/python/bin/python3'):
                    if not package.getinfo('DoTasksCLI/' + path).external_attr >> 16 & 0o111:
                        raise ValueError('Executable bit missing')
        print('Verified', name, version)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('directory', type=Path)
    parser.add_argument('--source-only', action='store_true')
    parser.add_argument('--portable-only', action='store_true')
    args = parser.parse_args()
    verify(args.directory, source_only=args.source_only, portable_only=args.portable_only)
