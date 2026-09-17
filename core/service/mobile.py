from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any


class MobileConversationMixin:
    """Personal, authenticated Agent channel; task/thread ownership stays here."""

    def mobile_conversations(self) -> list[dict[str, Any]]:
        with self.db.connection() as connection:
            rows = connection.execute(
                """SELECT DISTINCT t.id AS task_id, t.title, t.project, t.status,
                          n.thread_id, n.role
                   FROM tasks t JOIN native_dispatches n
                     ON n.entity_type='task' AND n.entity_id=t.id
                   WHERE trim(n.thread_id)!='' AND n.codex_project_id='codex-cli-app-server' AND trim(n.host_id)!=''
                   ORDER BY t.updated_at DESC, t.id, n.thread_id"""
            ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _mobile_task_locked(connection: Any, task_id: str) -> bool:
        # First release serializes mobile execution with the scheduler globally.
        # Threads may be reused across related tasks, even across project mappings.
        return connection.execute(
            "SELECT 1 FROM mobile_messages WHERE status IN ('starting','running','uncertain') LIMIT 1"
        ).fetchone() is not None

    @staticmethod
    def _require_mobile_thread(connection: Any, task_id: str, thread_id: str) -> None:
        if not connection.execute(
            """SELECT 1 FROM native_dispatches WHERE entity_type='task'
               AND entity_id=? AND thread_id=? AND codex_project_id='codex-cli-app-server'
               AND trim(host_id)!='' LIMIT 1""", (task_id, thread_id),
        ).fetchone():
            raise ValueError('Conversation does not belong to this task or its execution agent')

    def enqueue_mobile_message(self, message_id: str, task_id: str,
                               thread_id: str, body: str) -> dict[str, Any]:
        if not isinstance(message_id, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{16,128}', message_id):
            raise ValueError('Invalid message id')
        if not isinstance(body, str) or not 1 <= len(body.strip()) <= 8000:
            raise ValueError('Message must contain 1 to 8000 characters')
        body = body.strip()
        with self.db.transaction() as connection:
            self._require_mobile_thread(connection, task_id, thread_id)
            previous = connection.execute('SELECT * FROM mobile_messages WHERE id=?', (message_id,)).fetchone()
            if previous:
                if (previous['task_id'], previous['thread_id'], previous['body']) != (task_id, thread_id, body):
                    raise ValueError('Message id already belongs to a different request')
                return dict(previous)
            task = connection.execute('SELECT token_budget,effective_token_used FROM tasks WHERE id=?', (task_id,)).fetchone()
            if task['token_budget'] > 0 and task['effective_token_used'] >= task['token_budget']:
                return {'id': message_id, 'status': 'budget_exceeded'}
            if connection.execute(
                "SELECT COUNT(*) FROM mobile_messages WHERE task_id=? AND status='queued'", (task_id,),
            ).fetchone()[0] >= 20:
                raise ValueError('Task already has 20 queued messages')
            connection.execute(
                'INSERT INTO mobile_messages(id, task_id, thread_id, body) VALUES(?,?,?,?)',
                (message_id, task_id, thread_id, body),
            )
            self._event(connection, 'task', task_id, 'mobile_message_queued',
                        {'message_id': message_id, 'thread_id': thread_id})
        return {'id': message_id, 'status': 'queued', 'task_id': task_id, 'thread_id': thread_id}

    def claim_mobile_message(self, worker_id: str, host_id: str) -> dict[str, Any] | None:
        if not isinstance(worker_id, str) or not 1 <= len(worker_id) <= 200:
            raise ValueError('worker_id required')
        with self.db.transaction() as connection:
            enabled = connection.execute("SELECT value FROM system_settings WHERE key='dispatcher_enabled'").fetchone()
            if not enabled or enabled['value'] != '1':
                return None
            for table, states in (
                ('native_dispatches', "'claimed','pending_thread','bound'"),
                ('task_runs', "'awaiting_thread','running'"),
                ('requirement_decomposition_runs', "'running'"),
            ):
                if connection.execute(f'SELECT 1 FROM {table} WHERE status IN ({states}) LIMIT 1').fetchone():
                    return None
            rows = connection.execute(
                """SELECT m.* FROM mobile_messages m JOIN tasks t ON t.id=m.task_id
                   WHERE m.status='queued' AND t.status NOT IN ('paused','cancelled','ready','rework','code_review')
                   ORDER BY m.created_at, m.rowid"""
            ).fetchall()
            for row in rows:
                if not connection.execute("SELECT 1 FROM native_dispatches WHERE entity_type='task' AND entity_id=? AND thread_id=? AND host_id=? AND codex_project_id='codex-cli-app-server'", (row['task_id'], row['thread_id'], host_id)).fetchone():
                    continue
                if self._mobile_task_locked(connection, row['task_id']):
                    continue
                self._require_mobile_thread(connection, row['task_id'], row['thread_id'])
                task = connection.execute('SELECT token_budget,effective_token_used FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
                if task['token_budget'] > 0 and task['effective_token_used'] >= task['token_budget']:
                    connection.execute("UPDATE mobile_messages SET status='failed',result='任务预算已耗尽，追加已暂停。' WHERE id=?", (row['id'],))
                    self._event(connection, 'task', row['task_id'], 'mobile_budget_exceeded',
                                {'thread_id': row['thread_id'], 'summary': '任务预算已耗尽，追加已暂停。'})
                    continue
                run = connection.execute(
                    '''SELECT r.* FROM task_runs r JOIN task_run_conversations c ON c.run_id=r.id
                       WHERE r.task_id=? AND c.thread_id=? ORDER BY r.created_at DESC,r.id DESC LIMIT 1''',
                    (row['task_id'], row['thread_id']),
                ).fetchone()
                if not run:
                    connection.execute("UPDATE mobile_messages SET status='failed',result='原会话缺少任务运行记录，无法核算预算。' WHERE id=?", (row['id'],))
                    self._event(connection, 'task', row['task_id'], 'mobile_message_failed',
                                {'thread_id': row['thread_id'], 'summary': '原会话缺少任务运行记录，无法核算预算。'})
                    continue
                baseline = {key: run[key] for key in ('token_used', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')}
                connection.execute(
                    "UPDATE mobile_messages SET status='starting', worker_id=?,run_id=?,usage_baseline=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                    (worker_id, run['id'], json.dumps(baseline), row['id']),
                )
                return {**dict(row), 'status': 'starting', 'worker_id': worker_id}
        return None

    def prepare_mobile_turn(self, message_id: str, worker_id: str,
                            baseline: dict[str, int]) -> dict[str, bool]:
        keys = {'token_used', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'}
        if not isinstance(baseline, dict) or set(baseline) != keys or any(type(v) is not int or v < 0 for v in baseline.values()):
            raise ValueError('Invalid original thread usage baseline')
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM mobile_messages WHERE id=?', (message_id,)).fetchone()
            if not row or row['worker_id'] != worker_id or row['status'] != 'starting':
                raise ValueError('Message is not ready for this worker')
            previous = json.loads(row['thread_usage_baseline'])
            if previous and previous != baseline:
                raise ValueError('Original thread baseline cannot change')
            connection.execute('UPDATE mobile_messages SET thread_usage_baseline=? WHERE id=?', (json.dumps(baseline), message_id))
        return {'prepared': True}

    def record_mobile_usage(self, message_id: str, worker_id: str, turn_id: str,
                            usage: dict[str, int]) -> dict[str, Any]:
        keys = ('token_used', 'input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens')
        if not isinstance(usage, dict) or set(usage) != set(keys) or any(type(v) is not int or v < 0 for v in usage.values()):
            raise ValueError('Invalid cumulative mobile turn usage')
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM mobile_messages WHERE id=?', (message_id,)).fetchone()
            if not row or row['worker_id'] != worker_id or row['status'] != 'running' or not turn_id or row['turn_id'] != turn_id:
                raise ValueError('Usage does not belong to this running turn')
            baseline = json.loads(row['usage_baseline'])
            totals = {key: baseline[key] + usage[key] for key in keys}
            self._record_run_token_usage(connection, row['run_id'], totals['token_used'], totals)
            task = connection.execute('SELECT token_budget,effective_token_used FROM tasks WHERE id=?', (row['task_id'],)).fetchone()
            return {'exhausted': task['token_budget'] > 0 and task['effective_token_used'] >= task['token_budget']}

    def update_mobile_message(self, message_id: str, worker_id: str, status: str,
                              turn_id: str = '', result: str = '') -> dict[str, Any]:
        transitions = {'starting': {'running', 'failed', 'uncertain'}, 'running': {'completed', 'failed', 'uncertain'}}
        with self.db.transaction() as connection:
            row = connection.execute('SELECT * FROM mobile_messages WHERE id=?', (message_id,)).fetchone()
            if not row or row['worker_id'] != worker_id:
                raise ValueError('Message is not claimed by this worker')
            if row['status'] == status and row['turn_id'] == turn_id and row['result'] == result:
                return dict(row)
            if status not in transitions.get(row['status'], set()):
                raise ValueError('Invalid mobile message transition')
            if status == 'running' and not turn_id:
                raise ValueError('A running message needs a turn id')
            if row['turn_id'] and row['turn_id'] != turn_id:
                raise ValueError('Turn id changed')
            connection.execute(
                'UPDATE mobile_messages SET status=?, turn_id=?, result=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                (status, turn_id, result[:16000], message_id),
            )
            self._event(connection, 'task', row['task_id'], 'mobile_message_' + status,
                        {'message_id': message_id, 'thread_id': row['thread_id'], 'summary': result[:16000]})
            if status in {'completed', 'failed'}:
                self._request_schedule(connection, 'mobile_message_finished', 'task', row['task_id'])
        return {'id': message_id, 'status': status}

    def recover_mobile_messages(self, worker_id: str) -> dict[str, int]:
        # Called under the execution machine's exclusive worker lock, never by a timer.
        with self.db.transaction() as connection:
            rows = connection.execute("SELECT * FROM mobile_messages WHERE worker_id=? AND status IN ('starting','running')", (worker_id,)).fetchall()
            for row in rows:
                connection.execute("UPDATE mobile_messages SET status='uncertain', result='执行端重启，上一轮结果待核对。', updated_at=CURRENT_TIMESTAMP WHERE id=?", (row['id'],))
                self._event(connection, 'task', row['task_id'], 'mobile_message_uncertain',
                            {'message_id': row['id'], 'thread_id': row['thread_id'], 'summary': '执行端重启，上一轮结果待核对，请勿重复发送。'})
        return {'uncertain': len(rows)}

    def resolve_mobile_message(self, message_id: str, thread_id: str,
                               turn_id: str, evidence: str, final_usage: dict[str, int] | None = None) -> dict[str, str]:
        """Operator-only recovery after verifying the old turn is no longer running.

        Deliberately absent from mobile transport and autonomous MCP handlers.
        """
        if not isinstance(evidence, str) or not 10 <= len(evidence.strip()) <= 2000:
            raise ValueError('Record verification evidence before releasing the reservation')
        with self.db.transaction() as connection:
            enabled = connection.execute("SELECT value FROM system_settings WHERE key='dispatcher_enabled'").fetchone()
            if enabled and enabled['value'] == '1':
                raise ValueError('Pause the dispatcher before operator recovery')
            row = connection.execute('SELECT * FROM mobile_messages WHERE id=?', (message_id,)).fetchone()
            if not row or row['status'] != 'uncertain' or row['thread_id'] != thread_id or row['turn_id'] != turn_id:
                raise ValueError('Recovery must match the uncertain message and original turn')
            thread_baseline = json.loads(row['thread_usage_baseline'])
            if thread_baseline:
                if not isinstance(final_usage, dict) or set(final_usage) != set(thread_baseline) or any(type(v) is not int or v < thread_baseline[key] for key, v in final_usage.items()):
                    raise ValueError('Verify final original turn usage before releasing the reservation')
                run_baseline = json.loads(row['usage_baseline'])
                totals = {key: run_baseline[key] + final_usage[key] - thread_baseline[key] for key in thread_baseline}
                self._record_run_token_usage(connection, row['run_id'], totals['token_used'], totals)
            connection.execute(
                "UPDATE mobile_messages SET status='failed', result=?, updated_at=CURRENT_TIMESTAMP WHERE id=?",
                ('人工核对结束：' + evidence.strip(), message_id),
            )
            self._event(connection, 'task', row['task_id'], 'mobile_message_resolved',
                        {'message_id': message_id, 'thread_id': thread_id,
                         'summary': '人工核对已结束；原消息不会自动重放。', 'evidence': evidence.strip()})
        return {'id': message_id, 'status': 'failed'}

    def mobile_events(self, after: int | None = None, since: str | None = None) -> dict[str, Any]:
        if after is not None and (type(after) is not int or after < 0):
            raise ValueError('Invalid event cursor')
        with self.db.connection() as connection:
            if since is not None:
                if not isinstance(since, str) or datetime.fromisoformat(since.replace('Z', '+00:00')).tzinfo is None:
                    raise ValueError('since must be an ISO timestamp with timezone')
                if after is None:
                    after = connection.execute('SELECT COALESCE(MAX(id),0) FROM events WHERE created_at<datetime(?)', (since,)).fetchone()[0]
            if after is None:
                last = connection.execute('SELECT COALESCE(MAX(id),0) FROM events').fetchone()[0]
                return {'cursor': last, 'items': []}
            rows = connection.execute('SELECT * FROM events WHERE id>? ORDER BY id LIMIT 200', (after,)).fetchall()
            items = []
            for row in rows:
                payload = json.loads(row['payload'])
                task_id = row['entity_id'] if row['entity_type'] == 'task' else ''
                run_id = row['entity_id'] if row['entity_type'] == 'run' else payload.get('run_id', '')
                if run_id and not task_id:
                    run = connection.execute('SELECT task_id FROM task_runs WHERE id=?', (run_id,)).fetchone()
                    task_id = run['task_id'] if run else ''
                task = connection.execute('SELECT id,title,project FROM tasks WHERE id=?', (task_id,)).fetchone()
                if not task or row['event_type'] == 'token_usage_updated':
                    continue
                mapping = connection.execute('SELECT thread_id FROM task_run_conversations WHERE run_id=?', (run_id,)).fetchone()
                summary = str(payload.get('summary') or payload.get('delivery_summary') or payload.get('reason') or '')
                if row['event_type'] == 'delivery_submitted' and run_id:
                    delivery = connection.execute('SELECT delivery_summary,verification_result FROM task_runs WHERE id=? AND task_id=?', (run_id, task_id)).fetchone()
                    if delivery:
                        summary = '\n'.join(value for value in (delivery['delivery_summary'], delivery['verification_result']) if value)
                elif row['event_type'] == 'code_reviewed':
                    summary = '审查：' + str(payload.get('verdict', '')) + '；状态：' + str(payload.get('next_stage', ''))
                    reasons = payload.get('reasons', [])
                    if isinstance(reasons, list):
                        summary += '\n' + '\n'.join(str(reason) for reason in reasons)

                items.append({
                    'type': row['event_type'], **dict(task), 'task_id': task_id,
                    'event_id': row['id'], 'run_id': run_id,
                    'thread_id': payload.get('thread_id') or (mapping['thread_id'] if mapping else ''),
                    'summary': summary,
                })
            return {'cursor': rows[-1]['id'] if rows else after, 'items': items}
