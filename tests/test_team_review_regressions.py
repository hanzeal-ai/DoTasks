"""Regressions for stop handling, final acceptance and completed membership."""
import subprocess
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from taskboard.cloud.team_service import modules
from taskboard.team_agent import TeamAgent
from tests.test_team_collaboration import module
from tests import test_team_lifecycle as lifecycle


class TeamReviewRegressionTest(unittest.TestCase):
    def test_dense_dependencies_finish_and_cycles_still_rejected(self):
        subprocess.run([sys.executable, '-c', '''
from taskboard.cloud.team_service import modules
from tests.test_team_collaboration import module
specs=[module('m'+str(i), ['m'+str(j) for j in range(i)]) for i in range(30)]
assert len(modules(specs)) == 30
'''], check=True, timeout=5)
        for specs in ([module('a',['b']),module('b',['a'])], [module('a',['missing'])]):
            with self.assertRaises(ValueError):
                modules(specs)

    def test_stop_invalidates_integration_and_blocks_acceptance(self):
        case=lifecycle.TeamLifecycleTest(); case.setUp()
        try:
            accept=case.a.accept_requirement
            def stop_before_accept(payload):
                task=case.a.snapshot()['tasks'][0]
                case.a.request_stop({'task_id':task['id'],'revision':task['revision']})
                with self.assertRaises(ValueError):
                    accept(payload)
                with case.a.db.connection() as db:
                    req=case.a.requirement(payload['requirement_id'],db)
                    self.assertEqual('{}',req['integration'])
                    with self.assertRaises(ValueError):
                        case.a.integration_members(req,db)
                raise StopVerified()
            with patch.object(case.a,'accept_requirement',side_effect=stop_before_accept):
                with self.assertRaises(StopVerified):
                    case.test_complete_delivery_uses_real_git_and_verification()
        finally:
            case.tearDown()

    def test_completed_coordinator_can_be_revoked_but_active_cannot(self):
        case=lifecycle.TeamLifecycleTest(); case.setUp()
        try:
            case.test_complete_delivery_uses_real_git_and_verification()
            case.a.revoke_project({'project_id':case.pid,'account_id':case.ids['bob']})
            self.assertEqual([],case.b.snapshot()['projects'])
        finally:
            case.tearDown()
        case=lifecycle.TeamLifecycleTest(); case.setUp()
        try:
            case.upload()
            with self.assertRaises(ValueError):
                case.a.revoke_project({'project_id':case.pid,'account_id':case.ids['bob']})
        finally:
            case.tearDown()

    def test_analysis_does_not_block_stop_or_duplicate_claims(self):
        with tempfile.TemporaryDirectory() as home:
            agent=TeamAgent(SimpleNamespace(data_home=home,cloud_url='https://example.invalid',agent_id='a',agent_token='b'*32))
            team='a'*32
            agent.client=Mock(); agent.client.request.return_value={'teams':[{'id':team}]}
            executor=Mock(); agent.executors={(team,'TASK-1',1):executor}
            client=Mock(); client.action.return_value={'id':'analysis'}
            client.request.return_value={'tasks':[],'actor_id':'a'}
            started,release=threading.Event(),threading.Event()
            agent.analyze=lambda *_:(started.set(),release.wait(5))
            try:
                with patch('taskboard.team_agent.TeamClient',return_value=client):
                    agent.drain()
                    self.assertTrue(started.wait(1))
                    executor.stop.assert_called_once()
                    self.assertTrue(agent.analysis_thread.is_alive())
                    agent.drain()
                    client.action.assert_called_once_with('analysis-claim',{})
            finally:
                release.set()
                if agent.analysis_thread:
                    agent.analysis_thread.join(2)
                agent.stop()


class StopVerified(Exception):
    pass
