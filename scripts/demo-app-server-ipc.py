#!/usr/bin/env python3
"""Probe shared-store App Server -> CarryOn desktop IPC (experimental).

Creates one fixed-text parent session. --create-child authorizes one desktop
child through one model turn after owner discovery. Never retries creation.
Requires a local CarryOn checkout and running Codex desktop. No config edits.
"""
import argparse
import json
import os
from pathlib import Path
import queue
import sys
import subprocess
import threading
import time
import uuid


class AppServer:
    def __init__(self):
        self.process = subprocess.Popen(
            ['codex', 'app-server', '--listen', 'stdio://'],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True,
        )
        self.messages = queue.Queue()
        self.sequence = 0
        threading.Thread(target=self.read, daemon=True).start()

    def read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put({'eof': True})

    def send(self, value):
        self.process.stdin.write(json.dumps(value) + '\n')
        self.process.stdin.flush()

    def receive(self, deadline):
        value = self.messages.get(timeout=max(0.01, deadline - time.monotonic()))
        if value.get('eof'):
            raise RuntimeError('App Server closed stdout')
        # No tool execution or approval is needed for the fixed-text probe.
        if 'id' in value and 'method' in value:
            self.send({'id': value['id'], 'error': {'code': -32601, 'message': 'Demo does not execute tools'}})
        return value

    def request(self, method, params):
        self.sequence += 1
        self.send({'id': self.sequence, 'method': method, 'params': params})
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            value = self.receive(deadline)
            if value.get('id') == self.sequence and 'method' not in value:
                if 'error' in value:
                    raise RuntimeError(value['error'])
                return value['result']
        raise TimeoutError(method)

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait()
        self.process.stdout.close()



def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--create-child', action='store_true')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--carryon', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.carryon.resolve()))
    from carryon.ipc import DesktopIPC
    from carryon.thread_status import idle_snapshot
    from carryon.operations import turns
    report = {'status': 'running', 'child': 'not_created'}
    ipc = None

    def save():
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n')

    def progress(message):
        save()
        print(message, flush=True)
    server = AppServer()
    stage = 'app_server_initialize'
    try:
        report['server'] = server.request('initialize', {'clientInfo': {'name': 'dotasks-ipc-demo', 'version': '1.0'}})
        server.send({'method': 'initialized', 'params': {}})
        stage = 'parent_create'
        parent = server.request('thread/start', {
            'cwd': str(Path.cwd()), 'ephemeral': False,
            'approvalPolicy': 'never', 'sandbox': 'read-only',
        })['thread']['id']
        report['parent_id'] = parent
        server.request('thread/name/set', {'threadId': parent, 'name': 'DoTasks IPC Demo A2'})
        stage = 'parent_turn'
        turn = server.request('turn/start', {'threadId': parent, 'input': [{'type': 'text', 'text': 'Reply only DOTASKS_IPC_PARENT_OK. Do not call tools.'}]})['turn']['id']
        report['parent_turn_id'] = turn
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            event = server.receive(deadline)
            if event.get('method') == 'turn/completed' and event.get('params', {}).get('turn', {}).get('id') == turn:
                report['parent_turn_status'] = event['params']['turn']['status']
                if report['parent_turn_status'] != 'completed':
                    raise RuntimeError(event['params']['turn'])
                break
        else:
            raise TimeoutError('parent turn')
        server.close()
        server = None
        progress('Parent completed; independent App Server stopped')
        stage = 'desktop_ipc_connect'
        ipc = DesktopIPC(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'ipc/ipc.sock')
        ipc.connect()
        report['ipc_handshake'] = 'passed'
        stage = 'desktop_owner_snapshot'
        try:
            owner, state = ipc.snapshot(parent)
        except Exception as exc:
            report['initial_snapshot_error'] = str(exc)
            owner, state = ipc.wake_snapshot(parent, before_open=lambda: None)
            report['wake_required'] = True
        if (state.get('threadRuntimeStatus') or {}).get('type') == 'notLoaded':
            owner, state = ipc.wake_snapshot(parent, before_open=lambda: None)
            report['wake_required'] = True
        idle_snapshot(state)
        report['desktop_owner'] = owner
        report['desktop_parent_status'] = state.get('threadRuntimeStatus')
        progress('Desktop owner found; parent idle')
        if args.create_child:
            stage = 'desktop_start_create_turn'
            prompt = ('用户明确授权本次 IPC Demo 创建一个新会话。请只调用一次 codex_app 的 create_thread，'
                      'target={"type":"projectless"}，title="DoTasks IPC Demo B"，'
                      'prompt="Transport test only. Reply exactly DOTASKS_IPC_CHILD_OK. Do not use tools or change files."。'
                      '不要指定 model/thinking，不要在当前会话执行子任务。创建后报告工具返回的真实 threadId。'
                      '若工具不可用或创建失败，请报告原因，不要重试，不要用命令行或其他接口替代。')
            def before_send(write):
                idle_snapshot(ipc.current(parent) or state)
                report['child'] = 'creation_requested_outcome_unknown'
                save()
                write()
            native_turn = ipc.start(parent, prompt, owner, str(uuid.uuid4()), before_send)
            report['desktop_turn_id'] = native_turn['id']
            progress('Creation instruction accepted by desktop')
            stage = 'desktop_create_result'
            deadline = time.monotonic() + 180
            completed = None
            while time.monotonic() < deadline:
                current = ipc.current(parent) or {}
                if current.get('requests'):
                    report['pending_requests'] = current['requests']
                    raise RuntimeError('Desktop requires approval/input; inspect parent, do not resend')
                completed = next((t for t in turns(current) if t.get('turnId') == native_turn['id']), None)
                if completed and completed.get('status') in ('completed', 'failed', 'interrupted'):
                    break
                with ipc.changed:
                    ipc.changed.wait(timeout=1)
            else:
                raise TimeoutError('Creation outcome unknown; inspect parent, do not resend')
            report['desktop_turn'] = completed
            calls = [i for i in completed.get('items', []) if i.get('type') == 'mcpToolCall'
                     and i.get('server') == 'codex_app' and i.get('tool') == 'create_thread']
            if len(calls) != 1 or calls[0].get('status') != 'completed' or calls[0].get('error'):
                raise RuntimeError('No single successful native create_thread result; inspect evidence')
            child_id = None
            for item in (calls[0].get('result') or {}).get('content', []):
                if item.get('type') == 'text':
                    try:
                        child_id = json.loads(item['text']).get('threadId') or child_id
                    except (ValueError, AttributeError):
                        continue
            if not child_id:
                raise RuntimeError('Native tool did not return threadId; do not infer from model reply')
            report['child'] = child_id
            report['child_execution'] = 'requires_independent_read'
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='blocked', failed_stage=stage, error=f'{type(exc).__name__}: {exc}')
    finally:
        if server is not None:
            server.close()
        if ipc is not None:
            ipc.close()
        save()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
