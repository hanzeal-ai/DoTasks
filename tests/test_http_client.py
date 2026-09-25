from __future__ import annotations

import json
import tempfile
import threading
import unittest
import urllib.error
from contextlib import ExitStack
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock

from taskboard.agent import AgentConfig, RelayAgent
from taskboard.http_client import read_json
from taskboard.remote_service import RemoteToolClient
from taskboard.team_agent import TeamClient


class AuthenticatedHTTPTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.home = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.received = []
        self.redirect_status = None
        test = self

        class Destination(BaseHTTPRequestHandler):
            def do_GET(self):
                test.received.append(self.headers.get('Authorization'))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"result": {"ok": true}}')

            do_POST = do_GET

            def log_message(self, *args):
                pass

        target = self.start_server(Destination)

        class Origin(BaseHTTPRequestHandler):
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                if test.redirect_status:
                    self.send_response(test.redirect_status)
                    self.send_header('Location', f'http://127.0.0.1:{target.server_port}/capture')
                    self.end_headers()
                else:
                    test.received.append(self.headers.get('Authorization'))
                    body = json.dumps({'result': {'ok': True}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

            def log_message(self, *args):
                pass

        origin = self.start_server(Origin)
        self.url = f'http://127.0.0.1:{origin.server_port}'
        self.token = 'test-only-agent-token-with-no-real-access'
        self.remote = RemoteToolClient(self.url, 'test-agent', self.token)
        self.agent = RelayAgent(AgentConfig(self.url, 'test-agent', self.token, data_home=str(self.home)), executor=Mock())
        self.team = TeamClient(self.url, 'test-agent', self.token)

    def start_server(self, handler):
        server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.stack.callback(server.server_close)
        self.stack.callback(thread.join, 5)
        self.stack.callback(server.shutdown)
        return server

    def clients(self):
        return {
            'remote_tool': lambda: self.remote.call('list_board', {}),
            'agent_relay': lambda: self.agent._cloud_request('/_agent/v1/claim', {}),
            'cli_json': lambda: read_json(self.url, {}, self.token),
            'team': lambda: self.team.request('/_agent/v1/teams/test', {}),
        }

    def test_redirects_never_forward_credentials_to_another_origin(self):
        for code in (301, 302, 303, 307, 308):
            self.redirect_status = code
            for name, request in self.clients().items():
                with self.subTest(code=code, client=name):
                    with self.assertRaises((urllib.error.HTTPError, RuntimeError)) as failure:
                        request()
                    error = failure.exception
                    while error is not None:
                        if isinstance(error, urllib.error.HTTPError):
                            error.close()
                        error = error.__cause__
                    self.assertEqual([], self.received)

    def test_direct_requests_retain_authentication_and_response_contract(self):
        for name, request in self.clients().items():
            with self.subTest(client=name):
                response = request()
                self.assertEqual({'ok': True} if name == 'remote_tool' else {'result': {'ok': True}}, response)
        self.assertEqual(['Bearer ' + self.token] * len(self.clients()), self.received)
