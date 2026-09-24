"""Shared cloud ingress with a separate service/database/vault for every account."""
from __future__ import annotations

from dataclasses import replace
from http import HTTPStatus
import json
from pathlib import Path
import threading
from urllib.parse import urlparse

from taskboard.http_security import HTTPRequestError, RequestSecurityPolicy
from taskboard.web_auth import session_cookie, session_token
from .accounts import AccountStore
from .server import CloudTaskboardService, RelayHandler, RelayHTTPServer
from .store import RelayStore


class TenantTaskboardService(CloudTaskboardService):
    def upload_visual_artifact(self, payload):
        if payload.get('path'):
            raise ValueError('Shared cloud accepts uploaded image bytes, not server file paths')
        return super().upload_visual_artifact(payload)

    def _store_managed_artifacts(self, namespace, references):
        if any(not isinstance(item, str) or not item.startswith('artifact://') for item in references):
            raise ValueError('Shared cloud accepts only tenant-managed artifact references')
        return super()._store_managed_artifacts(namespace, references)

    def _run_project_command(self, command, project, timeout_seconds):
        # Deterministic verification belongs on the user's machine, never on the
        # shared cloud host. Reuse the existing durable relay queue.
        timeout = max(1, min(int(timeout_seconds), 60))
        command_id = self.relay_store.enqueue(self.account_id, 'POST', '/api/agent/verify',
            {'Content-Type': 'application/json'}, json.dumps({
                'command': command, 'project': project, 'timeout_seconds': timeout,
            }).encode())
        self.notify(self.account_id, 'command_available')
        result = self.relay_store.wait_result(command_id, timeout + 10)
        if result is None:
            self.relay_store.cancel_queued(command_id)
            return 'error', None, 'Local verification did not return in time', 0
        if result['status'] != 200:
            return 'error', None, 'Local verification failed', 0
        payload = json.loads(result['body'])
        return payload['status'], payload['exit_code'], payload['output'], payload['duration_ms']


class TenantSecurity(RequestSecurityPolicy):
    def __init__(self, handler):
        super().__init__(handler.config, handler.server.server_port)
        self.handler = handler

    def require_authentication(self, headers):
        # An Agent bearer cannot become a browser session, nor can legacy Basic
        # credentials bypass account routing.
        account = self.handler.server.accounts.session(session_token(headers.get('Cookie', '')))
        if not account or account['id'] != getattr(self.handler, 'account', {}).get('id'):
            raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, "请先登录")


class AccountHandler(RelayHandler):
    def _security(self):
        return TenantSecurity(self)

    def _handle_web_auth(self, method, path):
        return False  # Account routes are handled before inherited business routes.

    def _handle_error(self, exc):
        if not isinstance(exc, (ValueError, KeyError)):
            # Do not disclose filesystem locations or internal exceptions across tenants.
            self._json(HTTPStatus.INTERNAL_SERVER_ERROR, {'error': '服务内部错误'})
        else:
            super()._handle_error(exc)

    def _require_agent(self):
        if getattr(self, 'identity_kind', None) != 'agent':
            raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, 'Invalid agent credentials')
        return self.account['id']

    def _public(self, method, path):
        accounts = self.server.accounts
        cookie = session_token(self.headers.get('Cookie', ''))
        if path == '/api/auth/status' and method == 'GET':
            account = accounts.session(cookie)
            self._json(HTTPStatus.OK, {'enabled': True, 'authenticated': bool(account),
                                     **({'username': account['username']} if account else {})})
            return True
        if path not in {'/api/cli/init', '/api/auth/login', '/api/auth/logout'}:
            return False
        if method != 'POST':
            raise HTTPRequestError(HTTPStatus.METHOD_NOT_ALLOWED, 'Method not allowed')
        if path != '/api/cli/init' and self.headers.get('Origin') not in self._trusted_origins():
            raise HTTPRequestError(HTTPStatus.FORBIDDEN, 'Untrusted request origin')
        payload = self._read_json(4096)
        if path == '/api/auth/logout':
            accounts.revoke_session(cookie)
            self._json(HTTPStatus.OK, {'authenticated': False}, {'Set-Cookie': session_cookie('signed-out', secure=True)})
            return True
        if not self.server.web_sessions.allow_login():
            raise HTTPRequestError(HTTPStatus.TOO_MANY_REQUESTS, '请求过于频繁，请稍后重试', {'Retry-After': '60'})
        if path == '/api/cli/init':
            account, token = accounts.initialize(payload.get('username'), payload.get('password'), payload.get('device_id'), payload.get('device_token'))
            store, service = self.server.runtime(account['id'])
            accounts.provision(account['id'], service)
            self._json(HTTPStatus.OK, {'username': account['username'], 'agent_id': account['id'],
                                     'agent_token': token, 'cloud_url': self.config.public_url.rstrip('/')})
        else:
            account = accounts.authenticate(payload.get('username'), payload.get('password'))
            if not account:
                raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, '账号或密码错误')
            accounts.revoke_session(cookie)
            token = accounts.create_session(account['id'])
            self._json(HTTPStatus.OK, {'authenticated': True}, {'Set-Cookie': session_cookie(token, secure=True)})
        return True

    def _dispatch(self, method):
        # Reset per-request state even when the underlying connection is reused.
        self.config = self.server.base_config.server
        self.account = {}
        self.identity_kind = None
        try:
            self._security().validate_origin(self.headers)
            path = urlparse(self.path).path
            if self._public(method, path):
                return
            if path.startswith('/_agent/'):
                auth = self.headers.get('Authorization', '')
                account = self.server.accounts.agent(auth[7:]) if auth.startswith('Bearer ') else None
                self.identity_kind = 'agent'
            elif path.startswith('/api/'):
                account = self.server.accounts.session(session_token(self.headers.get('Cookie', '')))
                self.identity_kind = 'browser'
            else:
                if method != 'GET':
                    raise HTTPRequestError(HTTPStatus.NOT_FOUND, 'Route not found')
                self._serve_static(path)
                return
            if not account:
                raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, 'Authentication required')
            self.account = account
            self.store, self.service = self.server.runtime(account['id'])
            self.config = replace(self.config, home=str(self.service.data_home), http_user=account['username'])
            self.relay_config = replace(self.server.base_config, server=self.config, agent_id=account['id'], agent_token='')
            if path == '/_agent/v1/status' and method == 'GET':
                self._json(HTTPStatus.OK, {'agent_id': account['id'], 'connected': self.server.agent_connected(account['id']),
                                         'dispatcher_enabled': self.service.dispatcher_enabled()})
                return
            # Shared-cloud onboarding never imports an old machine's database.
            if path == '/_agent/v1/bootstrap/status' and method == 'POST':
                payload = self._read_agent_json()
                if payload.get('agent_id') != account['id']:
                    raise HTTPRequestError(HTTPStatus.FORBIDDEN, 'Agent ID mismatch')
                self._json(HTTPStatus.OK, {'accept_snapshot': False})
                return
            if path == '/_agent/v1/bootstrap':
                raise HTTPRequestError(HTTPStatus.FORBIDDEN, 'Shared cloud does not accept database imports')
            getattr(super(), f'do_{method}')()
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception as exc:
            self._handle_error(exc)

    def do_GET(self):
        self._dispatch('GET')

    def do_POST(self):
        self._dispatch('POST')

    def do_PATCH(self):
        self._dispatch('PATCH')

    def do_OPTIONS(self):
        self._dispatch('OPTIONS')


class AccountHTTPServer(RelayHTTPServer):
    def __init__(self, config, handler):
        self.base_config = config
        self.home = config.server.data_home or Path.home() / '.local/share/DoTasks'
        self.accounts = AccountStore(self.home)
        self._runtimes = {}
        self._runtime_lock = threading.Lock()
        super().__init__((config.server.host, config.server.port), handler)

    def runtime(self, account_id):
        # account_id is resolved exclusively by AccountStore, never by payload or URL.
        with self._runtime_lock:
            if account_id not in self._runtimes:
                home = self.home / 'tenants' / account_id
                home.mkdir(parents=True, exist_ok=True, mode=0o700)
                store = RelayStore(home)
                service = TenantTaskboardService(home, self.base_config.server.public_url)
                service.relay_store, service.account_id, service.notify = store, account_id, self.notify_agent
                self._runtimes[account_id] = store, service
            return self._runtimes[account_id]

    def agent_connected(self, account_id):
        with self._agent_sockets_lock:
            return bool(self._agent_sockets.get(account_id))

    def notify_agent(self, agent_id, event):
        store, _ = self.runtime(agent_id)
        payload = json.dumps({'type': event, 'event_id': store.latest_event_id()}).encode()
        with self._agent_sockets_lock:
            connections = list(self._agent_sockets.get(agent_id, {}).items())
        delivered = 0
        for registration, connection in connections:
            try:
                connection.send(payload)
                delivered += 1
            except OSError:
                self.unregister_agent_socket(agent_id, registration)
        return delivered


def build_account_server(config):
    import os
    config.server.validate()
    if os.environ.get('DOTASKS_OBSIDIAN_VAULT'):
        raise ValueError('Shared cloud must use account-scoped vaults; unset DOTASKS_OBSIDIAN_VAULT')
    public = urlparse(config.server.public_url)
    if public.scheme != 'https' and public.hostname not in {'127.0.0.1', 'localhost', '::1'}:
        raise ValueError('Shared cloud requires an HTTPS public URL')
    handler = type('ConfiguredAccountHandler', (AccountHandler,), {
        'config': config.server,
        'static_root': Path(__file__).resolve().parents[2] / 'static',
    })
    return AccountHTTPServer(config, handler)
