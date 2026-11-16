"""Database schema — all SQLite tables.

Tables: runs, subtasks, envelopes, scratch, logs, log_payloads,
        skill_usage_log, config, threads, messages, run_digests,
        log_entities, log_relations.

Call `create_all(db_path)` on startup to ensure all tables exist.

Error codes:
- BRT-DB-001: database connection failure
- BRT-DB-002: schema creation failure
- BRT-DB-003: constraint violation (FK, NOT NULL, etc.)
"""
import aiosqlite

DB_PATH = "data/borg_route.db"

# Migrations applied to existing databases (safe to re-run — errors silently ignored)
_MIGRATIONS = [
    "ALTER TABLE runs ADD COLUMN thread_id TEXT",
    "ALTER TABLE runs ADD COLUMN user_rating INTEGER",
    "ALTER TABLE log_payloads ADD COLUMN ner_processed BOOLEAN DEFAULT 0",
    # BRT-PIP-010 — surface run failure reason in the runs table so the
    # API and UI can show why a run failed. NULL for non-failed runs.
    "ALTER TABLE runs ADD COLUMN failure_reason TEXT",
    # BRT-LOG-DB-COMPAT — additive fields that make BORG_ROUTE's logs
    # ingestible by The_Borg_DB without breaking any existing call sites.
    # Every column has a safe default; old code paths keep working.
    "ALTER TABLE runs ADD COLUMN instance_id TEXT",
    "ALTER TABLE logs ADD COLUMN instance_id TEXT",
    "ALTER TABLE logs ADD COLUMN source_version TEXT",
    "ALTER TABLE logs ADD COLUMN domain_profile TEXT DEFAULT 'general'",
    "ALTER TABLE log_entities ADD COLUMN instance_id TEXT",
    "ALTER TABLE log_entities ADD COLUMN confidence REAL DEFAULT 1.0",
    "ALTER TABLE log_entities ADD COLUMN provenance TEXT",
    "ALTER TABLE log_relations ADD COLUMN instance_id TEXT",
    "ALTER TABLE log_relations ADD COLUMN confidence REAL DEFAULT 1.0",
    "ALTER TABLE log_relations ADD COLUMN provenance TEXT",
    # Work order 1.6 — what the same tokens would have cost on the baseline model.
    "ALTER TABLE logs ADD COLUMN baseline_dollars NUMERIC",
    "ALTER TABLE envelopes ADD COLUMN baseline_dollars NUMERIC",
]

_SCHEMA_SQL = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,
    thread_id TEXT,
    status TEXT NOT NULL CHECK(status IN ('pending','running','completed','failed')),
    user_instruction TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    user_rating INTEGER,
    failure_reason TEXT
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
    completed_at TIMESTAMP,
    baseline_dollars NUMERIC
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
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    baseline_dollars NUMERIC
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

CREATE TABLE IF NOT EXISTS threads (
    thread_id TEXT PRIMARY KEY,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS messages (
    message_id TEXT PRIMARY KEY,
    thread_id TEXT NOT NULL REFERENCES threads(thread_id),
    run_id TEXT REFERENCES runs(run_id),
    role TEXT NOT NULL CHECK(role IN ('user','assistant','system')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, created_at);

CREATE TABLE IF NOT EXISTS run_digests (
    run_id TEXT PRIMARY KEY REFERENCES runs(run_id),
    summary TEXT NOT NULL,
    cost_dollars REAL DEFAULT 0,
    token_count INTEGER DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS log_entities (
    entity_id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL REFERENCES logs(log_id),
    run_id TEXT,
    text TEXT NOT NULL,
    label TEXT NOT NULL,
    start_char INTEGER,
    end_char INTEGER,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_log_entities_run ON log_entities(run_id);
CREATE INDEX IF NOT EXISTS idx_log_entities_label ON log_entities(label);

CREATE TABLE IF NOT EXISTS log_relations (
    relation_id INTEGER PRIMARY KEY AUTOINCREMENT,
    log_id INTEGER NOT NULL REFERENCES logs(log_id),
    run_id TEXT,
    subject TEXT NOT NULL,
    relation TEXT NOT NULL,
    object TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_log_relations_run ON log_relations(run_id);

-- BRT-LOG-DB-COMPAT — incremental export cursor for The_Borg_DB.
-- One row per consumer (e.g., 'borg_db:default'); tracks the last log /
-- entity / relation rowid each consumer has ingested. DB updates the
-- cursor on each successful pull; ROUTE never writes to this table.
CREATE TABLE IF NOT EXISTS borg_db_export_cursor (
    consumer_id TEXT PRIMARY KEY,
    last_log_id INTEGER NOT NULL DEFAULT 0,
    last_entity_id INTEGER NOT NULL DEFAULT 0,
    last_relation_id INTEGER NOT NULL DEFAULT 0,
    last_synced_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- BRT-LOG-DB-COMPAT — denormalized read view that DB can pull from with
-- a single SELECT. Joins logs + runs + log_payloads(in/out) so The_Borg_DB
-- ingestion code doesn't need to know our internal schema.
CREATE VIEW IF NOT EXISTS borg_db_interactions AS
SELECT
    l.log_id,
    l.run_id,
    r.thread_id,
    l.envelope_id,
    l.event_type,
    l.model_id,
    l.tokens_input,
    l.tokens_output,
    l.dollars,
    l.latency_ms,
    l.decision_json,
    l.created_at,
    l.instance_id,
    l.source_version,
    l.domain_profile,
    r.user_instruction,
    r.status AS run_status,
    p_in.text  AS payload_in,
    p_out.text AS payload_out
FROM logs l
LEFT JOIN runs r ON r.run_id = l.run_id
LEFT JOIN log_payloads p_in  ON p_in.log_id  = l.log_id AND p_in.direction  = 'in'
LEFT JOIN log_payloads p_out ON p_out.log_id = l.log_id AND p_out.direction = 'out';
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
            # Migrations for existing databases
            for migration in _MIGRATIONS:
                try:
                    await db.execute(migration)
                    await db.commit()
                except Exception:
                    pass  # Column/index already exists — safe to skip
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(
            f"BRT-DB-001: database connection failed for '{db_path}': {exc}"
        ) from exc


async def drop_all(db_path: str = DB_PATH) -> None:
    """Drop all application tables. Used in tests only."""
    tables = [
        "log_relations", "log_entities", "log_payloads", "skill_usage_log", "logs",
        "scratch", "envelopes", "subtasks", "run_digests", "runs", "config",
        "messages", "threads", "borg_db_export_cursor",
    ]
    async with aiosqlite.connect(db_path) as db:
        await db.execute("DROP VIEW IF EXISTS borg_db_interactions")
        for table in tables:
            await db.execute(f"DROP TABLE IF EXISTS {table}")
        await db.commit()
