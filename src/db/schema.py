"""Database schema — all SQLite tables for Slice 2.

Tables: runs, subtasks, envelopes, scratch, logs, log_payloads,
        skill_usage_log, config.

Call `create_all(db_path)` on startup to ensure all tables exist.

Error codes:
- BRT-DB-001: database connection failure
- BRT-DB-002: schema creation failure
- BRT-DB-003: constraint violation (FK, NOT NULL, etc.)
"""
import aiosqlite

DB_PATH = "data/borg_route.db"

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    user_instruction TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    user_rating INTEGER
);

CREATE TABLE IF NOT EXISTS subtasks (
    subtask_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL REFERENCES runs(run_id),
    type TEXT NOT NULL,
    instruction TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending','in_flight','done','failed')),
    output TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS envelopes (
    envelope_id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    subtask_id TEXT NOT NULL,
    type TEXT NOT NULL,
    level_path TEXT NOT NULL,
    model_id TEXT,
    trigger_reason TEXT NOT NULL,
    skill_id TEXT,
    skill_version INTEGER,
    status TEXT NOT NULL,
    outbound_payload TEXT NOT NULL,
    inbound_payload TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    dollars NUMERIC,
    latency_ms INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP
);

CREATE TABLE IF NOT EXISTS scratch (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    level_path TEXT NOT NULL,
    envelope_id TEXT NOT NULL,
    table_name TEXT NOT NULL,
    data TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_scratch_run_level ON scratch(run_id, level_path);

CREATE TABLE IF NOT EXISTS logs (
    log_id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT,
    envelope_id TEXT,
    event_type TEXT NOT NULL CHECK(event_type IN (
        'model_call','orchestrator_decision','child_residue',
        'escalation','deferral','reorchestration','error'
    )),
    model_id TEXT,
    tokens_input INTEGER,
    tokens_output INTEGER,
    dollars NUMERIC,
    latency_ms INTEGER,
    decision_json TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_logs_run ON logs(run_id);
CREATE INDEX IF NOT EXISTS idx_logs_event_type ON logs(event_type);

CREATE TABLE IF NOT EXISTS log_payloads (
    payload_id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL REFERENCES logs(log_id),
    direction TEXT NOT NULL CHECK(direction IN ('in','out')),
    text TEXT NOT NULL,
    ner_processed BOOLEAN DEFAULT 0
);

CREATE TABLE IF NOT EXISTS skill_usage_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    skill_id TEXT NOT NULL,
    skill_version INTEGER NOT NULL,
    run_id TEXT,
    envelope_id TEXT,
    outcome TEXT NOT NULL CHECK(outcome IN ('success','partial','failure')),
    tokens_used INTEGER,
    latency_ms INTEGER,
    composer_accepted BOOLEAN,
    child_residue_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS config (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
"""


async def create_all(db_path: str = DB_PATH) -> None:
    """Execute all CREATE TABLE IF NOT EXISTS statements.

    Idempotent — safe to call on every startup.

    Raises:
        RuntimeError: BRT-DB-001 if connection fails.
        RuntimeError: BRT-DB-002 if schema creation fails.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            try:
                await db.executescript(_SCHEMA_SQL)
                await db.commit()
            except Exception as exc:
                raise RuntimeError(
                    f"BRT-DB-002: schema creation failed: {exc}"
                ) from exc
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"BRT-DB-001: database connection failed for '{db_path}': {exc}"
        ) from exc


async def drop_all(db_path: str = DB_PATH) -> None:
    """Drop all application tables. Used in tests only."""
    tables = [
        "log_payloads", "skill_usage_log", "logs",
        "scratch", "envelopes", "subtasks", "runs", "config",
    ]
    async with aiosqlite.connect(db_path) as db:
        for table in tables:
            await db.execute(f"DROP TABLE IF EXISTS {table}")
        await db.commit()
