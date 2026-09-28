"""Short-lived browser sessions for the configured cloud operator account."""
from __future__ import annotations

import hashlib
import secrets
import threading
import time
from collections import deque
from http.cookies import CookieError, SimpleCookie

SESSION_COOKIE = "dotasks_session"
SESSION_TTL = 12 * 60 * 60


class WebSessions:
    def __init__(self) -> None:
        self._sessions: dict[str, float] = {}
        self._attempts: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def allow_login(self, source: str = "local", username: str = "") -> bool:
        # Never trust a caller-supplied forwarded IP. Separate credentials behind
        # the same proxy, with a larger source cap to bound username spraying.
        source_key = "source:" + self._key(str(source))
        account_key = "login:" + self._key(str(source) + "\0" + str(username).strip().lower()[:128])
        with self._lock:
            now = time.monotonic()
            for key in list(self._attempts):
                attempts = self._attempts[key]
                while attempts and attempts[0] <= now - 60:
                    attempts.popleft()
                if not attempts:
                    del self._attempts[key]
            keys = ((account_key, 20), (source_key, 200))
            if any(len(self._attempts.get(key, ())) >= limit for key, limit in keys):
                return False
            if len(self._attempts) + sum(key not in self._attempts for key, _ in keys) > 10000:
                return False
            for key, _ in keys:
                self._attempts.setdefault(key, deque()).append(now)
            return True

    def create(self) -> str:
        with self._lock:
            now = time.monotonic()
            self._sessions = {key: expires for key, expires in self._sessions.items() if expires > now}
            if len(self._sessions) >= 1000:
                del self._sessions[next(iter(self._sessions))]
            token = secrets.token_urlsafe(32)
            self._sessions[self._key(token)] = now + SESSION_TTL
            return token

    def valid(self, token: str) -> bool:
        if not token:
            return False
        with self._lock:
            key = self._key(token)
            if self._sessions.get(key, 0) > time.monotonic():
                return True
            self._sessions.pop(key, None)
            return False

    def revoke(self, token: str) -> None:
        with self._lock:
            self._sessions.pop(self._key(token), None)


def has_session_cookie(cookie: str) -> bool:
    try:
        return SESSION_COOKIE in SimpleCookie(cookie)
    except CookieError:
        return False


def session_token(cookie: str) -> str:
    try:
        parsed = SimpleCookie(cookie)
        return parsed[SESSION_COOKIE].value if SESSION_COOKIE in parsed else ""
    except CookieError:
        return ""


def session_cookie(token: str, *, secure: bool) -> str:
    cookie = SimpleCookie()
    cookie[SESSION_COOKIE] = token
    value = cookie[SESSION_COOKIE]
    value["path"] = "/"
    value["httponly"] = True
    value["samesite"] = "Strict"
    value["max-age"] = SESSION_TTL if token != "signed-out" else 365 * 24 * 60 * 60
    if secure:
        value["secure"] = True
    return value.OutputString()
