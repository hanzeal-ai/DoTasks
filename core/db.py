from __future__ import annotations

import json
import fcntl
import posixpath
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .workflow import (
    ACTIVE_RUN_STATUSES,
    CONVERSATION_ROLES,
    RUN_STATUSES,
    RUN_TYPES,
    TASK_STATUSES,
    sql_values,
)


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS id_counters (
  prefix TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS requirements (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  original_content TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'conversation',
  source_reference TEXT,
  project TEXT,
  status TEXT NOT NULL DEFAULT 'inbox',
  priority TEXT NOT NULL DEFAULT 'P2',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  requirement_id TEXT REFERENCES requirements(id),
  title TEXT NOT NULL,
  type TEXT NOT NULL DEFAULT 'feature',
  project TEXT,
  modules TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'draft',
  status_started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  priority TEXT NOT NULL DEFAULT 'P2',
  goal TEXT NOT NULL DEFAULT '',
  scope TEXT NOT NULL DEFAULT '[]',
  out_of_scope TEXT NOT NULL DEFAULT '[]',
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  source_thread_id TEXT,
  codex_thread_id TEXT,
  assigned_to TEXT,
  token_budget INTEGER NOT NULL DEFAULT 60000,
  token_used INTEGER NOT NULL DEFAULT 0,
  effective_token_used INTEGER NOT NULL DEFAULT 0,
  context_version INTEGER NOT NULL DEFAULT 1,
  active_run_id TEXT,
  primary_run_id TEXT,
  delivery_summary TEXT NOT NULL DEFAULT '',
  verification_result TEXT NOT NULL DEFAULT '',
  location_context TEXT NOT NULL DEFAULT '{}',
  acceptance_plan TEXT NOT NULL DEFAULT '[]',
  dependency_analysis TEXT NOT NULL DEFAULT '{}',
  implementation_contract TEXT NOT NULL DEFAULT '{}',
  review_contract TEXT NOT NULL DEFAULT '{}',
  parent_acceptance_task_id TEXT REFERENCES tasks(id),
  workflow_version INTEGER NOT NULL DEFAULT 2,
  paused_from_status TEXT,
  last_failure_reason TEXT NOT NULL DEFAULT '',
  last_failure_at TEXT,
  review_failed_at TEXT,
  last_review_reasons TEXT NOT NULL DEFAULT '[]',
  last_failed_criteria TEXT NOT NULL DEFAULT '[]',
  review_rework_count INTEGER NOT NULL DEFAULT 0,
  auto_dispatch INTEGER NOT NULL DEFAULT 1,
  retry_required INTEGER NOT NULL DEFAULT 0,
  retry_run_type TEXT,
  blocked_from_status TEXT,
  review_interrupt_count INTEGER NOT NULL DEFAULT 0,
  review_retry_after TEXT,
  dispatch_failure_count INTEGER NOT NULL DEFAULT 0,
  dispatch_retry_after TEXT,
  last_dispatch_error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_runs (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  parent_run_id TEXT REFERENCES task_runs(id),
  delivery_run_id TEXT REFERENCES task_runs(id),
  run_type TEXT NOT NULL,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'awaiting_thread',
  claimed_by TEXT,
  lease_token TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  context_snapshot TEXT NOT NULL DEFAULT '{}',
  delivery_summary TEXT NOT NULL DEFAULT '',
  verification_result TEXT NOT NULL DEFAULT '',
  changed_locations TEXT NOT NULL DEFAULT '[]',
  acceptance_evidence TEXT NOT NULL DEFAULT '[]',
  artifact_snapshot TEXT NOT NULL DEFAULT '{}',
  token_used INTEGER NOT NULL DEFAULT 0,
  effective_token_used INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cached_input_tokens INTEGER NOT NULL DEFAULT 0,
  output_tokens INTEGER NOT NULL DEFAULT 0,
  reasoning_output_tokens INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  started_at TEXT,
  stage_completed_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  UNIQUE(task_id, run_type, attempt)
);

CREATE TABLE IF NOT EXISTS token_usage_events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
  source_total INTEGER NOT NULL CHECK(source_total > 0),
  token_delta INTEGER NOT NULL CHECK(token_delta > 0),
  effective_token_delta INTEGER NOT NULL DEFAULT 0,
  input_delta INTEGER NOT NULL DEFAULT 0,
  cached_input_delta INTEGER NOT NULL DEFAULT 0,
  output_delta INTEGER NOT NULL DEFAULT 0,
  reasoning_output_delta INTEGER NOT NULL DEFAULT 0,
  recorded_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(run_id, source_total)
);

CREATE TABLE IF NOT EXISTS acceptance_check_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  review_run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
  delivery_run_id TEXT NOT NULL REFERENCES task_runs(id),
  criterion TEXT NOT NULL,
  command TEXT NOT NULL,
  status TEXT NOT NULL,
  exit_code INTEGER,
  output TEXT NOT NULL DEFAULT '',
  duration_ms INTEGER NOT NULL DEFAULT 0,
  workspace_fingerprint TEXT NOT NULL DEFAULT '',
  cache_hit INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS location_analyses (
  id TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(id),
  delivery_run_id TEXT REFERENCES task_runs(id),
  delivery_attempt INTEGER,
  project TEXT NOT NULL,
  query TEXT NOT NULL,
  obsidian_evidence TEXT NOT NULL DEFAULT '{}',
  location_plan TEXT NOT NULL DEFAULT '{}',
  location_evidence TEXT NOT NULL DEFAULT '{}',
  targets TEXT NOT NULL DEFAULT '[]',
  acceptance_plan TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'prepared',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  consumed_at TEXT
);

CREATE TABLE IF NOT EXISTS location_reports (
  project TEXT PRIMARY KEY,
  available INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'error',
  summary TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '{}',
  agent_id TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_conversations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  run_id TEXT REFERENCES task_runs(id),
  role TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(task_id, thread_id, role)
);

CREATE TABLE IF NOT EXISTS task_run_conversations (
  run_id TEXT PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  role TEXT NOT NULL,
  thread_id TEXT NOT NULL,
  title TEXT NOT NULL DEFAULT '',
  summary TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'active',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS task_relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_task_id TEXT NOT NULL REFERENCES tasks(id),
  target_task_id TEXT NOT NULL REFERENCES tasks(id),
  relation_type TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_task_id, target_task_id, relation_type)
);

CREATE TABLE IF NOT EXISTS task_targets (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  file TEXT NOT NULL,
  symbol TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(task_id, file, symbol)
);

CREATE TABLE IF NOT EXISTS task_change_requests (
  id TEXT PRIMARY KEY,
  candidate_task_id TEXT NOT NULL REFERENCES tasks(id),
  source_thread_id TEXT,
  request_text TEXT NOT NULL,
  proposed_task TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{}',
  status TEXT NOT NULL DEFAULT 'pending',
  decision TEXT,
  result_task_id TEXT REFERENCES tasks(id),
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS task_revisions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  change_request_id TEXT REFERENCES task_change_requests(id),
  version INTEGER NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  before_snapshot TEXT NOT NULL,
  after_snapshot TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(task_id, version)
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  round INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]',
  passed_items TEXT NOT NULL DEFAULT '[]',
  failed_criteria TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS acceptance_results (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
  delivery_run_id TEXT REFERENCES task_runs(id),
  round INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]',
  passed_criteria TEXT NOT NULL DEFAULT '[]',
  failed_criteria TEXT NOT NULL DEFAULT '[]',
  failure_locations TEXT NOT NULL DEFAULT '[]',
  created_bug_task_id TEXT REFERENCES tasks(id),
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS experiences (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  project TEXT,
  modules TEXT NOT NULL DEFAULT '[]',
  keywords TEXT NOT NULL DEFAULT '[]',
  content TEXT NOT NULL,
  source_tasks TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'active',
  superseded_by TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS system_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS integration_outbox (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  integration TEXT NOT NULL,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending',
  attempts INTEGER NOT NULL DEFAULT 0,
  last_error TEXT NOT NULL DEFAULT '',
  next_attempt_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_requirements_status ON requirements(status);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_project_status ON tasks(project, status, created_at);
CREATE INDEX IF NOT EXISTS idx_tasks_requirement ON tasks(requirement_id);
CREATE INDEX IF NOT EXISTS idx_task_runs_task ON task_runs(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_task_type_status ON task_runs(task_id, run_type, status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_runs_status ON task_runs(status, lease_expires_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_events_recorded ON token_usage_events(recorded_at);
CREATE INDEX IF NOT EXISTS idx_token_usage_events_task ON token_usage_events(task_id, recorded_at);
CREATE INDEX IF NOT EXISTS idx_acceptance_checks_review ON acceptance_check_runs(review_run_id, criterion, created_at);
CREATE INDEX IF NOT EXISTS idx_acceptance_results_task ON acceptance_results(task_id, round, created_at);
CREATE INDEX IF NOT EXISTS idx_location_analyses_task ON location_analyses(task_id, stage, created_at);
CREATE INDEX IF NOT EXISTS idx_location_reports_state ON location_reports(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_task_conversations_task ON task_conversations(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_run_conversations_task ON task_run_conversations(task_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_run_conversations_thread ON task_run_conversations(task_id, thread_id, created_at);
CREATE INDEX IF NOT EXISTS idx_task_targets_lookup ON task_targets(file, symbol, task_id);
CREATE INDEX IF NOT EXISTS idx_task_change_requests_status ON task_change_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_task_revisions_task ON task_revisions(task_id, version);
CREATE INDEX IF NOT EXISTS idx_events_entity ON events(entity_type, entity_id);
CREATE INDEX IF NOT EXISTS idx_integration_outbox_pending ON integration_outbox(status, next_attempt_at, created_at);
"""

# Version 2 re-applies v2 run/conversation validation triggers. Version 1 may
# have been overwritten by a still-running pre-v2 MCP process.
SCHEMA_VERSION = 10


class Database:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".migrate.lock")
        with lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                with self.connection() as connection:
                    connection.executescript(SCHEMA)
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if self._needs_migration(connection, version):
                        self._migrate(connection)
                        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    connection.commit()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _needs_migration(connection: sqlite3.Connection, version: int) -> bool:
        """Detect version drift and the small set of known legacy-runtime damage."""
        if version < SCHEMA_VERSION:
            return True
        required_task_columns = {
            "workflow_version", "active_run_id", "primary_run_id", "location_context",
            "dependency_analysis", "implementation_contract", "review_contract", "status_started_at",
            "context_version",
            "effective_token_used",
        }
        task_columns = {row["name"] for row in connection.execute("PRAGMA table_info(tasks)")}
        if not required_task_columns.issubset(task_columns):
            return True
        required_run_columns = {
            "artifact_snapshot", "input_tokens", "cached_input_tokens",
            "output_tokens", "reasoning_output_tokens",
            "effective_token_used",
        }
        run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_runs)")}
        if not required_run_columns.issubset(run_columns):
            return True
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(token_usage_events)")}
        if not {
            "input_delta", "cached_input_delta", "output_delta",
            "reasoning_output_delta", "effective_token_delta",
        }.issubset(event_columns):
            return True
        check_columns = {row["name"] for row in connection.execute("PRAGMA table_info(acceptance_check_runs)")}
        if not {"workspace_fingerprint", "cache_hit"}.issubset(check_columns):
            return True
        analysis_columns = {row["name"] for row in connection.execute("PRAGMA table_info(location_analyses)")}
        if not {"location_plan", "location_evidence"}.issubset(analysis_columns):
            return True
        trigger_sql = {
            row["name"]: row["sql"] or ""
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger'"
            )
        }
        signatures = {
            "validate_task_update": ("acceptance_blocked", "primary_run_id"),
            "validate_run_update": ("bugfix", "code_review", "acceptance"),
            "validate_run_conversation_update": ("task_run_conversations", "run_type=NEW.role"),
            "track_task_status_started_at": ("status_started_at",),
            "track_run_stage_completed_at": ("stage_completed_at",),
        }
        if any(
            not all(signature in trigger_sql.get(name, "") for signature in expected)
            for name, expected in signatures.items()
        ):
            return True
        legacy_table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name IN ('notifications','notification_channels') LIMIT 1"
        ).fetchone()
        if legacy_table:
            return True
        missing_targets = connection.execute(
            """SELECT 1 FROM tasks t
               WHERE json_valid(t.location_context)
                 AND json_array_length(json_extract(t.location_context, '$.targets')) > 0
                 AND NOT EXISTS(SELECT 1 FROM task_targets target WHERE target.task_id=t.id)
               LIMIT 1"""
        ).fetchone()
        return missing_targets is not None

    @staticmethod
    def _migrate(connection: sqlite3.Connection) -> None:
        """Apply additive migrations to databases created by older plugin builds."""
        # Older builds mutated one canonical run between execution/review/rework.
        # Drop role-matching triggers before reconciling those legacy rows.
        task_statuses = sql_values(TASK_STATUSES)
        run_types = sql_values(RUN_TYPES)
        run_statuses = sql_values(RUN_STATUSES)
        conversation_roles = sql_values(CONVERSATION_ROLES)
        active_run_statuses = sql_values(ACTIVE_RUN_STATUSES)
        connection.executescript(
            f"""
            DROP TRIGGER IF EXISTS validate_task_insert;
            DROP TRIGGER IF EXISTS validate_task_update;
            DROP TRIGGER IF EXISTS validate_run_insert;
            DROP TRIGGER IF EXISTS validate_run_update;
            DROP TRIGGER IF EXISTS validate_conversation_insert;
            DROP TRIGGER IF EXISTS validate_conversation_update;
            DROP TRIGGER IF EXISTS validate_run_conversation_insert;
            DROP TRIGGER IF EXISTS validate_run_conversation_update;
            """
        )
        columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(tasks)").fetchall()
        }
        additions = {
            "active_run_id": "TEXT",
            "primary_run_id": "TEXT",
            "delivery_summary": "TEXT NOT NULL DEFAULT ''",
            "verification_result": "TEXT NOT NULL DEFAULT ''",
            "location_context": "TEXT NOT NULL DEFAULT '{}'",
            "acceptance_plan": "TEXT NOT NULL DEFAULT '[]'",
            "dependency_analysis": "TEXT NOT NULL DEFAULT '{}'",
            "implementation_contract": "TEXT NOT NULL DEFAULT '{}'",
            "review_contract": "TEXT NOT NULL DEFAULT '{}'",
            "parent_acceptance_task_id": "TEXT REFERENCES tasks(id)",
            "workflow_version": "INTEGER NOT NULL DEFAULT 2",
            "paused_from_status": "TEXT",
            "last_failure_reason": "TEXT NOT NULL DEFAULT ''",
            "last_failure_at": "TEXT",
            "review_failed_at": "TEXT",
            "last_review_reasons": "TEXT NOT NULL DEFAULT '[]'",
            "last_failed_criteria": "TEXT NOT NULL DEFAULT '[]'",
            "review_rework_count": "INTEGER NOT NULL DEFAULT 0",
            "auto_dispatch": "INTEGER NOT NULL DEFAULT 1",
            "retry_required": "INTEGER NOT NULL DEFAULT 0",
            "retry_run_type": "TEXT",
            "blocked_from_status": "TEXT",
            "review_interrupt_count": "INTEGER NOT NULL DEFAULT 0",
            "review_retry_after": "TEXT",
            "dispatch_failure_count": "INTEGER NOT NULL DEFAULT 0",
            "dispatch_retry_after": "TEXT",
            "last_dispatch_error": "TEXT NOT NULL DEFAULT ''",
            "status_started_at": "TEXT",
            "context_version": "INTEGER NOT NULL DEFAULT 1",
            "effective_token_used": "INTEGER NOT NULL DEFAULT 0",
        }
        workflow_version_added = "workflow_version" not in columns
        for name, definition in additions.items():
            if name not in columns:
                connection.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
        connection.execute(
            "UPDATE tasks SET status_started_at=COALESCE(status_started_at, updated_at, created_at, CURRENT_TIMESTAMP)"
        )
        if workflow_version_added:
            # Rows from pre-v2 databases retain their legacy review semantics.
            connection.execute("UPDATE tasks SET workflow_version=1 WHERE workflow_version=2")
        else:
            # A database upgraded by an intermediate build may already contain
            # the column while its pre-v2 rows still have empty contracts.
            connection.execute(
                """UPDATE tasks SET workflow_version=1
                   WHERE workflow_version=2 AND implementation_contract='{}'
                     AND dependency_analysis='{}'"""
            )
        run_columns = {row["name"] for row in connection.execute("PRAGMA table_info(task_runs)").fetchall()}
        run_additions = {
            "changed_locations": "TEXT NOT NULL DEFAULT '[]'",
            "acceptance_evidence": "TEXT NOT NULL DEFAULT '[]'",
            "artifact_snapshot": "TEXT NOT NULL DEFAULT '{}'",
            "parent_run_id": "TEXT REFERENCES task_runs(id)",
            "delivery_run_id": "TEXT REFERENCES task_runs(id)",
            "started_at": "TEXT",
            "stage_completed_at": "TEXT",
            "input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "cached_input_tokens": "INTEGER NOT NULL DEFAULT 0",
            "output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "reasoning_output_tokens": "INTEGER NOT NULL DEFAULT 0",
            "effective_token_used": "INTEGER NOT NULL DEFAULT 0",
        }
        for name, definition in run_additions.items():
            if name not in run_columns:
                connection.execute(f"ALTER TABLE task_runs ADD COLUMN {name} {definition}")
        event_columns = {row["name"] for row in connection.execute("PRAGMA table_info(token_usage_events)").fetchall()}
        for name in (
            "input_delta", "cached_input_delta", "output_delta",
            "reasoning_output_delta", "effective_token_delta",
        ):
            if name not in event_columns:
                connection.execute(f"ALTER TABLE token_usage_events ADD COLUMN {name} INTEGER NOT NULL DEFAULT 0")
        check_columns = {row["name"] for row in connection.execute("PRAGMA table_info(acceptance_check_runs)").fetchall()}
        if "workspace_fingerprint" not in check_columns:
            connection.execute("ALTER TABLE acceptance_check_runs ADD COLUMN workspace_fingerprint TEXT NOT NULL DEFAULT ''")
        if "cache_hit" not in check_columns:
            connection.execute("ALTER TABLE acceptance_check_runs ADD COLUMN cache_hit INTEGER NOT NULL DEFAULT 0")
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_acceptance_checks_cache "
            "ON acceptance_check_runs(delivery_run_id, criterion, command, workspace_fingerprint, created_at)"
        )
        connection.execute(
            """UPDATE task_runs SET started_at=COALESCE((
                   SELECT mapping.created_at FROM task_run_conversations mapping
                   WHERE mapping.run_id=task_runs.id
               ), created_at)
               WHERE started_at IS NULL AND status!='awaiting_thread'"""
        )
        connection.execute(
            """UPDATE task_runs SET stage_completed_at=COALESCE((
                   SELECT MIN(next.created_at) FROM task_runs next
                   WHERE next.delivery_run_id=task_runs.id
                     AND next.run_type IN ('review','code_review','acceptance')
               ), completed_at, updated_at)
               WHERE stage_completed_at IS NULL
                 AND status NOT IN ('awaiting_thread','running')"""
        )
        connection.execute(
            """UPDATE tasks SET token_used=MAX(token_used, COALESCE((
                   SELECT SUM(r.token_used) FROM task_runs r WHERE r.task_id=tasks.id
               ), 0))"""
        )
        connection.execute(
            """UPDATE task_runs SET effective_token_used=MAX(effective_token_used,
                   CASE WHEN input_tokens + output_tokens > 0 THEN
                     MAX(input_tokens - MIN(cached_input_tokens, input_tokens), 0)
                     + CAST((MIN(cached_input_tokens, input_tokens) + 9) / 10 AS INTEGER)
                     + output_tokens
                   ELSE token_used END)"""
        )
        connection.execute(
            """UPDATE tasks SET effective_token_used=MAX(effective_token_used, COALESCE((
                   SELECT SUM(r.effective_token_used) FROM task_runs r WHERE r.task_id=tasks.id
               ), 0))"""
        )
        # Older databases only stored the latest cumulative total per run. Seed
        # one event at the run's last update so historical totals remain visible;
        # future updates are recorded as exact deltas by the service.
        connection.execute(
            """INSERT OR IGNORE INTO token_usage_events(
                   task_id, run_id, source_total, token_delta, recorded_at
               )
               SELECT task_id, id, token_used, token_used, COALESCE(updated_at, created_at)
               FROM task_runs WHERE token_used > 0"""
        )
        connection.execute(
            """UPDATE tasks SET status_started_at=COALESCE((
                   SELECT MAX(COALESCE(r.stage_completed_at, r.completed_at, r.updated_at))
                   FROM task_runs r WHERE r.task_id=tasks.id
               ), status_started_at)
               WHERE status IN ('done','cancelled')"""
        )
        analysis_columns = {row["name"] for row in connection.execute("PRAGMA table_info(location_analyses)").fetchall()}
        if "location_plan" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN location_plan TEXT NOT NULL DEFAULT '{}'")
        if "location_evidence" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN location_evidence TEXT NOT NULL DEFAULT '{}'")
        if "acceptance_plan" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN acceptance_plan TEXT NOT NULL DEFAULT '[]'")
        if "delivery_run_id" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN delivery_run_id TEXT REFERENCES task_runs(id)")
        if "delivery_attempt" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN delivery_attempt INTEGER")
        if "dependency_analysis" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN dependency_analysis TEXT NOT NULL DEFAULT '{}'")
        if "implementation_contract" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN implementation_contract TEXT NOT NULL DEFAULT '{}'")
        if "review_contract" not in analysis_columns:
            connection.execute("ALTER TABLE location_analyses ADD COLUMN review_contract TEXT NOT NULL DEFAULT '{}'")
        review_columns = {row["name"] for row in connection.execute("PRAGMA table_info(reviews)").fetchall()}
        if "failed_criteria" not in review_columns:
            connection.execute("ALTER TABLE reviews ADD COLUMN failed_criteria TEXT NOT NULL DEFAULT '[]'")
        connection.execute("""CREATE TABLE IF NOT EXISTS acceptance_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
            run_id TEXT NOT NULL REFERENCES task_runs(id) ON DELETE CASCADE,
            delivery_run_id TEXT REFERENCES task_runs(id),
            round INTEGER NOT NULL,
            verdict TEXT NOT NULL,
            reasons TEXT NOT NULL DEFAULT '[]',
            passed_criteria TEXT NOT NULL DEFAULT '[]',
            failed_criteria TEXT NOT NULL DEFAULT '[]',
            failure_locations TEXT NOT NULL DEFAULT '[]',
            created_bug_task_id TEXT REFERENCES tasks(id),
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )""")
        connection.execute("CREATE INDEX IF NOT EXISTS idx_acceptance_results_task ON acceptance_results(task_id, round, created_at)")
        # Status notifications and their dedicated conversations were removed.
        # Drop legacy data so upgraded databases match fresh installations.
        connection.execute("DELETE FROM task_conversations WHERE role='notification'")
        connection.execute("DELETE FROM id_counters WHERE prefix='NOTICE'")
        connection.execute("DROP TABLE IF EXISTS notifications")
        connection.execute("DROP TABLE IF EXISTS notification_channels")
        connection.execute(
            """UPDATE tasks SET status='blocked', blocked_from_status='completion',
               last_failure_reason='历史完成中状态缺少最终验收，请人工确认后重新执行',
               updated_at=CURRENT_TIMESTAMP WHERE status='completion'"""
        )
        # Preserve old completed tasks as an explicit migrated acceptance pass.
        connection.execute(
            """INSERT INTO acceptance_results(task_id, run_id, delivery_run_id, round, verdict,
                   reasons, passed_criteria, failed_criteria, failure_locations)
               SELECT r.task_id, COALESCE(
                   (SELECT id FROM task_runs WHERE task_id=r.task_id ORDER BY created_at DESC LIMIT 1),
                   'MIGRATED-' || r.task_id),
                   (SELECT id FROM task_runs WHERE task_id=r.task_id ORDER BY created_at DESC LIMIT 1),
                   r.round, 'pass', r.reasons, r.passed_items, '[]', '[]'
               FROM reviews r JOIN tasks t ON t.id=r.task_id
               WHERE t.status='done' AND r.verdict='pass'
                 AND EXISTS(SELECT 1 FROM task_runs tr WHERE tr.task_id=r.task_id)
                 AND NOT EXISTS(SELECT 1 FROM acceptance_results a WHERE a.task_id=r.task_id)"""
        )
        connection.execute(
            """UPDATE tasks SET status='blocked', blocked_from_status='done',
               last_failure_reason='历史完成记录缺少验收记录，请人工确认后重新执行',
               updated_at=CURRENT_TIMESTAMP
               WHERE status='done' AND (
                   (workflow_version>=2 AND NOT EXISTS(SELECT 1 FROM acceptance_results WHERE acceptance_results.task_id=tasks.id AND verdict='pass'))
                   OR (workflow_version<2
                       AND NOT EXISTS(SELECT 1 FROM acceptance_results WHERE acceptance_results.task_id=tasks.id AND verdict='pass')
                       AND NOT EXISTS(SELECT 1 FROM reviews legacy WHERE legacy.task_id=tasks.id AND legacy.verdict='pass'))
               )"""
        )
        connection.execute(
            """UPDATE task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP,
               updated_at=CURRENT_TIMESTAMP WHERE status IN ('awaiting_thread','running')
               AND task_id IN (SELECT id FROM tasks WHERE status IN ('done','cancelled')
                 OR blocked_from_status IN ('done','completion'))"""
        )
        connection.execute(
            """UPDATE tasks SET active_run_id=NULL, assigned_to=NULL
               WHERE active_run_id IS NOT NULL AND NOT EXISTS(
                 SELECT 1 FROM task_runs WHERE task_runs.id=tasks.active_run_id
                   AND task_runs.task_id=tasks.id AND task_runs.status IN ('awaiting_thread','running')
               )"""
        )
        connection.execute(
            "INSERT OR IGNORE INTO system_settings(key, value) VALUES('dispatcher_enabled', '0')"
        )
        # Convert a legacy in-place review row back into its delivery role. New
        # reviews always receive a distinct run and point at this delivery.
        connection.execute(
            """UPDATE task_runs SET run_type=CASE
                     WHEN (SELECT review_rework_count FROM tasks WHERE id=task_runs.task_id)>0 THEN 'rework'
                     ELSE 'execution' END
               WHERE run_type='review' AND acceptance_evidence!='[]'
                 AND id IN (SELECT primary_run_id FROM tasks WHERE primary_run_id IS NOT NULL)"""
        )
        connection.execute(
            """UPDATE task_runs SET status='waiting_review', completed_at=NULL,
                   updated_at=CURRENT_TIMESTAMP
               WHERE id IN (SELECT primary_run_id FROM tasks WHERE status='review')
                 AND run_type IN ('execution','rework')"""
        )
        connection.execute(
            """UPDATE tasks SET active_run_id=NULL, assigned_to=NULL
               WHERE status='review' AND active_run_id=primary_run_id"""
        )
        connection.execute(
            """DELETE FROM task_conversations
               WHERE run_id IS NOT NULL AND EXISTS(
                 SELECT 1 FROM task_runs r JOIN task_conversations other
                   ON other.task_id=task_conversations.task_id
                  AND other.thread_id=task_conversations.thread_id
                  AND other.role=r.run_type
                  AND other.id!=task_conversations.id
                 WHERE r.id=task_conversations.run_id
               )"""
        )
        connection.execute(
            """UPDATE task_run_conversations SET role=(
                 SELECT run_type FROM task_runs WHERE id=task_run_conversations.run_id)
               WHERE role!=(SELECT run_type FROM task_runs WHERE id=task_run_conversations.run_id)"""
        )
        connection.execute(
            """UPDATE task_conversations SET role=(
                 SELECT run_type FROM task_runs WHERE id=task_conversations.run_id)
               WHERE run_id IS NOT NULL
                 AND role!=(SELECT run_type FROM task_runs WHERE id=task_conversations.run_id)"""
        )
        # primary_run_id is the latest submitted or active delivery, never a
        # review run. Each retry/rework/review receives its own immutable row.
        connection.execute(
            """UPDATE tasks SET primary_run_id=(
                   SELECT r.id FROM task_runs r WHERE r.task_id=tasks.id
                     AND r.run_type IN ('execution','rework')
                   ORDER BY (r.acceptance_evidence!='[]') DESC,
                            r.attempt DESC, r.created_at DESC LIMIT 1)
               WHERE primary_run_id IS NULL OR EXISTS(
                 SELECT 1 FROM task_runs current
                 WHERE current.id=tasks.primary_run_id AND current.run_type='review')"""
        )
        # Keep the database capable of rejecting duplicate active runs even if an
        # application caller misses its optimistic-lock check. Older databases
        # are reconciled before the partial unique index is installed.
        duplicate_active = connection.execute(
            """SELECT task_id FROM task_runs
               WHERE status IN ('awaiting_thread','running')
               GROUP BY task_id HAVING COUNT(*) > 1"""
        ).fetchall()
        for row in duplicate_active:
            active = connection.execute(
                "SELECT active_run_id FROM tasks WHERE id=?", (row["task_id"],)
            ).fetchone()
            keep_id = active["active_run_id"] if active and active["active_run_id"] else connection.execute(
                """SELECT id FROM task_runs WHERE task_id=? AND status IN ('awaiting_thread','running')
                   ORDER BY created_at DESC, id DESC LIMIT 1""",
                (row["task_id"],),
            ).fetchone()["id"]
            connection.execute(
                """UPDATE task_runs SET status='interrupted', completed_at=CURRENT_TIMESTAMP,
                   updated_at=CURRENT_TIMESTAMP
                   WHERE task_id=? AND status IN ('awaiting_thread','running') AND id!=?""",
                (row["task_id"], keep_id),
            )
            connection.execute(
                "UPDATE tasks SET active_run_id=? WHERE id=?", (keep_id, row["task_id"]),
            )
        connection.execute(
            """CREATE UNIQUE INDEX IF NOT EXISTS idx_task_runs_one_active
               ON task_runs(task_id) WHERE status IN ('awaiting_thread','running')"""
        )
        connection.executescript(
            f"""
            DROP TRIGGER IF EXISTS validate_task_insert;
            DROP TRIGGER IF EXISTS validate_task_update;
            DROP TRIGGER IF EXISTS validate_run_insert;
            DROP TRIGGER IF EXISTS validate_run_update;
            DROP TRIGGER IF EXISTS validate_conversation_insert;
            DROP TRIGGER IF EXISTS validate_conversation_update;
            DROP TRIGGER IF EXISTS validate_run_conversation_insert;
            DROP TRIGGER IF EXISTS validate_run_conversation_update;
            DROP TRIGGER IF EXISTS track_task_status_started_at;
            DROP TRIGGER IF EXISTS track_run_stage_completed_at;

            CREATE TRIGGER IF NOT EXISTS validate_task_insert BEFORE INSERT ON tasks
            WHEN NEW.status NOT IN ({task_statuses})
              OR NEW.priority NOT IN ('P0','P1','P2','P3') OR NEW.token_budget<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
              OR NEW.auto_dispatch NOT IN (0,1) OR NEW.retry_required NOT IN (0,1) OR NEW.dispatch_failure_count<0
              OR NEW.status IN ('review','code_review','acceptance','acceptance_blocked','done')
            BEGIN SELECT RAISE(ABORT, 'invalid task values'); END;

            CREATE TRIGGER IF NOT EXISTS validate_task_update BEFORE UPDATE ON tasks
            WHEN NEW.status NOT IN ({task_statuses})
              OR NEW.priority NOT IN ('P0','P1','P2','P3') OR NEW.token_budget<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
              OR NEW.auto_dispatch NOT IN (0,1) OR NEW.retry_required NOT IN (0,1) OR NEW.dispatch_failure_count<0
              OR (NEW.status IN ('review','code_review') AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.task_id=NEW.id
                     AND r.id=NEW.primary_run_id AND (
                       r.run_type IN ('execution','rework','bugfix') AND r.status='waiting_review')))
              OR (NEW.status='done' AND (
                   (NEW.workflow_version>=2 AND NOT EXISTS(
                       SELECT 1 FROM acceptance_results v WHERE v.task_id=NEW.id AND v.verdict='pass'))
                   OR (NEW.workflow_version<2 AND NOT EXISTS(
                       SELECT 1 FROM acceptance_results v WHERE v.task_id=NEW.id AND v.verdict='pass'
                   ) AND NOT EXISTS(
                       SELECT 1 FROM reviews legacy WHERE legacy.task_id=NEW.id AND legacy.verdict='pass'))
               ))
              OR (NEW.active_run_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.id=NEW.active_run_id AND r.task_id=NEW.id
                     AND r.status IN ({active_run_statuses})))
            BEGIN SELECT RAISE(ABORT, 'invalid task values or active run'); END;

            CREATE TRIGGER IF NOT EXISTS validate_run_insert BEFORE INSERT ON task_runs
            WHEN NEW.run_type NOT IN ({run_types})
              OR NEW.status NOT IN ({run_statuses})
              OR NEW.attempt<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
            BEGIN SELECT RAISE(ABORT, 'invalid task run values'); END;

            CREATE TRIGGER IF NOT EXISTS validate_run_update BEFORE UPDATE ON task_runs
            WHEN NEW.run_type NOT IN ({run_types})
              OR NEW.status NOT IN ({run_statuses})
              OR NEW.attempt<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
            BEGIN SELECT RAISE(ABORT, 'invalid task run values'); END;

            CREATE TRIGGER IF NOT EXISTS validate_conversation_insert BEFORE INSERT ON task_conversations
            WHEN NEW.role NOT IN ({conversation_roles}) OR trim(NEW.thread_id)=''
              OR (NEW.run_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role))
            BEGIN SELECT RAISE(ABORT, 'invalid conversation values'); END;

            CREATE TRIGGER IF NOT EXISTS validate_conversation_update BEFORE UPDATE ON task_conversations
            WHEN NEW.role NOT IN ({conversation_roles}) OR trim(NEW.thread_id)=''
              OR (NEW.run_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role))
            BEGIN SELECT RAISE(ABORT, 'invalid conversation values'); END;

            CREATE TRIGGER IF NOT EXISTS validate_run_conversation_insert BEFORE INSERT ON task_run_conversations
            WHEN NEW.role NOT IN ({run_types}) OR trim(NEW.thread_id)=''
              OR NOT EXISTS(SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role)
            BEGIN SELECT RAISE(ABORT, 'invalid run conversation values'); END;

            CREATE TRIGGER IF NOT EXISTS validate_run_conversation_update BEFORE UPDATE ON task_run_conversations
            WHEN NEW.role NOT IN ({run_types}) OR trim(NEW.thread_id)=''
              OR NOT EXISTS(SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role)
            BEGIN SELECT RAISE(ABORT, 'invalid run conversation values'); END;

            CREATE TRIGGER track_task_status_started_at AFTER UPDATE OF status ON tasks
            WHEN OLD.status != NEW.status
            BEGIN
              UPDATE tasks SET status_started_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
              WHERE id=NEW.id;
            END;

            CREATE TRIGGER track_run_stage_completed_at AFTER UPDATE OF status ON task_runs
            WHEN OLD.status IN ({active_run_statuses})
              AND NEW.status NOT IN ({active_run_statuses})
            BEGIN
              UPDATE task_runs SET stage_completed_at=COALESCE(stage_completed_at, CURRENT_TIMESTAMP)
              WHERE id=NEW.id;
            END;
            """
        )
        connection.execute(
            """INSERT OR IGNORE INTO task_run_conversations(
                   run_id, task_id, role, thread_id, title, summary, status, created_at, updated_at
               )
               SELECT run_id, task_id, role, thread_id, title, summary, status, created_at, updated_at
               FROM task_conversations WHERE run_id IS NOT NULL"""
        )
        connection.execute(
            """UPDATE task_conversations
               SET run_id=(
                     SELECT mapping.run_id FROM task_run_conversations mapping
                     WHERE mapping.task_id=task_conversations.task_id
                       AND mapping.thread_id=task_conversations.thread_id
                       AND mapping.role=task_conversations.role
                     ORDER BY mapping.updated_at DESC, mapping.created_at DESC,
                              CAST(substr(mapping.run_id, 5) AS INTEGER) DESC LIMIT 1
                   ),
                   status=COALESCE((
                     SELECT mapping.status FROM task_run_conversations mapping
                     WHERE mapping.task_id=task_conversations.task_id
                       AND mapping.thread_id=task_conversations.thread_id
                       AND mapping.role=task_conversations.role
                     ORDER BY mapping.updated_at DESC, mapping.created_at DESC,
                              CAST(substr(mapping.run_id, 5) AS INTEGER) DESC LIMIT 1
                   ), status),
                   updated_at=CURRENT_TIMESTAMP
               WHERE EXISTS(
                 SELECT 1 FROM task_run_conversations mapping
                 WHERE mapping.task_id=task_conversations.task_id
                   AND mapping.thread_id=task_conversations.thread_id
                   AND mapping.role=task_conversations.role
               )"""
        )
        for row in connection.execute("SELECT id, location_context FROM tasks").fetchall():
            try:
                targets = json.loads(row["location_context"] or "{}").get("targets", [])
            except (TypeError, json.JSONDecodeError):
                targets = []
            for target in targets:
                raw_file = str(target.get("file") or "").strip()
                if not raw_file:
                    continue
                file = posixpath.normpath(raw_file)
                if (
                    raw_file.startswith("/") or "\\" in raw_file or file in {".", ".."}
                    or file.startswith("../") or (len(raw_file) >= 2 and raw_file[0].isalpha() and raw_file[1] == ":")
                ):
                    connection.execute(
                        """UPDATE tasks SET blocked_from_status=status, status='blocked',
                           auto_dispatch=0, last_failure_reason='历史任务包含越出项目目录的位置锁，请重新定位',
                           updated_at=CURRENT_TIMESTAMP WHERE id=? AND status NOT IN ('done','cancelled')""",
                        (row["id"],),
                    )
                    continue
                symbols = sorted({str(symbol).strip() for symbol in target.get("symbols", []) if str(symbol).strip()}) or [""]
                connection.executemany(
                    "INSERT OR IGNORE INTO task_targets(task_id, file, symbol) VALUES(?, ?, ?)",
                    [(row["id"], file, symbol) for symbol in symbols],
                )

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a read connection and always close its file descriptors."""
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

    def next_id(self, connection: sqlite3.Connection, prefix: str) -> str:
        connection.execute(
            "INSERT INTO id_counters(prefix, value) VALUES(?, 0) "
            "ON CONFLICT(prefix) DO NOTHING",
            (prefix,),
        )
        connection.execute(
            "UPDATE id_counters SET value = value + 1 WHERE prefix = ?", (prefix,)
        )
        value = connection.execute(
            "SELECT value FROM id_counters WHERE prefix = ?", (prefix,)
        ).fetchone()["value"]
        return f"{prefix}-{value:04d}"
