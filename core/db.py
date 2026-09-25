from __future__ import annotations

import logging
import fcntl
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
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


SCHEMA = f"""
CREATE TABLE id_counters (
  prefix TEXT PRIMARY KEY,
  value INTEGER NOT NULL
);

CREATE TABLE requirements (
  id TEXT PRIMARY KEY,
  title TEXT NOT NULL,
  original_content TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  source_type TEXT NOT NULL DEFAULT 'conversation',
  source_reference TEXT,
  project TEXT,
  status TEXT NOT NULL DEFAULT 'ready',
  priority TEXT NOT NULL DEFAULT 'P2',
  goal TEXT NOT NULL DEFAULT '',
  modules TEXT NOT NULL DEFAULT '[]',
  scope TEXT NOT NULL DEFAULT '[]',
  out_of_scope TEXT NOT NULL DEFAULT '[]',
  acceptance_criteria TEXT NOT NULL DEFAULT '[]',
  visual_references TEXT NOT NULL DEFAULT '[]',
  source_thread_id TEXT,
  auto_dispatch INTEGER NOT NULL DEFAULT 1,
  decomposition_plan TEXT NOT NULL DEFAULT '[]',
  decomposition_attempts INTEGER NOT NULL DEFAULT 0,
  last_decomposition_error TEXT NOT NULL DEFAULT '',
  decomposed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE tasks (
  id TEXT PRIMARY KEY,
  requirement_id TEXT REFERENCES requirements(id),
  requirement_task_key TEXT,
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
  location_context TEXT NOT NULL DEFAULT '{{}}',
  acceptance_plan TEXT NOT NULL DEFAULT '[]',
  dependency_analysis TEXT NOT NULL DEFAULT '{{}}',
  implementation_contract TEXT NOT NULL DEFAULT '{{}}',
  review_contract TEXT NOT NULL DEFAULT '{{}}',
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
  execution_recovery_count INTEGER NOT NULL DEFAULT 0,
  last_recovery_reason TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_runs (
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
  context_snapshot TEXT NOT NULL DEFAULT '{{}}',
  delivery_summary TEXT NOT NULL DEFAULT '',
  verification_result TEXT NOT NULL DEFAULT '',
  changed_locations TEXT NOT NULL DEFAULT '[]',
  acceptance_evidence TEXT NOT NULL DEFAULT '[]',
  artifact_snapshot TEXT NOT NULL DEFAULT '{{}}',
  execution_environment TEXT NOT NULL DEFAULT 'local',
  workspace_path TEXT NOT NULL DEFAULT '',
  base_revision TEXT NOT NULL DEFAULT '',
  base_ref TEXT NOT NULL DEFAULT '',
  output_revision TEXT NOT NULL DEFAULT '',
  artifact_path TEXT NOT NULL DEFAULT '',
  artifact_sha256 TEXT NOT NULL DEFAULT '',
  integration_status TEXT NOT NULL DEFAULT '',
  integration_error TEXT NOT NULL DEFAULT '',
  integration_revision TEXT NOT NULL DEFAULT '',
  workspace_sync_status TEXT NOT NULL DEFAULT '',
  workspace_sync_error TEXT NOT NULL DEFAULT '',
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

CREATE TABLE requirement_decomposition_runs (
  id TEXT PRIMARY KEY,
  requirement_id TEXT NOT NULL REFERENCES requirements(id) ON DELETE CASCADE,
  attempt INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'running',
  claimed_by TEXT NOT NULL,
  lease_token TEXT NOT NULL,
  lease_expires_at TEXT NOT NULL,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(requirement_id, attempt)
);

CREATE TABLE native_dispatches (
  run_id TEXT PRIMARY KEY,
  entity_type TEXT NOT NULL CHECK(entity_type IN ('task','requirement')),
  entity_id TEXT NOT NULL,
  role TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'claimed'
    CHECK(status IN ('claimed','pending_thread','bound','completed','failed')),
  worker_id TEXT NOT NULL,
  project_path TEXT NOT NULL DEFAULT '',
  dispatch_title TEXT NOT NULL,
  dispatch_prompt TEXT NOT NULL,
  resume_thread_id TEXT NOT NULL DEFAULT '',
  client_thread_id TEXT NOT NULL DEFAULT '',
  thread_id TEXT NOT NULL DEFAULT '',
  host_id TEXT NOT NULL DEFAULT '',
  codex_project_id TEXT NOT NULL DEFAULT '',
  execution_environment TEXT NOT NULL DEFAULT 'local',
  parallel_fallback_reason TEXT NOT NULL DEFAULT '',
  base_revision TEXT NOT NULL DEFAULT '',
  base_ref TEXT NOT NULL DEFAULT '',
  resume_fallback_reason TEXT NOT NULL DEFAULT '',
  dispatch_attempt_id TEXT NOT NULL CHECK(trim(dispatch_attempt_id) != ''),
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE token_usage_events (
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

CREATE TABLE location_analyses (
  id TEXT PRIMARY KEY,
  stage TEXT NOT NULL,
  task_id TEXT REFERENCES tasks(id),
  delivery_run_id TEXT REFERENCES task_runs(id),
  delivery_attempt INTEGER,
  project TEXT NOT NULL,
  query TEXT NOT NULL,
  obsidian_evidence TEXT NOT NULL DEFAULT '{{}}',
  location_plan TEXT NOT NULL DEFAULT '{{}}',
  location_evidence TEXT NOT NULL DEFAULT '{{}}',
  targets TEXT NOT NULL DEFAULT '[]',
  acceptance_plan TEXT NOT NULL DEFAULT '[]',
  status TEXT NOT NULL DEFAULT 'prepared',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  completed_at TEXT,
  consumed_at TEXT
, dependency_analysis TEXT NOT NULL DEFAULT '{{}}', implementation_contract TEXT NOT NULL DEFAULT '{{}}', review_contract TEXT NOT NULL DEFAULT '{{}}');

CREATE TABLE location_reports (
  project TEXT PRIMARY KEY,
  available INTEGER NOT NULL DEFAULT 0,
  state TEXT NOT NULL DEFAULT 'error',
  summary TEXT NOT NULL DEFAULT '',
  evidence TEXT NOT NULL DEFAULT '{{}}',
  agent_id TEXT NOT NULL DEFAULT '',
  checked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_conversations (
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

CREATE TABLE task_run_conversations (
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

CREATE TABLE execution_batches (
  id TEXT PRIMARY KEY,
  project TEXT NOT NULL,
  owner_task_id TEXT NOT NULL REFERENCES tasks(id),
  owner_run_id TEXT REFERENCES task_runs(id),
  state TEXT NOT NULL DEFAULT 'development',
  revision INTEGER NOT NULL DEFAULT 1 CHECK(revision > 0),
  max_appended_tasks INTEGER NOT NULL DEFAULT 3 CHECK(max_appended_tasks >= 0),
  appended_count INTEGER NOT NULL DEFAULT 0 CHECK(appended_count >= 0),
  admission_open INTEGER NOT NULL DEFAULT 1 CHECK(admission_open IN (0,1)),
  delivery_run_id TEXT REFERENCES task_runs(id),
  sealed_at TEXT,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE execution_batch_tasks (
  batch_id TEXT NOT NULL REFERENCES execution_batches(id) ON DELETE CASCADE,
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  role TEXT NOT NULL DEFAULT 'appended',
  join_order INTEGER NOT NULL,
  joined_revision INTEGER NOT NULL,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(batch_id, task_id),
  UNIQUE(task_id),
  UNIQUE(batch_id, join_order)
);

CREATE TABLE execution_batch_runs (
  run_id TEXT PRIMARY KEY REFERENCES task_runs(id) ON DELETE CASCADE,
  batch_id TEXT NOT NULL REFERENCES execution_batches(id) ON DELETE CASCADE,
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE task_relations (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_task_id TEXT NOT NULL REFERENCES tasks(id),
  target_task_id TEXT NOT NULL REFERENCES tasks(id),
  relation_type TEXT NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  UNIQUE(source_task_id, target_task_id, relation_type)
);

CREATE TABLE task_targets (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  file TEXT NOT NULL,
  symbol TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY(task_id, file, symbol)
);

CREATE TABLE task_change_requests (
  id TEXT PRIMARY KEY,
  candidate_task_id TEXT NOT NULL REFERENCES tasks(id),
  source_thread_id TEXT,
  request_text TEXT NOT NULL,
  proposed_task TEXT NOT NULL,
  evidence TEXT NOT NULL DEFAULT '{{}}',
  status TEXT NOT NULL DEFAULT 'pending',
  decision TEXT,
  result_task_id TEXT REFERENCES tasks(id),
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  resolved_at TEXT
);

CREATE TABLE task_revisions (
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

CREATE TABLE reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  round INTEGER NOT NULL,
  verdict TEXT NOT NULL,
  reasons TEXT NOT NULL DEFAULT '[]',
  passed_items TEXT NOT NULL DEFAULT '[]',
  failed_criteria TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE experiences (
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

CREATE TABLE events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  entity_type TEXT NOT NULL,
  entity_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload TEXT NOT NULL DEFAULT '{{}}',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE system_settings (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE scheduler_state (
  id INTEGER PRIMARY KEY CHECK(id=1),
  generation INTEGER NOT NULL DEFAULT 0,
  handled_generation INTEGER NOT NULL DEFAULT 0,
  pending INTEGER NOT NULL DEFAULT 0 CHECK(pending IN (0,1)),
  last_trigger TEXT NOT NULL DEFAULT '',
  last_entity_type TEXT NOT NULL DEFAULT '',
  last_entity_id TEXT NOT NULL DEFAULT '',
  lease_owner TEXT NOT NULL DEFAULT '',
  lease_expires_at TEXT,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE project_integration_states (
  project TEXT PRIMARY KEY,
  integration_ref TEXT NOT NULL DEFAULT '',
  revision TEXT NOT NULL,
  workspace_fingerprint TEXT NOT NULL,
  managed_workspace_files TEXT NOT NULL DEFAULT '{{}}',
  workspace_sync_revision TEXT NOT NULL DEFAULT '',
  last_run_id TEXT NOT NULL DEFAULT '',
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE integration_outbox (
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

CREATE TABLE mobile_messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  thread_id TEXT NOT NULL,
  body TEXT NOT NULL,
  run_id TEXT REFERENCES task_runs(id),
  usage_baseline TEXT NOT NULL DEFAULT '{{}}',
  thread_usage_baseline TEXT NOT NULL DEFAULT '{{}}',
  status TEXT NOT NULL DEFAULT 'queued'
    CHECK(status IN ('queued','starting','running','completed','failed','uncertain')),
  worker_id TEXT NOT NULL DEFAULT '',
  turn_id TEXT NOT NULL DEFAULT '',
  result TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_requirements_status ON requirements(status);

CREATE INDEX idx_tasks_status ON tasks(status);

CREATE INDEX idx_tasks_project_status ON tasks(project, status, created_at);

CREATE INDEX idx_tasks_requirement ON tasks(requirement_id);

CREATE INDEX idx_requirement_runs_status
  ON requirement_decomposition_runs(status, lease_expires_at);

CREATE INDEX idx_task_runs_task ON task_runs(task_id, created_at);

CREATE INDEX idx_task_runs_task_type_status ON task_runs(task_id, run_type, status, created_at);

CREATE INDEX idx_task_runs_status ON task_runs(status, lease_expires_at);

CREATE INDEX idx_token_usage_events_recorded ON token_usage_events(recorded_at);

CREATE INDEX idx_token_usage_events_task ON token_usage_events(task_id, recorded_at);

CREATE INDEX idx_location_analyses_task ON location_analyses(task_id, stage, created_at);

CREATE INDEX idx_location_reports_state ON location_reports(state, updated_at);

CREATE INDEX idx_task_conversations_task ON task_conversations(task_id, created_at);

CREATE INDEX idx_task_run_conversations_task ON task_run_conversations(task_id, created_at);

CREATE INDEX idx_task_run_conversations_thread ON task_run_conversations(task_id, thread_id, created_at);

CREATE INDEX idx_execution_batches_project_state ON execution_batches(project, state, admission_open, created_at);

CREATE INDEX idx_execution_batch_tasks_task ON execution_batch_tasks(task_id);

CREATE INDEX idx_execution_batch_runs_batch ON execution_batch_runs(batch_id, created_at);

CREATE INDEX idx_task_targets_lookup ON task_targets(file, symbol, task_id);

CREATE INDEX idx_task_relations_source_type ON task_relations(source_task_id, relation_type, target_task_id);

CREATE INDEX idx_task_relations_target_type ON task_relations(target_task_id, relation_type, source_task_id);

CREATE INDEX idx_task_change_requests_status ON task_change_requests(status, created_at);

CREATE INDEX idx_task_revisions_task ON task_revisions(task_id, version);

CREATE INDEX idx_events_entity ON events(entity_type, entity_id);

CREATE INDEX idx_integration_outbox_pending ON integration_outbox(status, next_attempt_at, created_at);

CREATE INDEX idx_native_dispatches_worker ON native_dispatches(worker_id, status, updated_at);

CREATE INDEX idx_native_dispatches_entity ON native_dispatches(entity_type, entity_id, created_at);

CREATE INDEX idx_mobile_messages_queue ON mobile_messages(status, created_at);

CREATE INDEX idx_requirements_dispatch ON requirements(status, auto_dispatch, created_at);

CREATE UNIQUE INDEX idx_tasks_requirement_key ON tasks(requirement_id, requirement_task_key) WHERE requirement_id IS NOT NULL AND requirement_task_key IS NOT NULL;

CREATE UNIQUE INDEX idx_task_runs_one_active
               ON task_runs(task_id) WHERE status IN ({sql_values(ACTIVE_RUN_STATUSES)});

CREATE TRIGGER validate_task_insert BEFORE INSERT ON tasks
            WHEN NEW.status NOT IN ({sql_values(TASK_STATUSES)})
              OR NEW.priority NOT IN ('P0','P1','P2','P3') OR NEW.token_budget<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
              OR NEW.auto_dispatch NOT IN (0,1) OR NEW.retry_required NOT IN (0,1) OR NEW.dispatch_failure_count<0
              OR NEW.execution_recovery_count<0
              OR NEW.status IN ('code_review','done')
            BEGIN SELECT RAISE(ABORT, 'invalid task values'); END;

CREATE TRIGGER validate_task_update BEFORE UPDATE ON tasks
            WHEN NEW.status NOT IN ({sql_values(TASK_STATUSES)})
              OR NEW.priority NOT IN ('P0','P1','P2','P3') OR NEW.token_budget<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
              OR NEW.auto_dispatch NOT IN (0,1) OR NEW.retry_required NOT IN (0,1) OR NEW.dispatch_failure_count<0
              OR NEW.execution_recovery_count<0
              OR (NEW.status='code_review' AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.task_id=NEW.id
                     AND r.id=NEW.primary_run_id AND (
                       r.run_type IN ('execution','rework','bugfix') AND r.status='waiting_review')))
              OR (NEW.status='done'
                   AND COALESCE(json_extract(NEW.review_contract, '$.quality_gates.code_review.required'), 1)=1
                   AND NOT EXISTS(SELECT 1 FROM reviews review WHERE review.task_id=NEW.id AND review.verdict='pass'))
              OR (NEW.active_run_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.id=NEW.active_run_id AND r.task_id=NEW.id
                     AND r.status IN ({sql_values(ACTIVE_RUN_STATUSES)})))
            BEGIN SELECT RAISE(ABORT, 'invalid task values or active run'); END;

CREATE TRIGGER validate_run_insert BEFORE INSERT ON task_runs
            WHEN NEW.run_type NOT IN ({sql_values(RUN_TYPES)})
              OR NEW.status NOT IN ({sql_values(RUN_STATUSES)})
              OR NEW.attempt<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
            BEGIN SELECT RAISE(ABORT, 'invalid task run values'); END;

CREATE TRIGGER validate_run_update BEFORE UPDATE ON task_runs
            WHEN NEW.run_type NOT IN ({sql_values(RUN_TYPES)})
              OR NEW.status NOT IN ({sql_values(RUN_STATUSES)})
              OR NEW.attempt<=0 OR NEW.token_used<0 OR NEW.effective_token_used<0
            BEGIN SELECT RAISE(ABORT, 'invalid task run values'); END;

CREATE TRIGGER validate_conversation_insert BEFORE INSERT ON task_conversations
            WHEN NEW.role NOT IN ({sql_values(RUN_TYPES)},'source') OR trim(NEW.thread_id)=''
              OR (NEW.run_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role))
            BEGIN SELECT RAISE(ABORT, 'invalid conversation values'); END;

CREATE TRIGGER validate_conversation_update BEFORE UPDATE ON task_conversations
            WHEN NEW.role NOT IN ({sql_values(RUN_TYPES)},'source') OR trim(NEW.thread_id)=''
              OR (NEW.run_id IS NOT NULL AND NOT EXISTS(
                   SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role))
            BEGIN SELECT RAISE(ABORT, 'invalid conversation values'); END;

CREATE TRIGGER validate_run_conversation_insert BEFORE INSERT ON task_run_conversations
            WHEN NEW.role NOT IN ({sql_values(RUN_TYPES)}) OR trim(NEW.thread_id)=''
              OR NOT EXISTS(SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role)
            BEGIN SELECT RAISE(ABORT, 'invalid run conversation values'); END;

CREATE TRIGGER validate_run_conversation_update BEFORE UPDATE ON task_run_conversations
            WHEN NEW.role NOT IN ({sql_values(RUN_TYPES)}) OR trim(NEW.thread_id)=''
              OR NOT EXISTS(SELECT 1 FROM task_runs r WHERE r.id=NEW.run_id AND r.task_id=NEW.task_id AND r.run_type=NEW.role)
            BEGIN SELECT RAISE(ABORT, 'invalid run conversation values'); END;

CREATE TRIGGER enforce_native_dispatch_active_worker_insert
            BEFORE INSERT ON native_dispatches
            WHEN NEW.status IN ('claimed','pending_thread','bound')
              AND EXISTS(
                SELECT 1 FROM native_dispatches active
                WHERE active.worker_id=NEW.worker_id
                  AND active.status IN ('claimed','pending_thread','bound')
              )
            BEGIN
              SELECT RAISE(ABORT, 'active native dispatch already exists for worker');
            END;

CREATE TRIGGER enforce_native_dispatch_active_worker_update
            BEFORE UPDATE OF worker_id, status ON native_dispatches
            WHEN NEW.status IN ('claimed','pending_thread','bound')
              AND EXISTS(
                SELECT 1 FROM native_dispatches active
                WHERE active.worker_id=NEW.worker_id
                  AND active.status IN ('claimed','pending_thread','bound')
                  AND active.run_id!=NEW.run_id
              )
            BEGIN
              SELECT RAISE(ABORT, 'active native dispatch already exists for worker');
            END;

CREATE TRIGGER validate_native_dispatch_attempt_insert
            BEFORE INSERT ON native_dispatches
            WHEN trim(NEW.dispatch_attempt_id)=''
            BEGIN
              SELECT RAISE(ABORT, 'dispatch_attempt_id is required');
            END;

CREATE TRIGGER validate_native_dispatch_attempt_update
            BEFORE UPDATE OF dispatch_attempt_id ON native_dispatches
            WHEN trim(NEW.dispatch_attempt_id)=''
            BEGIN
              SELECT RAISE(ABORT, 'dispatch_attempt_id is required');
            END;

CREATE TRIGGER track_task_status_started_at AFTER UPDATE OF status ON tasks
            WHEN OLD.status != NEW.status
            BEGIN
              UPDATE tasks SET status_started_at=CURRENT_TIMESTAMP, updated_at=CURRENT_TIMESTAMP
              WHERE id=NEW.id;
            END;

CREATE TRIGGER track_run_stage_completed_at AFTER UPDATE OF status ON task_runs
            WHEN OLD.status IN ({sql_values(ACTIVE_RUN_STATUSES)})
              AND NEW.status NOT IN ({sql_values(ACTIVE_RUN_STATUSES)})
            BEGIN
              UPDATE task_runs SET stage_completed_at=COALESCE(stage_completed_at, CURRENT_TIMESTAMP)
              WHERE id=NEW.id;
            END;

CREATE TRIGGER schedule_on_task_insert AFTER INSERT ON tasks
            BEGIN
              UPDATE scheduler_state SET generation=generation+1, pending=1,
                last_trigger='task_created', last_entity_type='task',
                last_entity_id=NEW.id, updated_at=CURRENT_TIMESTAMP WHERE id=1;
            END;

CREATE TRIGGER schedule_on_task_update
            AFTER UPDATE OF status, auto_dispatch, active_run_id ON tasks
            WHEN OLD.status!=NEW.status
              OR OLD.auto_dispatch!=NEW.auto_dispatch
              OR COALESCE(OLD.active_run_id,'')!=COALESCE(NEW.active_run_id,'')
            BEGIN
              UPDATE scheduler_state SET generation=generation+1, pending=1,
                last_trigger='task_state_changed', last_entity_type='task',
                last_entity_id=NEW.id, updated_at=CURRENT_TIMESTAMP WHERE id=1;
            END;

CREATE TRIGGER schedule_on_run_update AFTER UPDATE OF status ON task_runs
            WHEN OLD.status!=NEW.status
            BEGIN
              UPDATE scheduler_state SET generation=generation+1, pending=1,
                last_trigger='run_state_changed', last_entity_type='run',
                last_entity_id=NEW.id, updated_at=CURRENT_TIMESTAMP WHERE id=1;
            END;

CREATE TRIGGER schedule_on_requirement_insert AFTER INSERT ON requirements
            BEGIN
              UPDATE scheduler_state SET generation=generation+1, pending=1,
                last_trigger='requirement_created', last_entity_type='requirement',
                last_entity_id=NEW.id, updated_at=CURRENT_TIMESTAMP WHERE id=1;
            END;

CREATE TRIGGER schedule_on_requirement_update
            AFTER UPDATE OF status, auto_dispatch ON requirements
            WHEN OLD.status!=NEW.status OR OLD.auto_dispatch!=NEW.auto_dispatch
            BEGIN
              UPDATE scheduler_state SET generation=generation+1, pending=1,
                last_trigger='requirement_state_changed', last_entity_type='requirement',
                last_entity_id=NEW.id, updated_at=CURRENT_TIMESTAMP WHERE id=1;
            END;

CREATE TRIGGER schedule_on_decomposition_run_update
            AFTER UPDATE OF status ON requirement_decomposition_runs
            WHEN OLD.status!=NEW.status
            BEGIN
              UPDATE scheduler_state SET generation=generation+1, pending=1,
                last_trigger='decomposition_run_state_changed',
                last_entity_type='requirement_run', last_entity_id=NEW.id,
                updated_at=CURRENT_TIMESTAMP WHERE id=1;
            END;

INSERT INTO scheduler_state(id) VALUES(1);
INSERT INTO system_settings(key,value) VALUES
 ('dispatcher_enabled','0'),('max_batch_appended_tasks','3'),
 ('parallel_development_enabled','0'),('max_parallel_development','2');
"""

SCHEMA_VERSION = 23


class Database:
    def __init__(self, path: str | Path):
        self._atomic_connection = ContextVar('dotasks_atomic_connection', default=None)
        self._commit_callbacks = ContextVar('dotasks_commit_callbacks', default=None)
        self.path = Path(path).expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".migrate.lock")
        with lock_path.open("a+") as lock_handle:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
            try:
                with self.connection() as connection:
                    version = int(connection.execute("PRAGMA user_version").fetchone()[0])
                    if version == SCHEMA_VERSION:
                        return
                    populated = connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' LIMIT 1").fetchone()
                    if version != 0 or populated:
                        raise ValueError('数据库不是当前结构；请为未上线版本使用新的数据目录，原数据库保持不变')
                    connection.executescript('PRAGMA journal_mode=WAL;\nBEGIN IMMEDIATE;\n' + SCHEMA)
                    connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
                    connection.commit()
            finally:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        """Yield a read connection and always close its file descriptors."""
        active = self._atomic_connection.get()
        if active is not None:
            yield active
            return
        connection = self.connect()
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        active = self._atomic_connection.get()
        if active is not None:
            # Team HTTP units combine core writes and their idempotent response.
            import uuid
            savepoint = 'nested_' + uuid.uuid4().hex
            callbacks = self._commit_callbacks.get()
            callback_count = len(callbacks) if callbacks is not None else 0
            active.execute('SAVEPOINT ' + savepoint)
            try:
                yield active
                active.execute('RELEASE ' + savepoint)
            except Exception:
                if callbacks is not None: del callbacks[callback_count:]
                active.execute('ROLLBACK TO ' + savepoint)
                active.execute('RELEASE ' + savepoint)
                raise
            return
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

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Opt-in request unit; normal personal-service transaction behavior is unchanged."""
        if self._atomic_connection.get() is not None:
            with self.transaction() as connection:
                yield connection
            return
        callbacks = []
        with self.transaction() as connection:
            callback_token = self._commit_callbacks.set(callbacks)
            token = self._atomic_connection.set(connection)
            try:
                yield connection
            finally:
                self._atomic_connection.reset(token)
                self._commit_callbacks.reset(callback_token)
        for callback in callbacks:
            try:
                callback()
            except Exception:
                logging.getLogger(__name__).exception("Post-commit integration failed; committed business state is unchanged")

    def defer_until_commit(self, callback) -> bool:
        callbacks = self._commit_callbacks.get()
        if callbacks is None:
            return False
        if callback not in callbacks:
            callbacks.append(callback)
        return True

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
