"""Tests for src/db/state.py.

TC-DB-020 — create_run returns a UUID and record is persisted
TC-DB-021 — update_run_status sets status and completed_at
TC-DB-022 — BRT-DB-010 raised by update_run_status for unknown run
TC-DB-023 — BRT-DB-012 raised for invalid status value
TC-DB-024 — save_envelope inserts record with status=in_flight
TC-DB-025 — update_envelope_inbound marks envelope completed
TC-DB-026 — BRT-DB-011 raised by update_envelope_inbound for unknown envelope
TC-DB-027 — get_run returns dict with correct fields
TC-DB-028 — get_envelopes returns ordered list
TC-DB-029 — get_config returns None for missing key
TC-DB-030 — set_config + get_config roundtrip for str and dict values
"""
import uuid

import pytest

from src.db.schema import create_all
from src.db.state import (
    create_run,
    get_config,
    get_envelopes,
    get_run,
    save_envelope,
    set_config,
    update_envelope_inbound,
    update_run_status,
)
from src.envelopes.schema import InboundEnvelope, WrappedTask


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "state_test.db")
    await create_all(path)
    return path


def _task(run_id: uuid.UUID, db_path: str, **kwargs) -> WrappedTask:
    return WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=run_id,
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        instruction="Classify this email.",
        output_schema={"bucket": "bug|feature|question"},
        **kwargs,
    )


@pytest.mark.asyncio
async def test_create_run_persisted(db):
    """TC-DB-020 — create_run returns UUID and record exists."""
    run_id = await create_run("Test instruction", db_path=db)
    assert isinstance(run_id, uuid.UUID)
    row = await get_run(run_id, db_path=db)
    assert row["status"] == "pending"
    assert row["user_instruction"] == "Test instruction"


@pytest.mark.asyncio
async def test_update_run_status(db):
    """TC-DB-021 — update_run_status transitions to running then completed."""
    run_id = await create_run("instruction", db_path=db)
    await update_run_status(run_id, "running", db_path=db)
    row = await get_run(run_id, db_path=db)
    assert row["status"] == "running"
    assert row["completed_at"] is None

    await update_run_status(run_id, "completed", db_path=db)
    row = await get_run(run_id, db_path=db)
    assert row["status"] == "completed"
    assert row["completed_at"] is not None


@pytest.mark.asyncio
async def test_update_run_status_unknown_raises(db):
    """TC-DB-022 — BRT-DB-010 raised for unknown run_id."""
    with pytest.raises(ValueError, match="BRT-DB-010"):
        await update_run_status(uuid.uuid4(), "running", db_path=db)


@pytest.mark.asyncio
async def test_update_run_status_invalid_value_raises(db):
    """TC-DB-023 — BRT-DB-012 raised for invalid status."""
    run_id = await create_run("x", db_path=db)
    with pytest.raises(ValueError, match="BRT-DB-012"):
        await update_run_status(run_id, "borked", db_path=db)


@pytest.mark.asyncio
async def test_save_envelope_in_flight(db):
    """TC-DB-024 — save_envelope inserts with status=in_flight."""
    run_id = await create_run("instruction", db_path=db)
    task = _task(run_id, db)
    await save_envelope(task, db_path=db)
    envs = await get_envelopes(run_id, db_path=db)
    assert len(envs) == 1
    assert envs[0]["status"] == "in_flight"
    assert envs[0]["envelope_id"] == str(task.envelope_id)


@pytest.mark.asyncio
async def test_update_envelope_inbound(db):
    """TC-DB-025 — update_envelope_inbound marks envelope completed."""
    run_id = await create_run("instruction", db_path=db)
    task = _task(run_id, db)
    await save_envelope(task, db_path=db)

    env = InboundEnvelope(
        envelope_id=task.envelope_id,
        subtask_id=task.subtask_id,
        status="resolved",
        output={"bucket": "bug"},
    )
    await update_envelope_inbound(
        task.envelope_id, env, tokens_input=100, tokens_output=20,
        dollars=0.001, latency_ms=500, db_path=db
    )
    envs = await get_envelopes(run_id, db_path=db)
    assert envs[0]["status"] == "resolved"
    assert envs[0]["tokens_input"] == 100
    assert envs[0]["completed_at"] is not None


@pytest.mark.asyncio
async def test_update_envelope_inbound_unknown_raises(db):
    """TC-DB-026 — BRT-DB-011 raised for unknown envelope_id."""
    env = InboundEnvelope(
        envelope_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        status="resolved",
        output={},
    )
    with pytest.raises(ValueError, match="BRT-DB-011"):
        await update_envelope_inbound(uuid.uuid4(), env, db_path=db)


@pytest.mark.asyncio
async def test_get_run_not_found(db):
    """TC-DB-027 — BRT-DB-010 raised by get_run for unknown id."""
    with pytest.raises(ValueError, match="BRT-DB-010"):
        await get_run(uuid.uuid4(), db_path=db)


@pytest.mark.asyncio
async def test_get_envelopes_ordered(db):
    """TC-DB-028 — get_envelopes returns list ordered by created_at."""
    run_id = await create_run("instruction", db_path=db)
    for _ in range(3):
        task = _task(run_id, db)
        await save_envelope(task, db_path=db)
    envs = await get_envelopes(run_id, db_path=db)
    assert len(envs) == 3


@pytest.mark.asyncio
async def test_get_config_missing_returns_none(db):
    """TC-DB-029 — get_config returns None for missing key."""
    val = await get_config("nonexistent_key", db_path=db)
    assert val is None


@pytest.mark.asyncio
async def test_set_and_get_config_roundtrip(db):
    """TC-DB-030 — set_config + get_config roundtrip for str and dict."""
    await set_config("global_rules", "No PII in outputs.", db_path=db)
    val = await get_config("global_rules", db_path=db)
    assert val == "No PII in outputs."

    rules = [{"id": 1, "text": "rule A", "added_at": "2026-01-01", "retired_at": None}]
    await set_config("admin_rules_ledger", rules, db_path=db)
    loaded = await get_config("admin_rules_ledger", db_path=db)
    assert loaded[0]["text"] == "rule A"
