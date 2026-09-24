"""A/B lifecycle through authenticated HTTP, durable relay and real local Git.

Only model generation is supplied as a deterministic analysis/review fixture.
No workspace, verification, authorization or workflow method is mocked.
"""
from dataclasses import replace
import http.client
import json
import secrets
import socket
import threading
import time
import unittest
from unittest.mock import Mock, patch

from tests import test_team_lifecycle as lifecycle
from taskboard.agent import AgentConfig, RelayAgent
from taskboard.cloud.server import RelayConfig, build_relay_server
from taskboard.config import ServerConfig
from taskboard.web_auth import session_cookie


class HTTPService:
    def __init__(self,test,name):
        self.test,self.name=test,name
    def __getattr__(self,name):
        def invoke(payload=None):
            agent=name in {'claim_analysis','analysis_result','execution_claim','execution_tool'}
            action={'claim_analysis':'analysis-claim','candidates':'recommend'}.get(name,name.replace('_','-'))
            prefix='/_agent/v1/teams/' if agent else '/api/teams/'
            path=prefix+self.test.team+('' if name=='snapshot' else '/'+action)
            headers={'Authorization':'Bearer '+self.test.tokens[self.name]} if agent else {'Cookie':self.test.cookies[self.name],'Origin':self.test.url}
            body=dict(payload or {})
            if not agent: body['request_id']=secrets.token_hex(16)
            conn=http.client.HTTPConnection('127.0.0.1',self.test.server.server_port,timeout=20)
            conn.request('GET' if name=='snapshot' else 'POST',path,None if name=='snapshot' else json.dumps(body),{'Content-Type':'application/json',**headers})
            response=conn.getresponse();result=json.loads(response.read());conn.close()
            self.test.assertEqual(200,response.status,(action,result))
            return result
        return invoke


class TeamTransportTest(unittest.TestCase):
    upload=lifecycle.TeamLifecycleTest.upload
    analyzed=lifecycle.TeamLifecycleTest.analyzed
    submitted=lifecycle.TeamLifecycleTest.submitted
    assigned=lifecycle.TeamLifecycleTest.assigned
    git=lifecycle.TeamLifecycleTest.git
    analyzed_owner=lifecycle.TeamLifecycleTest.analyzed_owner
    tool=lifecycle.TeamLifecycleTest.tool
    test_complete_delivery_uses_real_git_and_verification=lifecycle.TeamLifecycleTest.test_complete_delivery_uses_real_git_and_verification
    def setUp(self):
        lifecycle.TeamLifecycleTest.setUp(self)
        config=RelayConfig(ServerConfig(mode='cloud',account_mode='multi',host='127.0.0.1',port=0,home=str(self.home),public_url='http://127.0.0.1'),'', '')
        self.server=build_relay_server(config)
        self.url='http://127.0.0.1:'+str(self.server.server_port)
        self.server.base_config=replace(config,server=replace(config.server,public_url=self.url))
        self.http_thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.http_thread.start()
        self.cookies={name:session_cookie(self.server.accounts.create_session(self.ids[name]),secure=False).split(';')[0] for name in ('alice','bob')}
        self.agent=RelayAgent(AgentConfig(self.url,self.ids['bob'],self.tokens['bob'],data_home=str(self.home)),executor=Mock())
        self.errors=[]
        def stream():
            try:self.agent.run_event_stream_once()
            except (ConnectionError,OSError):pass
            except Exception as exc:self.errors.append(exc)
        self.agent.board_hash=lambda:''
        self.agent.metadata=lambda:{}
        self.socket_thread=threading.Thread(target=stream,daemon=True);self.socket_thread.start()
        deadline=time.monotonic()+5
        while not self.server.agent_connected(self.ids['bob']) and time.monotonic()<deadline:time.sleep(.02)
        self.assertTrue(self.server.agent_connected(self.ids['bob']))
        self.a=HTTPService(self,'alice');self.b=HTTPService(self,'bob')

    def tearDown(self):
        for connections in list(self.server._agent_sockets.values()):
            for connection in list(connections.values()):connection.socket.shutdown(socket.SHUT_RDWR)
        self.server.shutdown();self.server.server_close()
        self.http_thread.join(5);self.socket_thread.join(5)
        self.assertFalse(self.errors,self.errors)
        lifecycle.TeamLifecycleTest.tearDown(self)
