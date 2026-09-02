from __future__ import annotations

import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import Any, Iterator


SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS relay_commands (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  headers TEXT NOT NULL DEFAULT '{}',
  body BLOB NOT NULL DEFAULT X'',
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','claimed','completed','cancelled')),
  response_status INTEGER,
  response_headers TEXT NOT NULL DEFAULT '{}',
  response_body BLOB NOT NULL DEFAULT X'',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  claimed_at TEXT,
  completed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_relay_commands_claim
  ON relay_commands(agent_id, status, created_at);

CREATE TABLE IF NOT EXISTS relay_agents (
  agent_id TEXT PRIMARY KEY,
  last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  board_hash TEXT NOT NULL DEFAULT '',
  metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS relay_state (
  id INTEGER PRIMARY KEY CHECK(id=1),
  event_id INTEGER NOT NULL DEFAULT 0
);

INSERT OR IGNORE INTO relay_state(id) VALUES(1);

CREATE TABLE IF NOT EXISTS relay_vault_files (
  agent_id TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(agent_id, path)
);
"""


class RelayStore:
    def __init__(self, data_home: str | Path):
        self.data_home = Path(data_home).expanduser().resolve()
        self.path = self.data_home / "data" / "relay.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vault_root = self.data_home / "obsidian-vault"
        with self.connection() as connection:
            connection.executescript(SCHEMA)
            connection.commit()

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def enqueue(
        self,
        agent_id: str,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes,
    ) -> str:
        command_id = uuid.uuid4().hex
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO relay_commands(id, agent_id, method, path, headers, body)
                   VALUES(?, ?, ?, ?, ?, ?)""",
                (
                    command_id,
                    agent_id,
                    method,
                    path,
                    json.dumps(headers, ensure_ascii=False),
                    body,
                ),
            )
        return command_id

    def claim(self, agent_id: str) -> dict[str, Any] | None:
        with self.transaction() as connection:
            connection.execute(
                """UPDATE relay_commands SET status='queued', claimed_at=NULL
                   WHERE agent_id=? AND status='claimed'
                     AND claimed_at < datetime('now', '-90 seconds')""",
                (agent_id,),
            )
            row = connection.execute(
                """SELECT * FROM relay_commands
                   WHERE agent_id=? AND status='queued'
                   ORDER BY created_at, id LIMIT 1""",
                (agent_id,),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE relay_commands SET status='claimed', claimed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='queued'""",
                (row["id"],),
            )
            if updated.rowcount != 1:
                return None
        return {
            "id": row["id"],
            "method": row["method"],
            "path": row["path"],
            "headers": json.loads(row["headers"] or "{}"),
            "body": bytes(row["body"] or b""),
        }

    def cancel_queued(self, command_id: str) -> bool:
        with self.transaction() as connection:
            updated = connection.execute(
                "UPDATE relay_commands SET status='cancelled' "
                "WHERE id=? AND status='queued'",
                (command_id,),
            )
        return updated.rowcount == 1

    def complete(
        self,
        agent_id: str,
        command_id: str,
        status: int,
        headers: dict[str, str],
        body: bytes,
    ) -> None:
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT status, agent_id FROM relay_commands WHERE id=?",
                (command_id,),
            ).fetchone()
            if not row or row["agent_id"] != agent_id:
                raise KeyError(f"Relay command not found: {command_id}")
            if row["status"] == "completed":
                return
            updated = connection.execute(
                """UPDATE relay_commands
                   SET status='completed', response_status=?, response_headers=?,
                       response_body=?, completed_at=CURRENT_TIMESTAMP
                   WHERE id=? AND status='claimed'""",
                (
                    int(status),
                    json.dumps(headers, ensure_ascii=False),
                    body,
                    command_id,
                ),
            )
            if updated.rowcount != 1:
                raise ValueError("Relay command is not claimed")

    def wait_result(
        self, command_id: str, timeout_seconds: float
    ) -> dict[str, Any] | None:
        deadline = time.monotonic() + max(0.1, float(timeout_seconds))
        while time.monotonic() < deadline:
            with self.connection() as connection:
                row = connection.execute(
                    "SELECT * FROM relay_commands WHERE id=?", (command_id,)
                ).fetchone()
            if row and row["status"] == "completed":
                return {
                    "status": int(row["response_status"]),
                    "headers": json.loads(row["response_headers"] or "{}"),
                    "body": bytes(row["response_body"] or b""),
                }
            time.sleep(0.05)
        return None

    def touch_agent(
        self, agent_id: str, board_hash: str, metadata: dict[str, Any]
    ) -> int:
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT board_hash FROM relay_agents WHERE agent_id=?", (agent_id,)
            ).fetchone()
            changed = bool(previous and board_hash and previous["board_hash"] != board_hash)
            connection.execute(
                """INSERT INTO relay_agents(agent_id, board_hash, metadata)
                   VALUES(?, ?, ?)
                   ON CONFLICT(agent_id) DO UPDATE SET
                     last_seen_at=CURRENT_TIMESTAMP,
                     board_hash=excluded.board_hash,
                     metadata=excluded.metadata""",
                (agent_id, board_hash, json.dumps(metadata, ensure_ascii=False)),
            )
            if changed:
                connection.execute(
                    "UPDATE relay_state SET event_id=event_id+1 WHERE id=1"
                )
            return int(
                connection.execute(
                    "SELECT event_id FROM relay_state WHERE id=1"
                ).fetchone()["event_id"]
            )

    def latest_event_id(self) -> int:
        with self.connection() as connection:
            row = connection.execute(
                "SELECT event_id FROM relay_state WHERE id=1"
            ).fetchone()
        return int(row["event_id"] if row else 0)

    def agent_status(self, agent_id: str) -> dict[str, Any]:
        with self.connection() as connection:
            row = connection.execute(
                """SELECT *,
                          last_seen_at >= datetime('now', '-45 seconds') AS online
                   FROM relay_agents WHERE agent_id=?""",
                (agent_id,),
            ).fetchone()
        if not row:
            return {"agent_id": agent_id, "online": False, "last_seen_at": ""}
        return {
            "agent_id": agent_id,
            "online": bool(row["online"]),
            "last_seen_at": row["last_seen_at"],
            "metadata": json.loads(row["metadata"] or "{}"),
        }

    @staticmethod
    def normalize_vault_path(value: str) -> str:
        path = PurePosixPath(str(value or ""))
        if not value or path.is_absolute() or ".." in path.parts or path.suffix != ".md":
            raise ValueError("Vault path must be a project-relative Markdown file")
        return str(path)

    def write_vault_file(
        self, agent_id: str, relative_path: str, sha256: str, content: bytes
    ) -> None:
        relative = self.normalize_vault_path(relative_path)
        actual_sha256 = hashlib.sha256(content).hexdigest()
        if sha256 != actual_sha256:
            raise ValueError("Vault content SHA-256 does not match")
        root = (self.vault_root / agent_id).resolve()
        target = (root / relative).resolve()
        if root not in target.parents:
            raise ValueError("Vault path escapes the agent vault")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO relay_vault_files(agent_id, path, sha256)
                   VALUES(?, ?, ?)
                   ON CONFLICT(agent_id, path) DO UPDATE SET
                     sha256=excluded.sha256, updated_at=CURRENT_TIMESTAMP""",
                (agent_id, relative, actual_sha256),
            )

    def apply_vault_manifest(self, agent_id: str, paths: list[str]) -> None:
        normalized = {self.normalize_vault_path(path) for path in paths}
        with self.transaction() as connection:
            existing = {
                row["path"]
                for row in connection.execute(
                    "SELECT path FROM relay_vault_files WHERE agent_id=?", (agent_id,)
                )
            }
            for relative in sorted(existing - normalized):
                target = (self.vault_root / agent_id / relative).resolve()
                root = (self.vault_root / agent_id).resolve()
                if root in target.parents and target.is_file():
                    target.unlink()
                connection.execute(
                    "DELETE FROM relay_vault_files WHERE agent_id=? AND path=?",
                    (agent_id, relative),
                )
