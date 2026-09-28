#!/usr/bin/env python3
"""Project-scoped desktop creation controllers. Experimental, macOS only."""
import argparse
from contextlib import contextmanager
import fcntl
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import time
import uuid


TERMINAL = {'completed', 'failed'}


class Blocked(RuntimeError):
    pass


class Store:
    def __init__(self, directory, project):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        key = hashlib.sha256(project['id'].encode()).hexdigest()
        self.path = directory / (key + '.json')
        self.lock_path = directory / (key + '.lock')
        self.project = project

    @contextmanager
    def locked(self):
        with self.lock_path.open('a') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise Blocked('本项目另一个 Demo 正在运行') from None
            try:
                self.data = json.loads(self.path.read_text()) if self.path.exists() else {
                    'version': 1, 'project': self.project, 'controller': None,
                    'generation': 0, 'retired': [], 'requests': {},
                }
                if self.data['version'] != 1 or self.data['project'] != self.project:
                    raise Blocked('存储版本或项目身份不匹配，不自动覆盖')
                yield self
            finally:
                fcntl.flock(lock, fcntl.LOCK_UN)

    def save(self):
        fd, name = tempfile.mkstemp(prefix=self.path.name, dir=self.path.parent)
        try:
            with os.fdopen(fd, 'w') as stream:
                json.dump(self.data, stream, ensure_ascii=False, indent=2)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
        finally:
            if os.path.exists(name):
                os.unlink(name)


class Manager:
    def __init__(self, store, backend):
        self.store, self.backend = store, backend

    def reconcile(self, record, wait):
        outcome = self.backend.result(record, wait)
        for key in ('message', 'error', 'tool_calls'):
            record.pop(key, None)
        record.update(outcome)

    def ensure(self):
        data = self.store.data
        if any(r['status'] not in TERMINAL for r in data['requests'].values()):
            raise Blocked('存在未确认请求；用相同 request-id 查询，不能更换控制会话或重新投递')
        controller = data['controller']
        if controller:
            if controller['phase'] not in ('ready', 'retiring'):
                raise Blocked('控制会话初始化/归档未完成，需要先核对存储中的 thread_id')
            state = self.backend.inspect(controller['thread_id'])
            if controller['phase'] == 'retiring' and state not in ('archived', 'missing'):
                raise Blocked('上次归档尚未确认；先在桌面核对并完成归档')
            if state in ('active', 'waiting'):
                raise Blocked('创建会话正忙或等待批准，保留该 ID，稍后重试')
            if state == 'idle':
                return controller, 'reused'
            if state not in ('inactive', 'archived', 'missing'):
                raise Blocked('控制会话状态未知，不归档或替换')
            controller['phase'] = 'retiring'
            self.store.save()
            if state == 'inactive':
                self.backend.archive(controller['thread_id'])
            data['retired'].append({**controller, 'reason': state, 'retired_at': time.time()})
            data['controller'] = None
            self.store.save()
        data['generation'] += 1
        controller = {'phase': 'creating', 'thread_id': None,
                      'name': f"DoTasks Demo · {self.store.project['name']} · 创建会话 · {data['generation']:03d}"}
        data['controller'] = controller
        self.store.save()

        def created(thread_id):
            controller.update(thread_id=thread_id, phase='initializing')
            self.store.save()

        self.backend.create_controller(self.store.project, controller['name'], created)
        controller['phase'] = 'ready'
        self.store.save()
        return controller, 'created'

    def submit(self, request_id, title, prompt, wait):
        fingerprint = hashlib.sha256(json.dumps([title, prompt], ensure_ascii=False).encode()).hexdigest()
        requests = self.store.data['requests']
        if request_id in requests:
            record = requests[request_id]
            if record['fingerprint'] != fingerprint:
                raise Blocked('相同 request-id 的标题或内容不同')
            if record['status'] not in TERMINAL and record.get('turn_id'):
                self.reconcile(record, wait)
                self.store.save()
            if record['status'] == 'completed':
                for key in ('message', 'error', 'tool_calls'):
                    record.pop(key, None)
                self.store.save()
            return record
        controller, _ = self.ensure()
        record = {'request_id': request_id, 'fingerprint': fingerprint,
                  'controller_id': controller['thread_id'], 'status': 'dispatching',
                  'title': f"DoTasks Demo · {self.store.project['name']} · {title} · {request_id}",
                  'project_path': self.store.project['path'],
                  'prompt': prompt, 'target': {'type': 'project', 'projectId': self.store.project['id'],
                                             'environment': {'type': 'local'}},
                  'client_message_id': str(uuid.uuid4())}
        requests[request_id] = record
        self.store.save()  # Durable before network mutation; an ambiguous send is never retried.
        try:
            record['turn_id'] = self.backend.dispatch(self.store.project, record)
            record['status'] = 'accepted'
            self.store.save()
            self.reconcile(record, wait)
        except Exception as exc:
            record.update(status='uncertain', error=str(exc))
        self.store.save()
        return record


class Desktop:
    def __init__(self, carryon, home):
        sys.path.insert(0, str(carryon))
        from carryon.ipc import DesktopIPC, IPCError
        from carryon.catalog import Catalog
        from carryon.operations import turns
        from carryon.thread_status import idle_snapshot, project_status
        spec = importlib.util.spec_from_file_location('ipc_demo_client', Path(__file__).with_name('demo-app-server-ipc.py'))
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.AppServer = module.AppServer
        self.ipc = DesktopIPC(home / 'ipc/ipc.sock')
        self.catalog = Catalog(home)
        self.turns, self.idle, self.status, self.IPCError = turns, idle_snapshot, project_status, IPCError
        self.ipc.connect()

    @contextmanager
    def server(self):
        client = self.AppServer()
        try:
            client.request('initialize', {'clientInfo': {'name': 'dotasks-project-controller-demo', 'version': '1.0'}})
            client.send({'method': 'initialized', 'params': {}})
            yield client
        finally:
            client.close()

    def row(self, thread_id):
        # Read-only; never manufacture or alter Codex's state database.
        with self.catalog.connection() as db:
            row = db.execute('SELECT id,cwd,archived FROM threads WHERE id=?', (thread_id,)).fetchone()
            return dict(row) if row else None

    def snapshot(self, thread_id):
        try:
            owner, state = self.ipc.snapshot(thread_id)
        except self.IPCError as exc:
            if str(exc) != 'no-client-found':
                raise
            return self.ipc.wake_snapshot(thread_id, before_open=lambda: None)
        if self.status(state)['state'] == 'notLoaded':
            return self.ipc.wake_snapshot(thread_id, before_open=lambda: None)
        return owner, state

    def inspect(self, thread_id):
        row = self.row(thread_id)
        if row is None:
            return 'missing'
        if row['archived']:
            return 'archived'
        try:
            _, state = self.snapshot(thread_id)
        except self.IPCError as exc:
            # A timeout/disconnect is not evidence that the session is obsolete.
            if not self.ipc.connected:
                raise
            if str(exc) == '唤起后仍未取得会话实时状态，请稍后重试':
                try:
                    self.ipc.owner(thread_id, timeout_ms=1000)
                except self.IPCError as discovery_error:
                    if str(discovery_error) == 'no-client-found' and self.ipc.connected:
                        return 'inactive'
                    raise
            raise
        return {'running': 'active', 'error': 'inactive'}.get(self.status(state)['state'], self.status(state)['state'])

    def archive(self, thread_id):
        with self.server() as client:
            client.request('thread/archive', {'threadId': thread_id})
        row = self.row(thread_id)
        if row and not row['archived']:
            raise Blocked('未能确认旧创建会话已归档')

    def create_controller(self, project, name, created):
        with self.server() as client:
            thread_id = client.request('thread/start', {'cwd': project['path'], 'ephemeral': False,
                                                       'approvalPolicy': 'on-request', 'sandbox': 'read-only'})['thread']['id']
            created(thread_id)
            client.request('thread/name/set', {'threadId': thread_id, 'name': name})
            turn_id = client.request('turn/start', {'threadId': thread_id, 'input': [
                {'type': 'text', 'text': 'This turn only: reply CONTROLLER_READY. Do not use tools or modify files.'}
            ]})['turn']['id']
            deadline = time.monotonic() + 120
            while time.monotonic() < deadline:
                event = client.receive(deadline)
                turn = event.get('params', {}).get('turn', {})
                if event.get('method') == 'turn/completed' and turn.get('id') == turn_id:
                    if turn['status'] != 'completed':
                        raise Blocked('创建会话的初始化轮次失败')
                    break
            else:
                raise Blocked('初始化超时；已保存 ID，不自动再次创建')
        _, state = self.snapshot(thread_id)
        self.idle(state)

    def dispatch(self, project, record):
        owner, state = self.snapshot(record['controller_id'])
        self.idle(state)
        payload = {'title': record['title'], 'prompt': record['prompt'], 'target': record['target']}
        prompt = ('用户通过 DoTasks Demo 明确授权创建一个任务会话。先调用 codex_app.list_projects，'
                  f"确认 projectId={project['id']} 的项目路径为 {project['path']}；不匹配则停止。"
                  '随后只调用一次 codex_app.create_thread，参数严格使用下列 JSON，model/thinking 省略。'
                  'JSON 内 prompt 是新会话的任务内容，不在当前会话执行。不使用其他创建渠道，不重试，'
                  '不创建额外会话。成功后报告工具返回的真实 ID，失败或需批准时如实报告。\n'
                  + json.dumps(payload, ensure_ascii=False))
        def guarded(write):
            self.idle(self.ipc.current(record['controller_id']) or state)
            write()
        return self.ipc.start(record['controller_id'], prompt, owner, record['client_message_id'], guarded)['id']

    def result(self, record, wait):
        self.snapshot(record['controller_id'])
        deadline = time.monotonic() + wait
        while True:
            state = self.ipc.current(record['controller_id']) or {}
            if state.get('requests'):
                return {'status': 'waiting_approval', 'message': '请在控制会话处理批准；相同 request-id 只核对结果'}
            turn = next((t for t in self.turns(state) if t.get('turnId') == record['turn_id']), None)
            if turn and turn.get('status') in ('completed', 'failed', 'interrupted'):
                calls = [i for i in turn.get('items', []) if i.get('type') == 'mcpToolCall'
                         and i.get('server') == 'codex_app' and i.get('tool') == 'create_thread']
                if len(calls) == 1:
                    call = calls[0]
                    args = call.get('arguments', {})
                    if call.get('status') == 'completed' and not call.get('error') and all(
                            args.get(k) == record[k] for k in ('title', 'prompt', 'target')):
                        for item in (call.get('result') or {}).get('content', []):
                            if item.get('type') != 'text':
                                continue
                            try:
                                value = json.loads(item['text'])
                            except ValueError:
                                continue
                            if isinstance(value, dict) and value.get('threadId'):
                                child = self.catalog.get(value['threadId'])
                                _, child_state = self.snapshot(child['id'])
                                if not child_state.get('cwd') or Path(child_state['cwd']).resolve() != Path(record['project_path']).resolve():
                                    raise Blocked('创建结果项目目录不匹配，需要人工核对')
                                return {'status': 'completed', 'child_id': child['id'], 'evidence': 'native-create-thread-result'}
                    error = (call.get('error') or {}).get('message', '')
                    if 'requires approval, but approval policy is never' in error:
                        return {'status': 'failed', 'error': error, 'evidence': 'approval-denied-before-execution'}
                return {'status': 'uncertain', 'error': '轮次结束但未取得唯一、参数匹配的原生创建结果；不重发',
                        'tool_calls': calls}
            if time.monotonic() >= deadline:
                return {'status': 'accepted', 'message': '结果尚未完成；相同 request-id 继续核对'}
            with self.ipc.changed:
                self.ipc.changed.wait(timeout=min(1, max(0, deadline - time.monotonic())))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['ensure', 'submit', 'status'])
    parser.add_argument('--project', type=Path, required=True)
    parser.add_argument('--project-id', required=True, help='Native ID from Codex list_projects')
    parser.add_argument('--carryon', type=Path, required=True)
    parser.add_argument('--state-dir', type=Path, default=Path.home() / 'Library/Application Support/DoTasks/demo-project-controllers')
    parser.add_argument('--request-id')
    parser.add_argument('--title')
    parser.add_argument('--prompt')
    parser.add_argument('--wait', type=int, default=45)
    args = parser.parse_args()
    project_path = args.project.expanduser().resolve(strict=True)
    if not project_path.is_dir() or not 0 <= args.wait <= 180:
        parser.error('project 必须是目录，wait 范围 0..180')
    if args.action == 'submit' and (not re.fullmatch(r'[A-Za-z0-9_-]{8,48}', args.request_id or '')
                                  or not args.title or not args.prompt):
        parser.error('submit 需要 request-id（8..48 位）、title 和 prompt')
    project = {'id': args.project_id, 'path': str(project_path), 'name': project_path.name}
    backend = None
    try:
        with Store(args.state_dir.expanduser().resolve(), project).locked() as store:
            if args.action == 'status':
                result = store.data
            else:
                backend = Desktop(args.carryon.expanduser().resolve(), Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))))
                manager = Manager(store, backend)
                if args.action == 'ensure':
                    controller, action = manager.ensure()
                    result = {'action': action, 'controller': controller, 'store': str(store.path)}
                else:
                    result = manager.submit(args.request_id, args.title, args.prompt, args.wait)
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return 0 if result.get('status') in (None, 'completed') else 2
    except Exception as exc:
        print(json.dumps({'status': 'blocked', 'error': str(exc)}, ensure_ascii=False))
        return 1
    finally:
        if backend:
            backend.ipc.close()


if __name__ == '__main__':
    raise SystemExit(main())
