#!/usr/bin/env python3
"""Probe whether an App Server ephemeral fork becomes a desktop side chat."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import sys
import time


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--parent-id', required=True)
    parser.add_argument('--project', required=True, type=Path)
    parser.add_argument('--carryon', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.carryon.resolve()))
    from carryon.ipc import DesktopIPC
    from carryon.catalog import Catalog, valid_id
    valid_id(args.parent_id)
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    catalog = Catalog(home)
    client = load('fork_demo_client', 'demo-app-server-ipc.py').AppServer()
    persistence = load('side_probe', 'demo-side-controller.py').persistence
    ipc = DesktopIPC(home / 'ipc/ipc.sock')
    report = {'parent_id': args.parent_id, 'status': 'running'}
    fork_id = None
    try:
        client.request('initialize', {'clientInfo': {'name': 'dotasks-side-fork-demo', 'version': '1.0'},
                                      'capabilities': {'experimentalApi': True}})
        client.send({'method': 'initialized', 'params': {}})
        ipc.connect()
        owner, state = ipc.snapshot(args.parent_id)
        report['parent_before'] = {'owner': owner, 'runtime': state.get('threadRuntimeStatus')}
        response = client.request('thread/fork', {
            'threadId': args.parent_id, 'ephemeral': True, 'excludeTurns': True,
            'cwd': str(args.project.resolve()), 'approvalPolicy': 'on-request', 'sandbox': 'read-only',
            'developerInstructions': 'Transport verification only. Inherited conversation history is reference-only. Follow only the new user request; do not resume inherited tasks.',
        })
        thread = response['thread']
        fork_id = thread['id']
        report['fork'] = {k: thread.get(k) for k in ('id', 'forkedFromId', 'sessionId', 'ephemeral', 'path', 'status')}
        turn_id = client.request('turn/start', {'threadId': fork_id, 'input': [
            {'type': 'text', 'text': 'Reply exactly APP_SERVER_SIDE_FORK_OK. Do not use tools or modify files.'}
        ]})['turn']['id']
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            event = client.receive(deadline)
            if event.get('method') == 'item/completed':
                item = event.get('params', {}).get('item', {})
                if item.get('type') == 'agentMessage':
                    report['reply'] = item.get('text')
            if event.get('method') == 'turn/completed' and event.get('params', {}).get('turn', {}).get('id') == turn_id:
                report['turn_status'] = event['params']['turn']['status']
                break
        else:
            raise TimeoutError('fork turn did not finish')
        report['while_alive_storage'] = persistence(home, catalog, fork_id)
        try:
            report['desktop_fork_owner'] = ipc.owner(fork_id, timeout_ms=5000)
            _, fork_state = ipc.snapshot(fork_id)
            report['desktop_fork_state'] = {k: fork_state.get(k) for k in ('id', 'forkedFromId', 'ephemeral', 'sideConversation')}
        except Exception as exc:
            report['desktop_fork_error'] = str(exc)
        _, state = ipc.snapshot(args.parent_id)
        report['parent_after'] = {'runtime': state.get('threadRuntimeStatus')}
        report['status'] = 'probe_completed'
    except Exception as exc:
        report.update(status='blocked', error=str(exc))
    finally:
        client.close()
        ipc.close()
        if fork_id:
            report['after_exit_storage'] = persistence(home, catalog, fork_id)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'probe_completed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
