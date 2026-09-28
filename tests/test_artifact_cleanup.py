from pathlib import Path
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = [('deploy-cloud.yml', ['dotasks-release-current', 'portable-macos-arm64', 'portable-macos-x86_64'])]


class ArtifactCleanupTest(unittest.TestCase):
    def test_cleanup_only_deletes_consumed_artifacts_in_current_run(self):
        for workflow, names in WORKFLOWS:
            with self.subTest(workflow=workflow):
                source = (ROOT / '.github/workflows' / workflow).read_text()
                job = source.split('  cleanup-artifacts:', 1)[1]
                script = textwrap.dedent(job.split('        run: |\n', 1)[1])
                self.assertIn("result == 'success'", job)
                self.assertIn('actions: write', job)
                self.assertIn('continue-on-error: true', job)
                for scenario in ('normal', 'empty', 'list-error', 'delete-error', 'partial'):
                    with self.subTest(scenario=scenario), tempfile.TemporaryDirectory() as temporary:
                        directory = Path(temporary)
                        data = [{'id': index + 1, 'name': name} for index, name in enumerate(names)]
                        data += [{'id': 90, 'name': 'unrelated'}, {'id': 91, 'name': 'markfix-release-other'},
                                 {'id': 92, 'name': 'dotasks-release-other'}]
                        if scenario == 'empty':
                            data = []
                        fixture = directory / 'artifacts.json'
                        fixture.write_text(json.dumps({'artifacts': data}))
                        calls = directory / 'calls'
                        gh = directory / 'gh'
                        gh.write_text('#!' + sys.executable + '\n' + textwrap.dedent("""\
                            import json, os, pathlib, subprocess, sys
                            args = sys.argv[1:]
                            with open(os.environ['CALLS'], 'a') as stream:
                                stream.write(json.dumps(args) + '\\n')
                            if '--method' in args:
                                assert args[:3] == ['api', '--method', 'DELETE']
                                assert args[3].startswith('repos/example/project/actions/artifacts/')
                                sys.exit(1 if os.environ['SCENARIO'] == 'delete-error' else 0)
                            assert args[:3] == ['api', '--paginate', 'repos/example/project/actions/runs/123/artifacts?per_page=100']
                            if os.environ['SCENARIO'] == 'list-error':
                                sys.exit(1)
                            query = args[args.index('--jq') + 1]
                            sys.exit(subprocess.run(['jq', '-r', query, os.environ['FIXTURE']]).returncode)
                        """))
                        gh.chmod(0o755)
                        env = dict(os.environ, PATH=str(directory) + os.pathsep + os.environ['PATH'],
                                   GITHUB_REPOSITORY='example/project', GITHUB_RUN_ID='123', GITHUB_SHA='current',
                                   WINDOWS_RESULT='success', MACOS_RESULT='failure' if scenario == 'partial' else 'success',
                                   FIXTURE=str(fixture), CALLS=str(calls), SCENARIO=scenario)
                        result = subprocess.run(['bash', '-c', script], env=env, capture_output=True, text=True)
                        self.assertTrue(calls.exists(), result.stderr)
                        recorded = [json.loads(line) for line in calls.read_text().splitlines()]
                        deleted = [int(call[-1].rsplit('/', 1)[-1]) for call in recorded if '--method' in call]
                        expected = [index + 1 for index, name in enumerate(names)
                                    if not (scenario == 'partial' and name == 'desktop-macos')]
                        if scenario in ('empty', 'list-error'):
                            expected = []
                        elif scenario == 'delete-error':
                            expected = expected[:1]
                        self.assertEqual(deleted, expected, result.stderr)
                        self.assertEqual(result.returncode == 0, scenario not in ('list-error', 'delete-error'), result.stderr)


if __name__ == '__main__':
    unittest.main()
