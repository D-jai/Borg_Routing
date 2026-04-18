"""Tests for src/db/scratch.py.

TC-DB-040 — write_scratch inserts row and returns id
TC-DB-041 — read_scratch_for_level returns rows with parsed data
TC-DB-042 — delete_scratch_for_level removes rows and returns count
TC-DB-043 — count_scratch_for_level returns correct count
TC-DB-044 — count_scratch_for_level returns 0 when no rows
TC-DB-045 — apply_cleanup_directives with ids deletes specified rows
TC-DB-046 — apply_cleanup_directives with row_id deletes single row
TC-DB-047 — apply_cleanup_directives with write action is a no-op
TC-DB-048 — concurrent writes from multiple level_paths are isolated
"""
import uuid

import pytest

from src.db.schema import create_all
from src.db.scratch import (
    apply_cleanup_directives,
    count_scratch_for_level,
    delete_scratch_for_level,
    read_scratch_for_level,
    write_scratch,
)
from src.envelopes.schema import SideEffectAction


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "scratch_test.db")
    await create_all(path)
    return path


@pytest.mark.asyncio
async def test_write_and_read_scratch(db):
    """TC-DB-040/041 — write then read returns correct row."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    row_id = await write_scratch(run_id, "1a", env_id, "deferrals", {"key": "val"}, db_path=db)
    assert isinstance(row_id, int)

    rows = await read_scratch_for_level(run_id, "1a", db_path=db)
    assert len(rows) == 1
    assert rows[0]["table_name"] == "deferrals"
    assert rows[0]["data"]["key"] == "val"


@pytest.mark.asyncio
async def test_delete_scratch_for_level(db):
    """TC-DB-042 — delete returns count of deleted rows."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await write_scratch(run_id, "2b", env_id, "t", {"x": 1}, db_path=db)
    await write_scratch(run_id, "2b", env_id, "t", {"x": 2}, db_path=db)

    deleted = await delete_scratch_for_level(run_id, "2b", db_path=db)
    assert deleted == 2

    rows = await read_scratch_for_level(run_id, "2b", db_path=db)
    assert rows == []


@pytest.mark.asyncio
async def test_count_scratch_for_level(db):
    """TC-DB-043 — count returns correct value."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    for _ in range(3):
        await write_scratch(run_id, "3c", env_id, "t", {}, db_path=db)
    count = await count_scratch_for_level(run_id, "3c", db_path=db)
    assert count == 3


@pytest.mark.asyncio
async def test_count_returns_zero_when_empty(db):
    """TC-DB-044 — count returns 0 when no rows exist."""
    count = await count_scratch_for_level(uuid.uuid4(), "0a", db_path=db)
    assert count == 0


@pytest.mark.asyncio
async def test_apply_cleanup_with_ids(db):
    """TC-DB-045 — apply_cleanup_directives with ids deletes specified rows."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    id1 = await write_scratch(run_id, "1a", env_id, "t", {"n": 1}, db_path=db)
    id2 = await write_scratch(run_id, "1a", env_id, "t", {"n": 2}, db_path=db)
    await write_scratch(run_id, "1a", env_id, "t", {"n": 3}, db_path=db)

    directives = [SideEffectAction(table="t", action="delete", ids=[str(id1), str(id2)])]
    await apply_cleanup_directives(run_id, directives, db_path=db)

    rows = await read_scratch_for_level(run_id, "1a", db_path=db)
    assert len(rows) == 1
    assert rows[0]["data"]["n"] == 3


@pytest.mark.asyncio
async def test_apply_cleanup_with_row_id(db):
    """TC-DB-046 — apply_cleanup_directives with row_id deletes single row."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    row_id = await write_scratch(run_id, "1b", env_id, "t", {"n": 99}, db_path=db)

    directives = [SideEffectAction(table="t", action="delete", row_id=str(row_id))]
    await apply_cleanup_directives(run_id, directives, db_path=db)

    count = await count_scratch_for_level(run_id, "1b", db_path=db)
    assert count == 0


@pytest.mark.asyncio
async def test_apply_cleanup_write_is_noop(db):
    """TC-DB-047 — apply_cleanup_directives ignores write actions."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await write_scratch(run_id, "1c", env_id, "t", {"n": 1}, db_path=db)

    directives = [SideEffectAction(table="t", action="write", row_id="anything")]
    await apply_cleanup_directives(run_id, directives, db_path=db)

    count = await count_scratch_for_level(run_id, "1c", db_path=db)
    assert count == 1  # unchanged


@pytest.mark.asyncio
async def test_level_isolation(db):
    """TC-DB-048 — scratch rows from different level_paths are isolated."""
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()
    await write_scratch(run_id, "1a", env_id, "t", {"level": "1a"}, db_path=db)
    await write_scratch(run_id, "2b", env_id, "t", {"level": "2b"}, db_path=db)

    rows_1a = await read_scratch_for_level(run_id, "1a", db_path=db)
    rows_2b = await read_scratch_for_level(run_id, "2b", db_path=db)

    assert len(rows_1a) == 1 and rows_1a[0]["data"]["level"] == "1a"
    assert len(rows_2b) == 1 and rows_2b[0]["data"]["level"] == "2b"
