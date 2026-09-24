"""Account credentials and browser sessions; tenant IDs never come from request paths."""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
from pathlib import Path
import re
import secrets
import sqlite3
import time
import uuid

from taskboard.http_security import HTTPRequestError
from http import HTTPStatus


class AccountStore:
    def __init__(self, home: Path):
        self.path = home / "accounts" / "accounts.db"
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        with closing(self.connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS accounts (
                    id TEXT PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                    salt BLOB NOT NULL, password_hash BLOB NOT NULL,
                    device_id TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                    provisioned INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY, account_id TEXT NOT NULL,
                    expires REAL NOT NULL);
            """)
        self.path.chmod(0o600)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        return db

    @staticmethod
    def digest(value: str) -> str:
        return hashlib.sha256(value.encode()).hexdigest()

    @staticmethod
    def password_hash(password: str, salt: bytes) -> bytes:
        return hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)

    @staticmethod
    def credentials(username, password) -> tuple[str, str]:
        if not isinstance(username, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{2,63}", username):
            raise ValueError("账号需为 3–64 位字母、数字、点、下划线或短横线")
        if not isinstance(password, str) or not 12 <= len(password) <= 128:
            raise ValueError("密码需为 12–128 个字符")
        return username.lower(), password

    def initialize(self, username, password, device_id, device_token) -> tuple[dict, str]:
        username, password = self.credentials(username, password)
        if not isinstance(device_id, str) or not re.fullmatch(r"[0-9a-f]{32}", device_id):
            raise ValueError("Invalid device ID")
        if not isinstance(device_token, str) or not re.fullmatch(r"[A-Za-z0-9_-]{43,128}", device_token):
            raise ValueError("Invalid device token")
        # The transaction makes simultaneous signup/retries for one username atomic.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            if row:
                if not hmac.compare_digest(self.password_hash(password, row['salt']), row['password_hash']):
                    raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, "账号不可注册或凭据不正确")
                if row['device_id'] != device_id:
                    raise HTTPRequestError(HTTPStatus.CONFLICT, "该账号已绑定其他安装，请使用原设备；暂不支持自动更换设备")
                if not hmac.compare_digest(row['token_hash'], self.digest(device_token)):
                    raise HTTPRequestError(HTTPStatus.UNAUTHORIZED, "本机初始化凭证不匹配")
                account_id = row['id']
            else:
                account_id = uuid.uuid4().hex
            # The CLI generates a 256-bit secret before its first request. Binding
            # its hash makes retries stable without retaining a server-side secret.
            token = device_token
            if not row:
                salt = secrets.token_bytes(16)
                db.execute("INSERT INTO accounts(id,username,salt,password_hash,device_id,token_hash) VALUES(?,?,?,?,?,?)",
                           (account_id, username, salt, self.password_hash(password, salt), device_id, self.digest(token)))
            row = db.execute("SELECT id,username,provisioned FROM accounts WHERE id=?", (account_id,)).fetchone()
            return dict(row), token

    def authenticate(self, username, password) -> dict | None:
        try:
            username, password = self.credentials(username, password)
        except ValueError:
            return None
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
        actual = self.password_hash(password, bytes(row['salt']) if row else b'\0' * 16)
        if row and hmac.compare_digest(actual, row['password_hash']):
            return {"id": row['id'], "username": row['username']}
        return None

    def agent(self, token: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT id,username FROM accounts WHERE token_hash=?", (self.digest(token),)).fetchone()
        return dict(row) if row else None

    def session(self, token: str) -> dict | None:
        with closing(self.connect()) as db:
            row = db.execute("SELECT a.id,a.username FROM accounts a JOIN sessions s ON a.id=s.account_id WHERE s.token_hash=? AND s.expires>?",
                             (self.digest(token), time.time())).fetchone()
        return dict(row) if row else None

    def create_session(self, account_id: str) -> str:
        token = secrets.token_urlsafe(32)
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE expires<=?", (time.time(),))
            db.execute("INSERT INTO sessions VALUES(?,?,?)", (self.digest(token), account_id, time.time() + 12 * 3600))
        return token

    def revoke_session(self, token: str) -> None:
        with closing(self.connect()) as db, db:
            db.execute("DELETE FROM sessions WHERE token_hash=?", (self.digest(token),))

    def provision(self, account_id: str, service) -> None:
        # Only first initialization enables dispatch. Retrying init must not undo a pause.
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT provisioned FROM accounts WHERE id=?", (account_id,)).fetchone()
            if not row['provisioned']:
                service.set_dispatcher_enabled(True)
                db.execute("UPDATE accounts SET provisioned=1 WHERE id=?", (account_id,))
