from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from taskboard.agent import load_agent_config
from taskboard.remote_service import RemoteToolClient
from taskboard.runtime_paths import default_config_path, default_data_home


class RuntimePathsTest(unittest.TestCase):
    def test_default_and_configured_paths_are_shared_by_agent_and_mcp(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            with patch.dict(os.environ, {}, clear=True), patch('pathlib.Path.home', return_value=home):
                self.assertEqual(home / 'Library/Application Support/DoTasks', default_data_home())
                self.assertEqual(default_data_home() / 'cloud-agent.json', default_config_path())
            with patch.dict(os.environ, {'DOTASKS_HOME': str(home / 'data'), 'DOTASKS_AGENT_CONFIG': str(home / 'agent.json')}, clear=True):
                path = default_config_path()
                path.write_text(json.dumps({'cloud_url': 'https://example.test', 'agent_id': 'fixture', 'agent_token': 'x' * 24}))
                agent = load_agent_config()
                remote = RemoteToolClient.from_environment()
                self.assertEqual(str((home / 'data').resolve()), agent.data_home)
                self.assertEqual(agent.cloud_url, remote.cloud_url)
                self.assertEqual(agent.agent_id, remote.agent_id)
                self.assertEqual(agent.agent_token, remote.agent_token)
