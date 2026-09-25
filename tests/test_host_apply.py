import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('host_apply', Path(__file__).resolve().parents[1] / 'deployment/host/apply.py')
apply = importlib.util.module_from_spec(spec)
spec.loader.exec_module(apply)
DT = 'services:\n  dotasks:\n    image: same\n    restart: unless-stopped\n'
MF = "services:\n  postgres:\n    image: pg\n    restart: unless-stopped\n  api:\n    image: api\n    restart: unless-stopped\n  dashboard:\n    image: web\n    restart: unless-stopped\n    ports:\n      - '8766:80'\n"


class HostApplyTest(unittest.TestCase):
    def test_configuration_preserves_images_and_binds_loopback(self):
        result = apply.configure(MF, ['postgres', 'api', 'dashboard'])
        for image in ['image: pg', 'image: api', 'image: web']:
            self.assertIn(image, result)
        self.assertIn("'127.0.0.1:8766:80'", result)
        self.assertEqual(result.count('mem_limit:'), 3)
        self.assertIn('mem_limit: 384m', result)
        with self.assertRaises(ValueError):
            apply.configure(result, ['postgres'])

    def exercise_failure(self, recovery_failure):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dt, mf, current, env = root / 'dt', root / 'mf', root / 'current', root / 'env'
            current.mkdir()
            files = {dt: DT, mf: MF, current / 'compose.yaml': MF, env: 'DOTASKS_BIND_ADDRESS=0.0.0.0\nCUSTOM=keep\n'}
            for path, text in files.items():
                path.write_text(text)
            calls = []
            def run(*args, **kwargs):
                if 'up' in args:
                    calls.append(args)
                    if len(calls) == 1 or (recovery_failure and len(calls) == 2):
                        raise RuntimeError('simulated failure')
            # Redirect only deployment locks; leave tempfile files and config copies real.
            real_open = open
            def locks(path, *args, **kwargs):
                if str(path).endswith('/deploy.lock'):
                    path = root / ('dt.lock' if 'dotasks' in str(path) else 'mf.lock')
                return real_open(path, *args, **kwargs)
            with patch.multiple(apply, DT=dt, MF=mf, CURRENT=current, ENV=env, ROOT=root), patch.object(apply, 'commands', return_value=(['dt'], ['mf'])), patch.object(apply, 'image_ids', return_value={'same': 'image'}), patch.object(apply, 'verify'), patch.object(apply, 'health'), patch.object(apply, 'run', side_effect=run), patch('builtins.open', side_effect=locks):
                with self.assertRaisesRegex(RuntimeError, 'RECOVERY INCOMPLETE' if recovery_failure else 'HTTPS restored'):
                    apply.main()
            self.assertEqual(len(calls), 4)
            self.assertEqual(calls[-1][-1], 'dotasks')
            for path, text in files.items():
                self.assertEqual(path.read_text(), text)
            for call in calls:
                self.assertIn('--no-build', call)
                self.assertIn('never', call)

    def test_start_failure_restores_all_configuration_and_services(self):
        self.exercise_failure(False)

    def test_recovery_continues_after_one_service_fails(self):
        self.exercise_failure(True)
