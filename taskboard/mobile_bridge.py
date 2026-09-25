"""Bounded JSON bridge for a local personal messaging gateway; no shell dispatch."""
from __future__ import annotations

import json
import hashlib
import sys
import urllib.request
from urllib.parse import urlparse

from .http_client import NoRedirect
from .runtime_paths import default_data_home
from .remote_service import RemoteToolClient


METHODS = {
    'identity': 'identity',
    'conversations': 'mobile_conversations', 'events': 'mobile_events',
    'enqueue': 'enqueue_mobile_message', 'claim': 'claim_mobile_message',
    'update': 'update_mobile_message',
    'recover': 'recover_mobile_messages',
    'usage': 'record_mobile_usage',
    'prepare': 'prepare_mobile_turn',
}


def mobile_handlers_for(service):
    # Transport operations are not advertised as autonomous lifecycle MCP tools.
    return {name: (lambda arguments, method=name: getattr(service, method)(**arguments))
            for name in METHODS.values() if name != 'identity'}


class MobileService:
    def __init__(self):
        self.remote = RemoteToolClient.from_environment()
        self.local = None
        if self.remote:
            url = urlparse(self.remote.cloud_url)
            if url.scheme != 'https' or url.username or url.password or url.query or url.fragment or url.path not in {'', '/'}:
                raise ValueError('Mobile access requires an HTTPS DoTasks origin; update cloud-agent configuration')
        else:
            from core.service import TaskboardService
            self.local = TaskboardService(default_data_home())

    def call(self, action: str, arguments: dict):
        if action == 'identity':
            source = [self.remote.cloud_url, self.remote.agent_id, self.remote.agent_token] if self.remote else [str(self.local.db.path)]
            return hashlib.sha256(json.dumps(source).encode()).hexdigest()
        name = METHODS[action]
        if self.local:
            return getattr(self.local, name)(**arguments)
        remote = self.remote
        request = urllib.request.Request(
            remote.cloud_url + '/_agent/v1/tools/call',
            data=json.dumps({'agent_id': remote.agent_id, 'name': name, 'arguments': arguments}).encode(),
            headers={'Authorization': f'Bearer {remote.agent_token}', 'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.build_opener(NoRedirect).open(request, timeout=20) as response:
            result = json.loads(response.read(2 * 1024 * 1024))
        if not isinstance(result, dict) or 'result' not in result:
            raise ValueError('Invalid mobile service response')
        return result['result']


def main():
    try:
        request = json.loads(sys.stdin.read(32769))
        if not isinstance(request, dict) or request.get('action') not in METHODS:
            raise ValueError('Invalid mobile bridge action')
        arguments = request.get('arguments', {})
        if not isinstance(arguments, dict):
            raise ValueError('Invalid mobile bridge arguments')
        result = MobileService().call(request['action'], arguments)
        print(json.dumps({'result': result}, ensure_ascii=False))
    except Exception:
        # No provider error bodies, credentials or conversation content on stderr.
        print(json.dumps({'error': 'DoTasks mobile bridge failed; check HTTPS, agent authorization and server version'}))
        sys.exit(1)


if __name__ == '__main__':
    main()
