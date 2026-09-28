"""Observe execution-provider usage without deciding how tasks are executed."""
from __future__ import annotations

FIELDS = {
    'token_used': 'totalTokens', 'input_tokens': 'inputTokens',
    'cached_input_tokens': 'cachedInputTokens', 'output_tokens': 'outputTokens',
    'reasoning_output_tokens': 'reasoningOutputTokens',
}


class TurnUsage:
    def __init__(self, baseline=None):
        self.baseline = baseline
        self.session_baseline = None
        self.previous = None
        self.current = baseline

    def observe(self, payload):
        total, last = payload.get('total') or {}, payload.get('last') or {}
        if any(type(total.get(name)) is not int or type(last.get(name)) is not int
               or not 0 <= last[name] <= total[name] for name in FIELDS.values()):
            raise ValueError('Invalid execution usage counters')
        current = {key: total[name] for key, name in FIELDS.items()}
        self.current = current
        if self.session_baseline is None:
            if self.baseline is not None and current == self.baseline:
                return None
            before = {key: total[name] - last[name] for key, name in FIELDS.items()}
            if self.baseline is not None and before == self.baseline:
                self.session_baseline = self.baseline
            elif not any(before.values()):
                self.session_baseline = before
            else:
                raise ValueError('Execution usage baseline is unavailable')
        result = {key: current[key] - self.session_baseline[key] for key in FIELDS}
        if any(value < (self.previous or {}).get(key, 0) for key, value in result.items()):
            raise ValueError('Execution usage counters moved backwards')
        if result == self.previous:
            return None
        self.previous = result
        return result


class UsageOutbox:
    """Keep tracking receipts across disconnects; execution does not depend on upload."""
    def __init__(self, home, service):
        from pathlib import Path
        import threading
        import hashlib
        import json
        client = getattr(service, "client", None)
        identity = [str(getattr(client, key, "")) for key in ("cloud_url", "agent_id", "team_id", "task_id", "revision")] if client is not None else [str(Path(home).resolve())]
        scope = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        self.root = Path(home) / 'usage-outbox' / scope
        self.service = service
        self.lock = threading.Lock()
        self.upload_lock = threading.Lock()

    def record(self, run_id, thread_id, turn_id, usage):
        import hashlib
        import json
        import os
        with self.lock:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            key = hashlib.sha256((run_id + '\0' + thread_id + '\0' + turn_id).encode()).hexdigest()
            path = self.root / (key + '.json')
            if usage is None and path.exists() and json.loads(path.read_text()).get('usage') is not None:
                return
            temporary = path.with_suffix('.tmp')
            payload = {'run_id': run_id, 'thread_id': thread_id, 'turn_id': turn_id, 'usage': usage}
            with temporary.open('w', encoding='utf-8') as stream:
                os.chmod(temporary, 0o600)
                json.dump(payload, stream)
            os.replace(temporary, path)

    def flush_async(self):
        import threading
        if self.upload_lock.acquire(blocking=False):
            threading.Thread(target=self._upload, daemon=True, name="dotasks-usage-upload").start()

    def flush(self):
        if self.upload_lock.acquire(blocking=False):
            self._upload()

    def _upload(self):
        import json
        import logging
        try:
            # Upload never holds the receipt lock across network I/O. Failed
            # receipts stay durable for the next notification/reconnect/start.
            for path in list(self.root.glob('*.json'))[:100]:
                for _ in range(2):
                    with self.lock:
                        if not path.exists():
                            break
                        content = path.read_text()
                    try:
                        self.service.record_execution_usage(**json.loads(content))
                    except Exception as exc:
                        logging.getLogger(__name__).warning('Usage upload pending: %s', type(exc).__name__)
                        break
                    with self.lock:
                        if path.exists() and path.read_text() == content:
                            path.unlink()
                            break
        finally:
            self.upload_lock.release()
