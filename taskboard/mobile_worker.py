"""Resume queued conversations on the execution machine using existing Codex homes."""
from __future__ import annotations

import json
import signal
import fcntl
import socket
import threading
import time
from pathlib import Path

from .app_server import CodexAppServerClient
from .mobile_bridge import MobileService
from .remote_service import _default_data_home


def rollout_usage(path: str | None) -> dict[str, int]:
    # Snapshot the original thread's cumulative usage before submitting a turn.
    # Fail closed if this app-server version cannot expose an auditable baseline.
    if not path:
        raise ValueError('Original conversation usage baseline is unavailable')
    latest = None
    with Path(path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            payload = event.get('payload') or {}
            if event.get('type') == 'event_msg' and payload.get('type') == 'token_count':
                latest = (payload.get('info') or {}).get('total_token_usage') or latest
    if not isinstance(latest, dict):
        raise ValueError('Original conversation usage baseline is unavailable')
    fields = {'token_used': 'total_tokens', 'input_tokens': 'input_tokens',
              'cached_input_tokens': 'cached_input_tokens', 'output_tokens': 'output_tokens',
              'reasoning_output_tokens': 'reasoning_output_tokens'}
    result = {key: latest.get(field) for key, field in fields.items()}
    if any(type(value) is not int or value < 0 for value in result.values()):
        raise ValueError('Invalid conversation usage baseline')
    return result


def execute_message(service, message, client, stop: threading.Event):
    turn_id = ''
    submitted = False
    try:
        client.start()
        resumed = client.request('thread/resume', {'threadId': message['thread_id']})
        if (resumed.get('thread') or {}).get('id') != message['thread_id']:
            raise ValueError('Original conversation cannot be resumed')
        baseline = rollout_usage(resumed['thread'].get('path'))
        service.call('prepare', {'message_id': message['id'], 'worker_id': message['worker_id'], 'baseline': baseline})
        usage_recorded = False
        session_baseline = None
        previous_usage = {key: 0 for key in baseline}
        budget_exhausted = False
        # Preserve the original thread's cwd, model, sandbox and approval settings.
        # Never fall back to a new thread or inherit the gateway's workspace.
        submitted = True
        response = client.request('turn/start', {
            'threadId': message['thread_id'],
            'input': [{'type': 'text', 'text': (
                '这是用户通过已绑定的手机入口发给当前会话的新消息。继续原会话上下文；'
                '不要重复提交此前已结束 run 的生命周期回调；新的执行仍须遵循项目授权边界。\n\n'
                + message['body']
            )}],
        })
        turn_id = str((response.get('turn') or {}).get('id') or '')
        if not turn_id:
            raise ValueError('Missing turn id')
        service.call('update', {'message_id': message['id'], 'worker_id': message['worker_id'],
                               'status': 'running', 'turn_id': turn_id})
        output = {}
        deadline = time.monotonic() + 7200
        while not stop.is_set() and time.monotonic() < deadline:
            event = client.wait_notification(timeout=1)
            if not client.connected:
                raise ConnectionError('Executor disconnected')
            if not event:
                continue
            params = event.get('params') or {}
            if params.get('threadId') != message['thread_id']:
                continue
            if event.get('method') == 'thread/tokenUsage/updated' and params.get('turnId') == turn_id:
                total = (params.get('tokenUsage') or {}).get('total') or {}
                names = {'token_used': 'totalTokens', 'input_tokens': 'inputTokens',
                         'cached_input_tokens': 'cachedInputTokens', 'output_tokens': 'outputTokens',
                         'reasoning_output_tokens': 'reasoningOutputTokens'}
                last = (params.get('tokenUsage') or {}).get('last') or {}
                if any(type(total.get(name)) is not int or type(last.get(name)) is not int
                       or not 0 <= last[name] <= total[name] for name in names.values()):
                    raise ValueError('Invalid conversation usage')
                # Rate-limit notifications can repeat the historical usage under
                # the new turn ID. Ignore those snapshots. Establish a baseline
                # only for a proven continuation or a fresh session counter.
                if session_baseline is None:
                    current = {key: total[name] for key, name in names.items()}
                    if current == baseline:
                        continue
                    before = {key: total[name] - last[name] for key, name in names.items()}
                    if before == baseline:
                        session_baseline = baseline
                    elif all(value == 0 for value in before.values()):
                        session_baseline = before
                    else:
                        raise ValueError('Cannot establish mobile inference usage baseline')
                usage = {key: total[name] - session_baseline[key] for key, name in names.items()}
                if any(usage[key] < previous_usage[key] for key in names):
                    raise ValueError('Conversation usage reset within mobile turn')
                previous_usage = usage
                result = service.call('usage', {'message_id': message['id'], 'worker_id': message['worker_id'],
                                                'turn_id': turn_id, 'usage': usage})
                usage_recorded = True
                if result['exhausted'] and not budget_exhausted:
                    budget_exhausted = True
                    client.interrupt_turn(message['thread_id'], turn_id)
            if event.get('method') == 'item/completed' and params.get('turnId') == turn_id:
                item = params.get('item') or {}
                if item.get('type') == 'agentMessage':
                    output[str(item.get('id') or len(output))] = str(item.get('text') or '')
            if event.get('method') == 'turn/completed' and (params.get('turn') or {}).get('id') == turn_id:
                if not usage_recorded:
                    raise ValueError('Turn ended without auditable token usage')
                status = 'completed' if params['turn'].get('status') == 'completed' else 'failed'
                service.call('update', {
                    'message_id': message['id'], 'worker_id': message['worker_id'],
                    'status': status, 'turn_id': turn_id,
                    'result': ('任务预算已耗尽，追加已暂停。\n' if budget_exhausted else '') + ('\n\n'.join(output.values())[-16000:] or '会话已结束，请查看任务详情。'),
                })
                return
        raise TimeoutError('Conversation interrupted or exceeded two hours')
    except Exception as exc:
        print(f'Mobile turn reconciliation required: message={message["id"]} turn={turn_id or "unassigned"} error={type(exc).__name__}', flush=True)
        if turn_id:
            try:
                client.interrupt_turn(message['thread_id'], turn_id)
            except Exception:
                pass
        service.call('update', {
            'message_id': message['id'], 'worker_id': message['worker_id'],
            'status': 'uncertain' if submitted else 'failed', 'turn_id': turn_id,
            'result': '会话投递或执行结果待核对，请勿重复发送。' if submitted else '原会话恢复失败，未创建新会话。',
        })
    finally:
        # Existing DoTasks mechanism makes the completed worker session visible locally.
        if hasattr(client, '_threads_to_sync'):
            client._threads_to_sync[message['thread_id']] = ''
        client.stop()


def claim_message(service, worker_id: str, host_id: str):
    # Called only when this exclusive worker has no local turn in flight.
    service.call('recover', {'worker_id': worker_id})
    return service.call('claim', {'worker_id': worker_id, 'host_id': host_id})


def run():
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_args: stop.set())
    service = MobileService()
    worker_id = 'mobile-' + socket.gethostname()
    while not stop.is_set():
        try:
            # No local turn can be executing here: this worker holds its OS lock
            # and executes each message synchronously. Recover lost claim replies.
            message = claim_message(service, worker_id, socket.gethostname())
            if message:
                client = CodexAppServerClient(_default_data_home(), Path(__file__).resolve().parents[1])
                execute_message(service, message, client, stop)
                continue
        except Exception:
            # Under our exclusive worker lock, no local turn remains in flight here.
            # A claim response may have been lost after the server reserved it.
            try:
                service.call('recover', {'worker_id': worker_id})
            except Exception:
                pass
            print('Mobile worker paused this cycle; check service connectivity and pending message state.', flush=True)
        stop.wait(5)


def main():
    directory = _default_data_home()
    directory.mkdir(parents=True, exist_ok=True)
    with (directory / 'mobile-worker.lock').open('a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        run()


if __name__ == '__main__':
    main()
