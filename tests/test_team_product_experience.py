import base64
import io
import http.client
import os
import subprocess
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch
from tests.test_team_http import TeamHTTPTest
from taskboard.cloud.team_attachments import decode_attachments, ATTACHMENT_POLICY
from taskboard.cloud.team_events import TeamEvents, serve_team_events
from taskboard.http_security import HTTPRequestError


def attachment(content, mime='image/png'):
    return {'name':'sample','mime':mime,'content_base64':base64.b64encode(content).decode()}


class AttachmentPolicyTests(unittest.TestCase):
    def test_large_screenshot_and_total_limit(self):
        image=b'\x89PNG\r\n\x1a\n'+b'x'*(2*1024*1024-8)
        self.assertEqual(len(decode_attachments([attachment(image)])[0]['content']),len(image))
        self.assertEqual(len(decode_attachments([attachment(image),attachment(image)])),2)
        with self.assertRaisesRegex(ValueError,'合计'):decode_attachments([attachment(image),attachment(image),attachment(b'\x89PNG\r\n\x1a\n')])
        with self.assertRaises(ValueError):decode_attachments([attachment(image+b'x')])
        with self.assertRaisesRegex(ValueError,'256'):decode_attachments([attachment(b'x'*(256*1024+1),'text/plain')])
        with self.assertRaises(ValueError):decode_attachments([attachment(b'<script>','image/png')])
        with self.assertRaises(ValueError):decode_attachments([attachment(b'\xff','text/plain')])


class ProductHTTPTests(TeamHTTPTest):
    def test_large_upload_crosses_old_http_limit_and_replay_does_not_broadcast(self):
        alice,bob,headers,cb,team,project=self.prepare()
        base='/api/teams/'+team['id']
        payload={'project_id':project['id'],'title':'反馈','content':'按钮错误','coordinator_id':alice['agent_id'],
                 'request_id':'feedback-upload',
                 'attachments':[attachment(b'\x89PNG\r\n\x1a\n'+b'x'*1024*1024)]}
        first=self.request('POST',base+'/upload-requirement',payload,**headers)
        self.assertEqual(first[0],200,first)
        revision=self.server.teams.events.wait(team['id'],-1,0)
        self.assertEqual(self.request('POST',base+'/upload-requirement',payload,**headers)[2],first[2])
        self.assertEqual(self.server.teams.events.wait(team['id'],-1,0),revision)
        board=self.request('GET',base,Cookie=headers['Cookie'])[2]
        self.assertEqual(board['attachment_policy'],ATTACHMENT_POLICY)

    def test_real_sse_does_not_survive_logout(self):
        alice,bob,headers,cb,team,project=self.prepare()
        base='/api/teams/'+team['id']
        self.assertEqual(self.request('GET',base+'/events',Cookie=cb)[0],403)
        connection=http.client.HTTPConnection('127.0.0.1',self.server.server_port,timeout=4)
        try:
            connection.request('GET',base+'/events',headers={'Host':'dotasks.test','Cookie':headers['Cookie']})
            response=connection.getresponse()
            self.assertEqual(response.status,200)
            lines=[]
            while b'data: {}\n' not in lines:
                lines.append(response.fp.readline())
            self.assertIn(b'event: board_changed\n',lines)
            self.request('POST','/api/auth/logout',{},**headers)
            self.server.teams.events.changed(team['id'])
            self.assertNotIn(b'board_changed',response.read())
        finally:
            connection.close()


class BrowserSyncTests(unittest.TestCase):
    def test_browser_lifecycle(self):
        root=Path(__file__).resolve().parents[1]
        result=subprocess.run([os.environ.get('DOTASKS_NODE_BIN','node'),'--test','web/src/team-board-sync.test.js'],cwd=root,capture_output=True,text=True,timeout=20)
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)


class TeamEventTests(unittest.TestCase):
    def test_team_revision_and_wait_are_isolated(self):
        events=TeamEvents();events.changed('a')
        self.assertEqual(events.wait('a',0,0),1)
        self.assertEqual(events.wait('b',0,0),0)

    def test_stream_rechecks_session_and_releases_capacity_on_revocation(self):
        for session_revoked in [True,False]:
            registry=SimpleNamespace(events=TeamEvents(),directory=SimpleNamespace(role=Mock()))
            handler=Mock();handler.wfile=io.BytesIO()
            if session_revoked:handler._require_authentication.side_effect=HTTPRequestError(401,'revoked')
            else:registry.directory.role.side_effect=HTTPRequestError(403,'removed')
            serve_team_events(handler,registry,'team','actor')
            self.assertNotIn(b'board_changed',handler.wfile.getvalue())
            self.assertTrue(handler.close_connection)
            self.assertEqual(registry.events.slots._value,64)

    def test_changed_event_then_disconnect_keeps_slots_available(self):
        registry=SimpleNamespace(events=TeamEvents(),directory=SimpleNamespace(role=Mock()))
        handler=Mock();handler.wfile=io.BytesIO()
        handler._require_authentication.side_effect=[None,OSError('gone')]
        with patch.object(registry.events,'wait',return_value=2):
            serve_team_events(handler,registry,'team','actor')
        self.assertEqual(handler.wfile.getvalue().count(b'event: board_changed'),1)
        self.assertEqual(registry.events.slots._value,64)
