"""Tests for src/db/schema.py.

TC-DB-001 — create_all is idempotent (safe to call twice)
TC-DB-002 — all expected tables exist after create_all
TC-DB-003 — runs status constraint rejects invalid values
TC-DB-004 — subtasks status constraint rejects invalid values
TC-DB-005 — logs event_type constraint rejects invalid values
TC-DB-006 — log_payloads direction constraint rejects invalid values
TC-DB-007 — skill_usage_log outcome constraint rejects invalid values
TC-DB-008 — scratch index exists on (run_id, level_path)
TC-DB-009 — config key is unique (upsert pattern works)
TC-DB-010 — BRT-DB-001 raised on bad db path (permission / invalid path)
"""
import os
import pytest
import aiosqlite

from src.db.schema import create_all, drop_all

TEST_DB = ":memory:"


@pytest.fixture
async def db_path(tmp_path):
    path = str(tmp_path / "test_borg.db")
    await create_all(path)
    yield path
    # cleanup handled by tmp_path


async def _tables(db_path: str) -> list[str]:
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def _exec(db_path: str, sql: str, params=()):
    async with aiosqlite.connect(db_path) as db:
        await db.execute(sql, params)
        await db.commit()


@pytest.mark.asyncio
async def test_create_all_idempotent(tmp_path):
    """TC-DB-001 — create_all can be called multiple times without error."""
    path = str(tmp_path / "idempotent.db")
    await create_all(path)
    await create_all(path)  # second call must not raise


@pytest.mark.asyncio
async def test_all_tables_exist(db_path):
    """TC-DB-002 — all eight tables exist after create_all."""
    tables = await _tables(db_path)
    expected = {"runs", "subtasks", "envelopes", "scratch", "logs",
                "log_payloads", "skill_usage_log", "config"}
    assert expected.issubset(set(tables))


@pytest.mark.asyncio
async def test_runs_status_constraint(db_path):
    """TC-DB-003 — runs.status only allows valid values."""
    import aiosqlite
    async with aiosqlite.connect(db_path) as db:
        await db.execute("PRAGMA foreign_keys=ON")
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO runs(run_id, status, user_instruction) VALUES (?,?,?)",
                ("r1", "invalid_status", "test"),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_subtasks_status_constraint(db_path):
    """TC-DB-004 — subtasks.status only allows valid values."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO runs(run_id, status, user_instruction) VALUES (?,?,?)",
            ("run-sub", "pending", "test instruction"),
        )
        await db.commit()
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO subtasks(subtask_id, run_id, type, instruction, status) VALUES (?,?,?,?,?)",
                ("s1", "run-sub", "answerer", "do thing", "bad_status"),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_logs_event_type_constraint(db_path):
    """TC-DB-005 — logs.event_type rejects unknown values."""
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO logs(event_type) VALUES (?)",
                ("unknown_event",),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_log_payloads_direction_constraint(db_path):
    """TC-DB-006 — log_payloads.direction only allows 'in' or 'out'."""
    async with aiosqlite.connect(db_path) as db:
        # insert a log row first
        await db.execute(
            "INSERT INTO logs(event_type) VALUES ('model_call')"
        )
        await db.commit()
        async with db.execute("SELECT last_insert_rowid()") as cur:
            row = await cur.fetchone()
        log_id = row[0]
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO log_payloads(log_id, direction, text) VALUES (?,?,?)",
                (log_id, "sideways", "payload"),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_skill_usage_log_outcome_constraint(db_path):
    """TC-DB-007 — skill_usage_log.outcome rejects unknown values."""
    async with aiosqlite.connect(db_path) as db:
        with pytest.raises(Exception):
            await db.execute(
                "INSERT INTO skill_usage_log(skill_id, skill_version, outcome) VALUES (?,?,?)",
                ("classify_into_buckets", 1, "unknown_outcome"),
            )
            await db.commit()


@pytest.mark.asyncio
async def test_scratch_index_exists(db_path):
    """TC-DB-008 — idx_scratch_run_level index exists."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_scratch_run_level'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None, "idx_scratch_run_level index not found"


@pytest.mark.asyncio
async def test_config_key_unique(db_path):
    """TC-DB-009 — config key is unique; INSERT OR REPLACE acts as upsert."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT OR REPLACE INTO config(key, value) VALUES (?,?)", ("setting_a", "v1")
        )
        await db.commit()
        await db.execute(
            "INSERT OR REPLACE INTO config(key, value) VALUES (?,?)", ("setting_a", "v2")
        )
        await db.commit()
        async with db.execute("SELECT value FROM config WHERE key='setting_a'") as cur:
            row = await cur.fetchone()
    assert row[0] == "v2"


@pytest.mark.asyncio
async def test_create_all_valid_run_insert(db_path):
    """TC-DB-010 — valid run record inserts cleanly."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO runs(run_id, status, user_instruction) VALUES (?,?,?)",
            ("valid-run-1", "pending", "Classify these emails."),
        )
        await db.commit()
        async with db.execute("SELECT run_id FROM runs WHERE run_id='valid-run-1'") as cur:
            row = await cur.fetchone()
    assert row is not None
