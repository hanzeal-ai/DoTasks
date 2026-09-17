import unittest
from unittest.mock import patch

from taskboard.web_auth import WebSessions, session_cookie, session_token


class WebAuthTest(unittest.TestCase):
    def test_session_expiry_revoke_and_restart(self):
        sessions = WebSessions()
        with patch("taskboard.web_auth.time.monotonic", return_value=10):
            token = sessions.create()
            self.assertTrue(sessions.valid(token))
            self.assertFalse(WebSessions().valid(token))
        with patch("taskboard.web_auth.time.monotonic", return_value=50000):
            self.assertFalse(sessions.valid(token))
        token = sessions.create()
        sessions.revoke(token)
        self.assertFalse(sessions.valid(token))

    def test_cookie_security_and_parsing(self):
        cookie = session_cookie("example", secure=True)
        for attribute in ("Secure", "HttpOnly", "SameSite=Strict", "Path=/", "Max-Age=43200"):
            self.assertIn(attribute, cookie)
        self.assertEqual("example", session_token(cookie))
        self.assertEqual("", session_token(""))
        self.assertIn("dotasks_session=signed-out", session_cookie("signed-out", secure=True))

    def test_rate_limit_recovers_and_session_storage_is_bounded(self):
        sessions = WebSessions()
        with patch("taskboard.web_auth.time.monotonic", return_value=1):
            for _ in range(20):
                self.assertTrue(sessions.allow_login())
            self.assertFalse(sessions.allow_login())
        with patch("taskboard.web_auth.time.monotonic", return_value=62):
            self.assertTrue(sessions.allow_login())
        first = sessions.create()
        for _ in range(1000):
            latest = sessions.create()
        self.assertFalse(sessions.valid(first))
        self.assertTrue(sessions.valid(latest))
        self.assertEqual(1000, len(sessions._sessions))
