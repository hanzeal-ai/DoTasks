"""Bounded team invalidations. Snapshots remain the authoritative, authorized read."""
import threading
import time


class TeamEvents:
    def __init__(self):
        self.condition = threading.Condition()
        self.revisions = {}
        self.slots = threading.BoundedSemaphore(64)

    def changed(self, team):
        with self.condition:
            self.revisions[team] = self.revisions.get(team, 0) + 1
            self.condition.notify_all()

    def wait(self, team, revision, timeout=10):
        with self.condition:
            self.condition.wait_for(lambda: self.revisions.get(team, 0) != revision, timeout)
            return self.revisions.get(team, 0)


def serve_team_events(handler, registry, team, actor):
    from taskboard.http_security import HTTPRequestError
    if not registry.events.slots.acquire(blocking=False):
        raise HTTPRequestError(503, '实时连接已达上限，请稍后重试')
    try:
        handler.connection.settimeout(15)
        handler.send_response(200)
        for key, value in {'Content-Type': 'text/event-stream; charset=utf-8',
                           'Cache-Control': 'no-store', 'Connection': 'close',
                           'X-Accel-Buffering': 'no'}.items():
            handler.send_header(key, value)
        handler.end_headers()
        handler.wfile.write(b'retry: 3000\n\n')
        handler.wfile.flush()
        revision = -1
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            latest = registry.events.wait(team, revision)
            # A long-lived stream must not outlive session or membership revocation.
            handler._require_authentication()
            registry.directory.role(team, actor)
            payload = b'event: board_changed\ndata: {}\n\n' if latest != revision else b': keepalive\n\n'
            handler.wfile.write(payload)
            handler.wfile.flush()
            revision = latest
    except (ValueError, OSError):
        pass
    finally:
        handler.close_connection = True
        registry.events.slots.release()
