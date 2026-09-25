import json
import socket
import unittest
from unittest.mock import patch
from tests import test_team_http
from taskboard.cloud.task_source import authorization_address, source_url, fetch_source, _public_address, _PinnedHTTPS

class FeedTransportTests(unittest.TestCase):
    def test_authorization_fragment_is_separated_and_urls_are_restricted(self):
        self.assertEqual(authorization_address('https://feed.example/items#token=secret'),('https://feed.example/items','secret'))
        with self.assertRaisesRegex(ValueError,'来源地址无效'):
            source_url('https://\ud800.example')
        for value in ['http://example.com','https://u:p@example.com','https://example.com:444','https://example.com/?token=secret','https://example.com/#bad=value']:
            with self.assertRaises(ValueError):authorization_address(value)
        for ip in ['127.0.0.1','10.0.0.1','169.254.169.254','::1','::ffff:127.0.0.1']:
            with patch('socket.getaddrinfo',return_value=[(socket.AF_INET,socket.SOCK_STREAM,6,'',(ip,443))]):
                with self.assertRaises(ValueError):_public_address('feed.example')

    def test_pagination_validation_and_limits(self):
        item={'id':'a','title':'按钮','content':'页面中错位','url':'https://example.com/feedback/a'}
        pages=[json.dumps({'items':[item],'nextCursor':'next'}).encode(),json.dumps({'items':[{**item,'id':'b'}],'nextCursor':None}).encode()]
        with patch('taskboard.cloud.task_source._get_page',side_effect=pages) as get:
            self.assertEqual(len(fetch_source('https://feed.example/items','secret')),2)
            self.assertEqual(get.call_args_list[1].args[0],'https://feed.example/items?cursor=next')
            self.assertEqual(get.call_args.args[1],'secret')
        for payload in [{'items':[item,item]}, {'items':[{'id':'a'}]}, {'items':[dict(item,url='javascript:alert(1)')]}, {'items':[],'nextCursor':'x'}]:
            with patch('taskboard.cloud.task_source._get_page',return_value=json.dumps(payload).encode()):
                with self.assertRaises(ValueError):fetch_source('https://feed.example/items','secret')
        with patch('taskboard.cloud.task_source._get_page',return_value=b'<html>login</html>'):
            with self.assertRaises(ValueError):fetch_source('https://feed.example/items','secret')

    def test_transport_refuses_redirects_auth_errors_and_oversized_responses(self):
        from unittest.mock import Mock
        from taskboard.cloud.task_source import _get_page
        for status, data in [(302,b''),(401,b'private error'),(403,b'private error'),(200,b'x'*11)]:
            connection=Mock();response=Mock(status=status)
            response.getheader.return_value='identity';response.read.return_value=data
            connection.getresponse.return_value=response
            with patch('taskboard.cloud.task_source._public_address',return_value='93.184.216.34'), patch('taskboard.cloud.task_source._PinnedHTTPS',return_value=connection):
                with self.assertRaises(ValueError) as error:
                    _get_page('https://feed.example/items','secret',10,__import__('time').monotonic()+30)
            self.assertNotIn('secret',str(error.exception));self.assertNotIn('private error',str(error.exception))
            connection.request.assert_called_once_with('GET','/items',headers={'Accept':'application/json','Accept-Encoding':'identity','Authorization':'Bearer secret'})
            connection.close.assert_called_once()

    def test_vetted_address_is_pinned_without_changing_tls_hostname(self):
        from unittest.mock import Mock
        raw,wrapped=Mock(),Mock()
        connection=_PinnedHTTPS('feed.example','93.184.216.34',2)
        connection._context=Mock();connection._context.wrap_socket.return_value=wrapped
        with patch('socket.create_connection',return_value=raw) as connect:
            connection.connect()
        connect.assert_called_once_with(('93.184.216.34',443),2)
        connection._context.wrap_socket.assert_called_once_with(raw,server_hostname='feed.example')
        connection.close()


class TaskSourceHTTPTests(unittest.TestCase):
    setUp=test_team_http.TeamHTTPTest.setUp
    tearDown=test_team_http.TeamHTTPTest.tearDown
    request=test_team_http.TeamHTTPTest.request
    register=test_team_http.TeamHTTPTest.register
    login=test_team_http.TeamHTTPTest.login
    prepare=test_team_http.TeamHTTPTest.prepare

    def setup_source(self):
        alice,bob,headers,cb,team,project=self.prepare()
        base='/api/teams/'+team['id']
        payload={'project_id':project['id'],'url':'https://feed.example/items#token=source-secret',
                 'coordinator_id':alice['agent_id'],'version':0,'enabled':True,'request_id':'source-save'}
        response=self.request('POST',base+'/save-task-source',payload,**headers)
        self.assertEqual(response[0],200,response)
        pull={'project_id':project['id'],'version':1,'request_id':'pull-1'}
        return alice,bob,headers,cb,team,project,base,payload,pull

    def test_pull_is_user_authorized_idempotent_and_persists_only_new_drafts(self):
        alice,bob,headers,cb,team,project,base,payload,pull=self.setup_source()
        item={'id':'external-1','title':'修复按钮','content':'按钮错位','url':'https://feed.example/report/1'}
        with patch('taskboard.cloud.team_http.fetch_source',return_value=[item]) as fetch:
            denied=self.request('POST',base+'/pull-task-source',pull,Cookie=cb,Origin='https://dotasks.test')
            self.assertEqual(denied[0],403);fetch.assert_not_called()
            first=self.request('POST',base+'/pull-task-source',pull,**headers)
            self.assertEqual(first[0],200,first);self.assertEqual(first[2]['created'],1)
            self.assertEqual(self.request('POST',base+'/pull-task-source',pull,**headers)[2],first[2])
            self.assertEqual(fetch.call_count,1)
            again=self.request('POST',base+'/pull-task-source',{**pull,'request_id':'pull-2'},**headers)
            self.assertEqual(again[2]['skipped'],1)
        board=self.request('GET',base,Cookie=headers['Cookie'])[2]
        self.assertNotIn('source-secret',json.dumps(board))
        self.assertNotIn('#token',json.dumps(board))
        self.assertEqual(board['requirements'][0]['status'],'draft')
        self.assertEqual(board['requirements'][0]['source_reference'],item['url'])
        self.assertEqual(board['jobs'][0]['status'],'awaiting_permission')
        self.assertEqual(len(board['requirements']),1)
        _,service=self.server.teams.runtime(team['id'],alice['agent_id'])
        with service.db.connection() as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM team_source_imports').fetchone()[0],1)
        self.assertEqual(service.db.path.stat().st_mode & 0o777,0o600)
        self.assertEqual(service.db.path.parent.stat().st_mode & 0o777,0o700)

    def test_configuration_change_clears_credential_and_rejects_inflight_old_source(self):
        alice,bob,headers,cb,team,project,base,payload,pull=self.setup_source()
        def fetch(*args):
            # A second browser request can complete while the source is waiting on the network.
            changed=self.request('POST',base+'/save-task-source',{**payload,'url':'https://different.example/items#token=new-secret','version':1,'request_id':'source-change'},**headers)
            self.assertEqual(changed[0],200,changed)
            return []
        with patch('taskboard.cloud.team_http.fetch_source',side_effect=fetch):
            self.assertEqual(self.request('POST',base+'/pull-task-source',pull,**headers)[0],409)
        board=self.request('GET',base,Cookie=headers['Cookie'])[2]
        self.assertTrue(board['task_sources'][0]['has_token'])
        _,service=self.server.teams.runtime(team['id'],alice['agent_id'])
        with service.db.connection() as db:
            self.assertEqual(db.execute('SELECT token FROM team_task_sources').fetchone()[0],'new-secret')
        self.assertEqual(board['requirements'],[])

    def test_remote_failure_does_not_mutate_and_logout_during_fetch_is_rechecked(self):
        alice,bob,headers,cb,team,project,base,payload,pull=self.setup_source()
        with patch('taskboard.cloud.team_http.fetch_source',side_effect=ValueError('来源拒绝访问')):
            self.assertEqual(self.request('POST',base+'/pull-task-source',pull,**headers)[0],400)
        self.assertEqual(self.request('GET',base,Cookie=headers['Cookie'])[2]['requirements'],[])
        def logout(*args):
            self.request('POST','/api/auth/logout',{},**headers)
            return [{'id':'a','title':'a','content':'a','url':'https://example.com/a'}]
        with patch('taskboard.cloud.team_http.fetch_source',side_effect=logout):
            self.assertEqual(self.request('POST',base+'/pull-task-source',pull,**headers)[0],401)

    def test_new_source_requires_authorization_and_cannot_reuse_old_token_for_a_new_destination(self):
        alice,bob,headers,cb,team,project,base,payload,pull=self.setup_source()
        response=self.request('POST',base+'/save-task-source',{**payload,'url':'https://other.example/items','version':1,'request_id':'no-token'},**headers)
        self.assertEqual(response[0],400,response)
        with self.assertRaises(ValueError):fetch_source('https://feed.example/items')

    def test_malformed_unicode_hostname_is_rejected_without_changing_source(self):
        alice,bob,headers,cb,team,project,base,payload,pull=self.setup_source()
        response=self.request('POST',base+'/save-task-source',
            {**payload,'url':'https://'+'a'*64+'.example/items#token=secret','version':1,'request_id':'invalid-host'},**headers)
        self.assertEqual(response[0],400,response)
        self.assertIn('来源地址无效',json.dumps(response[2],ensure_ascii=False))
        board=self.request('GET',base,Cookie=headers['Cookie'])[2]
        self.assertEqual(board['task_sources'][0]['version'],1)

    def test_import_failure_rolls_back_whole_batch_and_source_ledger(self):
        alice,bob,headers,cb,team,project,base,payload,pull=self.setup_source()
        items=[{'id':'a','title':'a','content':'a','url':'https://example.com/a'}, {'id':'b','title':'','content':'b','url':'https://example.com/b'}]
        with patch('taskboard.cloud.team_http.fetch_source',return_value=items):
            self.assertEqual(self.request('POST',base+'/pull-task-source',pull,**headers)[0],400)
        board=self.request('GET',base,Cookie=headers['Cookie'])[2]
        self.assertEqual(board['requirements'],[]);self.assertEqual(board['jobs'],[])

if __name__=='__main__':unittest.main()
