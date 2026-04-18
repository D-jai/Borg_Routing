"""Tests for src/orchestrator/fold_in.py.

TC-ORC-010 — fold_in returns empty dict when no residue
TC-ORC-011 — fold_in detects residue, logs child_residue event, cleans up
TC-ORC-012 — fold_in digest contains residue_count and sample_tables
TC-ORC-013 — scratch is empty after fold_in (cleanup confirmed)
TC-ORC-014 — child_residue event logged with correct model_id
"""
import asyncio
import uuid

import aiosqlite
import pytest

from src.db.schema import create_all
from src.db.scratch import count_scratch_for_level, write_scratch
from src.orchestrator.fold_in import fold_in


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "fold_in_test.db")
    await create_all(path)
    return path


@pytest.mark.asyncio
async def test_fold_in_no_residue_returns_empty(db):
    """TC-ORC-010 — no scratch rows → empty dict returned."""
    run_id = uuid.uuid4()
    result = await fold_in(run_id, "1a", db_path=db)
    assert result == {}


@pytest.mark.asyncio
async def test_fold_in_detects_and_cleans_residue(db):
    """TC-ORC-011 — residue found → event logged, scratch cleaned."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await write_scratch(run_id, "2b", env_id, "deferrals", {"note": "leftover"}, db_path=db)
    await write_scratch(run_id, "2b", env_id, "cross_refs", {"ref": "x"}, db_path=db)

    result = await fold_in(run_id, "2b", child_model_id="test-model", db_path=db)

    assert result["residue_found"] is True
    assert result["residue_count"] == 2

    # Scratch must be empty after fold_in
    remaining = await count_scratch_for_level(run_id, "2b", db_path=db)
    assert remaining == 0


@pytest.mark.asyncio
async def test_fold_in_digest_contains_tables(db):
    """TC-ORC-012 — digest contains sample_tables listing affected tables."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await write_scratch(run_id, "3c", env_id, "deferrals", {}, db_path=db)
    await write_scratch(run_id, "3c", env_id, "results", {}, db_path=db)

    result = await fold_in(run_id, "3c", db_path=db)

    assert "deferrals" in result["sample_tables"]
    assert "results" in result["sample_tables"]


@pytest.mark.asyncio
async def test_scratch_empty_after_fold_in(db):
    """TC-ORC-013 — no scratch rows remain after fold_in."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    for i in range(5):
        await write_scratch(run_id, "4d", env_id, "t", {"i": i}, db_path=db)

    await fold_in(run_id, "4d", db_path=db)

    count = await count_scratch_for_level(run_id, "4d", db_path=db)
    assert count == 0


@pytest.mark.asyncio
async def test_child_residue_event_logged(db):
    """TC-ORC-014 — child_residue event logged with correct model_id."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await write_scratch(run_id, "1x", env_id, "scratch", {"data": 1}, db_path=db)

    await fold_in(run_id, "1x", child_model_id="qwen3-coder", db_path=db)

    # Drain async log tasks
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT event_type, model_id FROM logs WHERE event_type='child_residue'"
        ) as cur:
            rows = await cur.fetchall()

    assert len(rows) >= 1
    assert rows[0][1] == "qwen3-coder"
