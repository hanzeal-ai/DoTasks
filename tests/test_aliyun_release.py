import ast
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import unittest
from unittest.mock import patch
import zipfile

spec = importlib.util.spec_from_file_location('aliyun_release', Path(__file__).resolve().parents[1] / 'scripts/deploy-aliyun-cli.py')
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


class AlibabaReleaseTest(unittest.TestCase):
    def test_artifact_transfer_plan_is_valid_shell_and_never_pulls_registry(self):
        image = 'ghcr.io/hanzeal-ai/dotasks:' + 'a' * 40
        for artifact in (None, ('https://example.test/image.zip?signature=temporary', 'b' * 64)):
            script = release.remote_script(image, '/home/admin/dotasks', artifact)
            subprocess.run(['bash', '-n'], input=script, text=True, check=True)
            if artifact:
                transfer = script.split("<<'IMAGE_TRANSFER'\n", 1)[1].split('\nIMAGE_TRANSFER', 1)[0]
                ast.parse(transfer, feature_version=(3, 6))
                self.assertNotIn('docker pull', script)
                self.assertIn('--loaded-image', script)
                self.assertIn('Image artifact checksum mismatch', script)

    def test_wrong_revision_is_rejected_before_requesting_token(self):
        result = subprocess.CompletedProcess([], 0, json.dumps({'name': 'wrong', 'expired': False}))
        with patch.object(release.subprocess, 'run', return_value=result) as run:
            with self.assertRaises(ValueError):
                release.github_artifact(1, 'ghcr.io/hanzeal-ai/dotasks:' + 'a' * 40)
            self.assertEqual(1, run.call_count)

    def test_signed_artifact_is_hashed_and_github_token_is_not_returned(self):
        revision = 'a' * 40
        metadata = {'name': 'dotasks-image-' + revision, 'expired': False, 'workflow_run': {'head_sha': revision}}
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, 'w') as package:
            package.writestr('dotasks-cloud.tar', b'image')
        raw = archive.getvalue()
        responses = [subprocess.CompletedProcess([], 0, json.dumps(metadata)), subprocess.CompletedProcess([], 0, 'private-token')]
        redirect = release.urllib.error.HTTPError('https://api.github.com', 302, 'Found', {'Location': 'https://example.test/signed-image'}, None)
        with patch.object(release.subprocess, 'run', side_effect=responses), patch.object(release.urllib.request, 'build_opener') as opener, patch.object(release.urllib.request, 'urlopen', return_value=io.BytesIO(raw)):
            opener.return_value.open.side_effect = redirect
            url, digest = release.github_artifact(1, 'ghcr.io/hanzeal-ai/dotasks:' + revision)
        self.assertEqual('https://example.test/signed-image', url)
        self.assertEqual(release.hashlib.sha256(raw).hexdigest(), digest)
        self.assertNotIn('private-token', release.remote_script('ghcr.io/hanzeal-ai/dotasks:' + revision, '/home/admin/dotasks', (url, digest)))
