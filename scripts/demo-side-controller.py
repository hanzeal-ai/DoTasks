#!/usr/bin/env python3
"""Test an existing, user-opened ephemeral side chat as a creation controller.

Does not open or close desktop UI. An output file journals the one allowed send;
rerun with the same file to inspect the result without resending.
"""
import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
import uuid
import fcntl


def save(path, record):
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(record, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def persistence(home, catalog, tid):
    with catalog.connection() as db:
        count = db.execute('SELECT count(*) FROM threads WHERE id=?', (tid,)).fetchone()[0]
    index = home / 'session_index.jsonl'
    desktop_state = home / '.codex-global-state.json'
    desktop = json.loads(desktop_state.read_text()) if desktop_state.is_file() else {}
    bindings = desktop.get('electron-persisted-atom-state', {}).get('client-thread-bindings-v1', {})
    return {'database_rows': count,
            'rollout_files': [str(p) for root in ('sessions', 'archived_sessions')
                              for p in (home / root).rglob('*' + tid + '*')],
            'session_index_contains_id': index.is_file() and tid in index.read_text(),
            'desktop_binding_count': sum(value == tid for value in bindings.values())}


def verify_creation(record, valid_id):
    calls = [i for i in record.get('tool_calls', []) if i.get('server') == 'codex_app'
             and i.get('tool') == 'create_thread']
    if record.get('turn_status') in ('completed', 'failed', 'interrupted'):
        record['status'] = 'finished_without_verified_child'
        if len(calls) == 1 and calls[0].get('status') == 'completed' and not calls[0].get('error'):
            expected = {'target': {'type': 'projectless'}, 'title': 'DoTasks Demo · 临时侧边创建验证',
                        'prompt': 'Reply only SIDE_CONTROLLER_CHILD_OK. Do not use tools or change files.'}
            if calls[0].get('arguments') == expected:
                for item in (calls[0].get('result') or {}).get('content', []):
                    if item.get('type') != 'text':
                        continue
                    try:
                        value = json.loads(item['text'])
                        child = valid_id(value.get('threadId'))
                    except (ValueError, AttributeError):
                        continue
                    record.update(status='completed', child_id=child)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--carryon', required=True, type=Path)
    parser.add_argument('--parent-id', required=True)
    parser.add_argument('--side-id', required=True)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--create-child', action='store_true')
    parser.add_argument('--after-close', action='store_true', help='Read storage after user closes side chat')
    args = parser.parse_args()
    sys.path.insert(0, str(args.carryon.resolve()))
    from carryon.ipc import DesktopIPC
    from carryon.catalog import Catalog, valid_id
    from carryon.operations import turns
    from carryon.thread_status import idle_snapshot
    valid_id(args.parent_id)
    valid_id(args.side_id)
    if args.after_close and args.create_child:
        parser.error('after-close cannot create')
    home = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
    catalog = Catalog(home)
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.with_suffix(output.suffix + '.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = json.loads(output.read_text()) if output.exists() else {
            'parent_id': args.parent_id, 'side_id': args.side_id, 'status': 'not_sent'}
        if (record['parent_id'], record['side_id']) != (args.parent_id, args.side_id):
            raise ValueError('Journal belongs to another side chat')
        ipc = DesktopIPC(home / 'ipc/ipc.sock')
        try:
            if args.after_close:
                record['after_close_storage'] = persistence(home, catalog, args.side_id)
                ipc.connect()
                try:
                    record['after_close_owner'] = ipc.owner(args.side_id, timeout_ms=1500)
                except Exception as exc:
                    record['after_close_owner'] = str(exc)
            else:
                ipc.connect()
                owner, state = ipc.snapshot(args.side_id)
                if not (state.get('sideConversation') is True and state.get('ephemeral') is True
                        and state.get('forkedFromId') == args.parent_id):
                    raise ValueError('Not a verified ephemeral side chat of the specified parent')
                record['side_properties'] = {k: state.get(k) for k in ('id', 'sideConversation', 'ephemeral', 'forkedFromId')}
                record['while_open_storage'] = persistence(home, catalog, args.side_id)
                if args.create_child and record['status'] == 'not_sent':
                    idle_snapshot(state)
                    prompt = ('用户明确授权在此临时侧边聊天中做一次本地会话创建实验。'
                              '如果当前会话允许，请只调用一次 codex_app.create_thread：'
                              'target={"type":"projectless"}，title="DoTasks Demo · 临时侧边创建验证"，'
                              'prompt="Reply only SIDE_CONTROLLER_CHILD_OK. Do not use tools or change files."。'
                              '这不是创建子代理。若工具或当前会话规则不允许，请报告限制，不绕过，不使用命令行替代。')
                    def guarded(write):
                        idle_snapshot(ipc.current(args.side_id) or state)
                        record['status'] = 'sending_outcome_unknown'
                        save(output, record)
                        write()
                    turn = ipc.start(args.side_id, prompt, owner, str(uuid.uuid4()), guarded)
                    record.update(status='accepted', turn_id=turn['id'])
                    save(output, record)
                if record.get('turn_id'):
                    current = ipc.current(args.side_id) or state
                    turn = next((t for t in turns(current) if t.get('turnId') == record['turn_id']), None)
                    record['runtime'] = current.get('threadRuntimeStatus')
                    record['pending_request_count'] = len(current.get('requests') or [])
                    if turn:
                        record['turn_status'] = turn.get('status')
                        record['tool_calls'] = [i for i in turn.get('items', []) if i.get('type') == 'mcpToolCall']
                        record['final_messages'] = [i.get('text') for i in turn.get('items', [])
                                                    if i.get('type') == 'agentMessage' and i.get('phase') == 'final_answer']
        except Exception as exc:
            record['probe_error'] = str(exc)
        finally:
            ipc.close()
            verify_creation(record, valid_id)
            save(output, record)
        print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
