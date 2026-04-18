"""Tests for src/logging/writer.py.

TC-LOG-001 — write_event lands in logs table
TC-LOG-002 — payload_in lands in log_payloads with direction='in'
TC-LOG-003 — payload_out lands in log_payloads with direction='out'
TC-LOG-004 — BRT-LOG-001 raised for invalid event_type
TC-LOG-005 — decision dict is serialised to JSON in decision_json column
TC-LOG-006 — write_event with no optional fields succeeds
TC-LOG-007 — all valid event_types are accepted
"""
import asyncio
import uuid

import aiosqlite
import pytest

from src.db.schema import create_all
from src.logging.writer import VALID_EVENT_TYPES, write_event


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "log_test.db")
    await create_all(path)
    return path


async def _drain(db_path: str) -> tuple[list[dict], list[dict]]:
    """Drain all pending tasks then return logs and log_payloads."""
    # Yield multiple times to let asyncio tasks (fire-and-forget) complete
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM logs ORDER BY log_id") as cur:
            logs = [dict(r) for r in await cur.fetchall()]
        async with db.execute("SELECT * FROM log_payloads ORDER BY payload_id") as cur:
            payloads = [dict(r) for r in await cur.fetchall()]
    return logs, payloads


@pytest.mark.asyncio
async def test_write_event_lands_in_logs(db):
    """TC-LOG-001 — write_event schedules a task that persists to logs."""
    run_id = uuid.uuid4()
    await write_event("model_call", run_id=run_id, model_id="test-model",
                      tokens_input=100, tokens_output=50, dollars=0.001,
                      latency_ms=200, db_path=db)
    logs, _ = await _drain(db)
    assert len(logs) == 1
    assert logs[0]["event_type"] == "model_call"
    assert logs[0]["run_id"] == str(run_id)
    assert logs[0]["model_id"] == "test-model"
    assert logs[0]["tokens_input"] == 100


@pytest.mark.asyncio
async def test_payload_in_stored(db):
    """TC-LOG-002 — payload_in persisted with direction='in'."""
    await write_event("model_call", payload_in="Hello model", db_path=db)
    _, payloads = await _drain(db)
    assert any(p["direction"] == "in" and p["text"] == "Hello model" for p in payloads)


@pytest.mark.asyncio
async def test_payload_out_stored(db):
    """TC-LOG-003 — payload_out persisted with direction='out'."""
    await write_event("model_call", payload_out='{"bucket":"bug"}', db_path=db)
    _, payloads = await _drain(db)
    assert any(p["direction"] == "out" for p in payloads)


@pytest.mark.asyncio
async def test_invalid_event_type_raises(db):
    """TC-LOG-004 — BRT-LOG-001 raised synchronously for unknown event_type."""
    with pytest.raises(ValueError, match="BRT-LOG-001"):
        await write_event("unknown_event", db_path=db)


@pytest.mark.asyncio
async def test_decision_dict_serialised(db):
    """TC-LOG-005 — decision dict stored as JSON string."""
    decision = {"plan": "direct", "rationale": "short question"}
    await write_event("orchestrator_decision", decision=decision, db_path=db)
    logs, _ = await _drain(db)
    import json
    stored = json.loads(logs[0]["decision_json"])
    assert stored["plan"] == "direct"


@pytest.mark.asyncio
async def test_minimal_write_event(db):
    """TC-LOG-006 — write_event with only event_type succeeds."""
    await write_event("error", db_path=db)
    logs, _ = await _drain(db)
    assert len(logs) == 1
    assert logs[0]["event_type"] == "error"
    assert logs[0]["run_id"] is None


@pytest.mark.asyncio
async def test_all_valid_event_types_accepted(db):
    """TC-LOG-007 — all seven valid event_types are accepted."""
    for et in sorted(VALID_EVENT_TYPES):
        await write_event(et, db_path=db)
    logs, _ = await _drain(db)
    found_types = {r["event_type"] for r in logs}
    assert found_types == VALID_EVENT_TYPES
