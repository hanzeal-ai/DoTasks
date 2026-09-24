"""Additive schema for team approval/version records in the core database.

Execution state stays in tasks/task_runs. These records own only collaboration.
Installed only for a team runtime; personal databases are untouched.
"""

SCHEMA = '''
CREATE TABLE IF NOT EXISTS team_attachments (
    id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL REFERENCES requirements(id),
    name TEXT NOT NULL, mime TEXT NOT NULL, sha256 TEXT NOT NULL, content BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS team_projects (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, repository TEXT NOT NULL,
    baseline TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS team_project_members (
    project_id TEXT NOT NULL REFERENCES team_projects(id), account_id TEXT NOT NULL,
    modules TEXT NOT NULL DEFAULT '[]', capacity INTEGER NOT NULL DEFAULT 1,
    auto_analysis INTEGER NOT NULL DEFAULT 0, token_budget INTEGER NOT NULL DEFAULT 10000,
    PRIMARY KEY(project_id, account_id));
CREATE TABLE IF NOT EXISTS team_requirements (
    requirement_id TEXT PRIMARY KEY REFERENCES requirements(id),
    project_id TEXT NOT NULL REFERENCES team_projects(id), product_id TEXT NOT NULL,
    coordinator_id TEXT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
    submitted_version INTEGER, accepted_version INTEGER,
    integration TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS team_tasks (
    task_id TEXT PRIMARY KEY REFERENCES tasks(id), revision INTEGER NOT NULL DEFAULT 1,
    requirement_version INTEGER NOT NULL, owner_account_id TEXT,
    confirmed_revision INTEGER, delivered_revision INTEGER,
    handling TEXT NOT NULL DEFAULT '', interfaces TEXT NOT NULL DEFAULT '[]',
    analysis TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS team_versions (
    requirement_id TEXT NOT NULL REFERENCES requirements(id), version INTEGER NOT NULL,
    snapshot TEXT NOT NULL, actor_id TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(requirement_id, version));
CREATE TABLE IF NOT EXISTS team_questions (
    id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL REFERENCES requirements(id),
    task_id TEXT REFERENCES tasks(id), version INTEGER NOT NULL, author_id TEXT NOT NULL,
    question TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'awaiting_clarification',
    answer TEXT NOT NULL DEFAULT '', answered_by TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS team_jobs (
    id TEXT PRIMARY KEY, requirement_id TEXT NOT NULL REFERENCES requirements(id),
    task_id TEXT REFERENCES tasks(id), actor_id TEXT NOT NULL, kind TEXT NOT NULL,
    version INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'queued',
    lease TEXT NOT NULL DEFAULT '', expires_at REAL NOT NULL DEFAULT 0,
    thread_id TEXT NOT NULL DEFAULT '', result TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '', token_budget INTEGER NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE UNIQUE INDEX IF NOT EXISTS team_job_generation
    ON team_jobs(requirement_id, COALESCE(task_id,''), kind, version, actor_id);
CREATE TABLE IF NOT EXISTS team_notifications (
    id TEXT PRIMARY KEY, account_id TEXT NOT NULL, requirement_id TEXT NOT NULL,
    message TEXT NOT NULL, read INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS team_requests (
    actor_id TEXT NOT NULL, request_id TEXT NOT NULL, digest TEXT NOT NULL,
    result TEXT NOT NULL, PRIMARY KEY(actor_id, request_id));
CREATE TABLE IF NOT EXISTS team_run_versions (
    run_id TEXT PRIMARY KEY REFERENCES task_runs(id), task_id TEXT NOT NULL,
    revision INTEGER NOT NULL, actor_id TEXT NOT NULL, project_id TEXT NOT NULL);
'''
