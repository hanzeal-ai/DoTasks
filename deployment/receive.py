#!/usr/bin/python3.11
"""Root-owned forced SSH receiver. Accepts images for this application only."""
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import zipfile

APP = 'dotasks'
BASE = Path('/home/admin/dotasks')
HELPERS = Path('/usr/local/libexec/dotasks-deploy')
COMPONENTS = ('cloud',)
MAX_UPLOAD = 2 * 1024**3
MAX_EXPANDED = 4 * 1024**3


def command(value):
    match = re.fullmatch(r'(site|windows|macos) ([a-f0-9]{40})', value)
    if not match or (APP == 'dotasks' and match[1] != 'site'):
        raise ValueError('Expected permitted release kind and exact commit SHA')
    return match[1], match[2]


def unpack(source, destination):
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or len(names) > 20 or sum(e.file_size for e in entries) > MAX_EXPANDED:
            raise ValueError('Invalid archive size or duplicate members')
        for entry in entries:
            name = entry.filename
            mode = stat.S_IFMT(entry.external_attr >> 16)
            if not name or '/' in name or '\\' in name or name in ('.', '..') or mode not in (0, stat.S_IFREG):
                raise ValueError('Only flat regular files are accepted')
        for entry in entries:
            with archive.open(entry) as src, (destination / entry.filename).open('xb') as dst:
                shutil.copyfileobj(src, dst)
        return set(names)


def digest(path):
    checksum = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            checksum.update(chunk)
    return checksum.hexdigest()


def image_identity(path, tag, normalize=True):
    # Do not let Docker load replace tags belonging to another application.
    with tarfile.open(path, 'r:') as archive:
        members = []
        for member in archive:
            members.append(member)
            if len(members) > 4096:
                raise ValueError('Too many Docker archive members')
        names = [m.name for m in members]
        if len(names) != len(set(names)) or sum(m.size for m in members) > MAX_EXPANDED:
            raise ValueError('Invalid Docker archive')
        for member in members:
            p = PurePosixPath(member.name)
            if p.is_absolute() or '..' in p.parts or '\\' in member.name or not (member.isfile() or member.isdir()):
                raise ValueError('Unsafe Docker archive member')
        member = archive.getmember('manifest.json')
        if member.size > 65536:
            raise ValueError('Oversized Docker manifest')
        manifests = json.load(archive.extractfile(member))
        if len(manifests) != 1 or manifests[0].get('RepoTags') != [tag]:
            raise ValueError('Unexpected Docker image tags')
        config = archive.getmember(manifests[0]['Config'])
        if config.size > 1024 * 1024:
            raise ValueError('Oversized image config')
        image_id = 'sha256:' + hashlib.sha256(archive.extractfile(config).read()).hexdigest()
        # OCI indexes and legacy repositories files may contain additional tag mappings.
        # Normalize the archive to this one checked Docker manifest and its referenced blobs.
        selected = {'manifest.json', manifests[0]['Config'], *manifests[0]['Layers']}
        if not normalize:
            return image_id
        normalized = path.with_suffix('.checked.tar')
        with tarfile.open(normalized, 'w') as target:
            for name in sorted(selected):
                item = archive.getmember(name)
                if not item.isfile():
                    raise ValueError('Image blobs must be regular files')
                target.addfile(item, archive.extractfile(item))
        return image_id


def install_images(directory, names, commit):
    if names != {'release.json', *('image-' + c + '.tar' for c in COMPONENTS)}:
        raise ValueError('Unexpected release files')
    manifest_path = directory / 'release.json'
    if manifest_path.stat().st_size > 16384:
        raise ValueError('Oversized release manifest')
    manifest = json.loads(manifest_path.read_text())
    if manifest.get('commit') != commit or set(manifest.get('images', {})) != set(COMPONENTS):
        raise ValueError('Release manifest mismatch')
    images = {}
    # Validate every image before changing Docker state.
    for component in COMPONENTS:
        path = directory / ('image-' + component + '.tar')
        if digest(path) != manifest['images'][component]:
            raise ValueError('Image checksum mismatch')
        tag = APP + '-' + component + ':' + commit
        images[component] = image_identity(path, tag)
    for component in COMPONENTS:
        subprocess.run(['docker', 'load', '-i', str(directory / ('image-' + component + '.checked.tar'))], check=True, timeout=600, stdout=subprocess.DEVNULL)
        actual = subprocess.check_output(['docker', 'image', 'inspect', '--format', '{{.Id}}', APP + '-' + component + ':' + commit], text=True).strip()
        if not re.fullmatch(r'sha256:[a-f0-9]{64}', actual):
            raise ValueError('Invalid loaded image ID')
        # Docker classic reports the config digest as Id, while the containerd image
        # store reports a manifest digest. Verify the loaded config via Docker's export.
        exported = directory / 'loaded.tar'
        try:
            subprocess.run(['docker', 'save', '-o', str(exported), APP + '-' + component + ':' + commit], check=True, timeout=600)
            if image_identity(exported, APP + '-' + component + ':' + commit, normalize=False) != images[component]:
                raise ValueError('Loaded image identity mismatch')
        finally:
            exported.unlink(missing_ok=True)
        images[component] = actual
    return images


def main():
    if len(sys.argv) != 2:
        raise ValueError('Expected one validated SSH command')
    kind, commit = command(sys.argv[1])
    os.umask(0o077)
    # Hold a receiver lock during transfer, separately from each application's activation lock.
    with (BASE / 'receiver.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with tempfile.TemporaryDirectory(prefix='.upload-', dir=BASE) as temporary:
            root = Path(temporary)
            bundle = root / 'bundle.zip'
            total = 0
            with bundle.open('xb') as output:
                while True:
                    chunk = sys.stdin.buffer.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_UPLOAD:
                        raise ValueError('Upload too large')
                    output.write(chunk)
            files = root / 'files'
            files.mkdir()
            names = unpack(bundle, files)
            environment = {'PATH': '/usr/bin:/bin', 'HOME': '/root', 'GITHUB_SHA': commit}
            if kind == 'site':
                images = install_images(files, names, commit)
                subprocess.run(['/usr/bin/python3.11', str(HELPERS / 'activate.py'), images['cloud'], commit], env=environment, check=True, timeout=1200)
            print('DEPLOYED ' + kind + ' ' + commit)


if __name__ == '__main__':
    try:
        main()
    except Exception as exc:
        print('Deployment rejected or failed: ' + type(exc).__name__ + '; inspect server state before retrying', file=sys.stderr)
        sys.exit(1)
