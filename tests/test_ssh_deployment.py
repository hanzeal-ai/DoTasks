import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import shutil
import tarfile
import tempfile
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[1]
DIRECTORY = ROOT / 'deployment'
spec = importlib.util.spec_from_file_location('ssh_receiver', DIRECTORY / 'receive.py')
receiver = importlib.util.module_from_spec(spec)
spec.loader.exec_module(receiver)
SHA = 'a' * 40


def image_archive(path, tag):
    config = b'{"architecture":"amd64","os":"linux"}'
    content = {'manifest.json': json.dumps([{'Config': 'config.json', 'RepoTags': [tag], 'Layers': []}]).encode(),
               'config.json': config}
    with tarfile.open(path, 'w') as archive:
        for name, data in content.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    return 'sha256:' + hashlib.sha256(config).hexdigest()


class ReceiverTests(unittest.TestCase):
    def test_only_expected_commands_are_accepted(self):
        self.assertEqual(receiver.command('site ' + SHA), ('site', SHA))
        for value in ('site main', 'site ' + SHA + '; id', 'scp -t /tmp', '../site ' + SHA, 'site ' + SHA + '\n'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                receiver.command(value)

    def test_zip_rejects_paths_links_duplicates_and_oversize_before_writing(self):
        for name, mode in (('../outside', 0), ('/etc/passwd', 0), ('a/b', 0), ('a\\b', 0), ('link', 0o120777)):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                data = io.BytesIO()
                with zipfile.ZipFile(data, 'w') as archive:
                    member = zipfile.ZipInfo(name)
                    member.external_attr = mode << 16
                    archive.writestr(member, b'unsafe')
                data.seek(0)
                with self.assertRaises(ValueError):
                    receiver.unpack(data, Path(temporary))
                self.assertEqual(list(Path(temporary).iterdir()), [])
        with tempfile.TemporaryDirectory() as temporary:
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr('valid', b'12345')
            data.seek(0)
            with patch.object(receiver, 'MAX_EXPANDED', 4), self.assertRaises(ValueError):
                receiver.unpack(data, Path(temporary))

    def test_image_cannot_replace_another_app_tag(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / 'image.tar'
            image_archive(path, 'other-service:' + SHA)
            with self.assertRaises(ValueError):
                receiver.image_identity(path, receiver.APP + '-cloud:' + SHA)

    def test_package_verified_and_loaded_by_content_identity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            arguments = []
            expected = {}
            for component in receiver.COMPONENTS:
                path = root / (component + '.tar')
                expected[component] = image_archive(path, receiver.APP + '-' + component + ':' + SHA)
                arguments.append(component + '=' + str(path))
            output = root / 'release.zip'
            subprocess.run(['python3', str(DIRECTORY / 'package.py'), SHA, str(output), *arguments], check=True)
            files = root / 'files'
            files.mkdir()
            names = receiver.unpack(output, files)
            # Give each component its own expected tag on re-export.
            def docker_call(args, **kwargs):
                if args[1] == 'save':
                    component = args[-1].split(':')[0].removeprefix(receiver.APP + '-')
                    shutil.copyfile(files / ('image-' + component + '.tar'), args[3])
            with patch.object(receiver.subprocess, 'run', side_effect=docker_call) as run, patch.object(receiver.subprocess, 'check_output', return_value=next(iter(expected.values())) + '\n'):
                self.assertEqual(receiver.install_images(files, names, SHA), expected)
                self.assertEqual(run.call_count, len(receiver.COMPONENTS) * 2)
            # Corruption must be detected before calling Docker at all.
            (files / ('image-' + receiver.COMPONENTS[0] + '.tar')).write_bytes(b'corrupt')
            with patch.object(receiver.subprocess, 'run') as run, self.assertRaises(ValueError):
                receiver.install_images(files, names, SHA)
            run.assert_not_called()

    def test_loaded_identity_mismatch_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            hashes = {}
            for component in receiver.COMPONENTS:
                path = root / ('image-' + component + '.tar')
                image_archive(path, receiver.APP + '-' + component + ':' + SHA)
                hashes[component] = receiver.digest(path)
            (root / 'release.json').write_text(json.dumps({'commit': SHA, 'images': hashes}))
            def docker_call(args, **kwargs):
                if args[1] == 'save':
                    image_archive(Path(args[3]), 'wrong-tag:' + SHA)
            with patch.object(receiver.subprocess, 'run', side_effect=docker_call), patch.object(receiver.subprocess, 'check_output', return_value='sha256:' + 'b' * 64), self.assertRaises(ValueError):
                receiver.install_images(root, {p.name for p in root.iterdir()}, SHA)

    def test_sender_pins_host_and_uses_only_forced_receiver(self):
        import os
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            ssh = root / 'ssh'
            log = root / 'args'
            ssh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$TEST_LOG"\ncat >/dev/null\n')
            ssh.chmod(0o755)
            bundle = root / 'bundle.zip'
            bundle.write_bytes(b'test')
            env = dict(os.environ, PATH=str(root) + ':' + os.environ['PATH'], TEST_LOG=str(log),
                       DEPLOY_HOST='server.example.com', DEPLOY_KEY='test-key', DEPLOY_KNOWN_HOSTS='verified-host-key')
            subprocess.run(['bash', str(DIRECTORY / 'send.sh'), 'site', SHA, str(bundle)], env=env, check=True)
            args = log.read_text()
            self.assertIn('StrictHostKeyChecking=yes', args)
            self.assertIn(receiver.APP + '-deploy@server.example.com', args)
            self.assertIn('site ' + SHA, args)
            result = subprocess.run(['bash', str(DIRECTORY / 'send.sh'), 'site', 'main; id', str(bundle)], env=env, capture_output=True)
            self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
