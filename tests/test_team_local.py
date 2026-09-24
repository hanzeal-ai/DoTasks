import os
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from taskboard.app_server import prepare_worker_codex_home, AppServerError
from taskboard.team_local import ReadOnlyAnalysisClient, attachment_inputs
from core.db import Database


class TeamLocalTest(unittest.TestCase):
    def test_analysis_configuration_excludes_callback_credentials_and_write_permissions(self):
        import tomllib
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);shared=root/'shared';shared.mkdir()
            (shared/'config.toml').write_text('model="test-model"\n[mcp_servers.private]\ncommand="secret-command"\n')
            result=prepare_worker_codex_home(root/'worker',root,shared_codex_home=shared,readonly=True,readonly_executable='/usr/bin/true')
            config=tomllib.loads((result/'config.toml').read_text())
            self.assertEqual('never',config['approval_policy'])
            self.assertNotIn('mcp_servers',config)
            self.assertEqual('none',config['shell_environment_policy']['inherit'])
            profile=config['permissions']['dotasks-analysis']
            self.assertFalse(profile['network']['enabled'])
            self.assertEqual('deny',profile['filesystem'][':root'])
            self.assertEqual({'.':'read'},profile['filesystem'][':workspace_roots'])

    def test_atomic_unit_rolls_back_all_nested_core_writes_and_restores_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            db=Database(Path(directory)/'database.sqlite')
            with self.assertRaises(RuntimeError):
                with db.atomic():
                    with db.transaction() as conn:conn.execute("INSERT INTO requirements(id,title,original_content) VALUES('REQ-1','test','test')")
                    with db.connection() as conn:self.assertEqual(1,conn.execute('SELECT COUNT(*) FROM requirements').fetchone()[0])
                    raise RuntimeError('simulated interruption before request receipt')
            with db.connection() as conn:self.assertEqual(0,conn.execute('SELECT COUNT(*) FROM requirements').fetchone()[0])
            with db.transaction() as conn:conn.execute("INSERT INTO requirements(id,title,original_content) VALUES('REQ-2','committed','test')")
            with db.connection() as conn:self.assertEqual(1,conn.execute('SELECT COUNT(*) FROM requirements').fetchone()[0])

    @unittest.skipUnless(os.environ.get('DOTASKS_TEST_CODEX_SANDBOX')=='1' and shutil.which('codex'),'explicit local Codex sandbox probe')
    def test_actual_codex_denies_project_writes_and_outside_reads_without_model_turn(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);project=root/'project';project.mkdir()
            client=ReadOnlyAnalysisClient(root/'worker',Path.cwd())
            try:client.start();client.probe(str(project))
            finally:client.stop()
            self.assertEqual([],list(project.iterdir()))
