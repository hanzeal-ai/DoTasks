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
        self._attempts: deque[float] = deque()
        self._lock = threading.Lock()

    @staticmethod
    def _key(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def allow_login(self) -> bool:
        with self._lock:
            now = time.monotonic()
            while self._attempts and self._attempts[0] <= now - 60:
                self._attempts.popleft()
            if len(self._attempts) >= 20:
                return False
            self._attempts.append(now)
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
