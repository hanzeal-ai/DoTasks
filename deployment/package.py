"""Package CI-built Docker archives for the forced SSH receiver."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import zipfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('commit')
    parser.add_argument('output', type=Path)
    parser.add_argument('images', nargs='+', help='component=archive.tar')
    args = parser.parse_args()
    if not re.fullmatch('[a-f0-9]{40}', args.commit):
        raise ValueError('Expected exact commit SHA')
    images = {}
    for item in args.images:
        component, path = item.split('=', 1)
        if not re.fullmatch('[a-z]+', component) or component in images:
            raise ValueError('Invalid or duplicate component')
        images[component] = Path(path)
    hashes = {}
    with zipfile.ZipFile(args.output, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
        for component, path in images.items():
            checksum = hashlib.sha256()
            with path.open('rb') as stream:
                for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                    checksum.update(chunk)
            hashes[component] = checksum.hexdigest()
            archive.write(path, 'image-' + component + '.tar')
        archive.writestr('release.json', json.dumps({'commit': args.commit, 'images': hashes}))


if __name__ == '__main__':
    main()
