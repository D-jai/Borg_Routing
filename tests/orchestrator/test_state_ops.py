"""Tests for src/orchestrator/state_ops.py.

TC-SOP-001 — op_filter keeps matching items
TC-SOP-002 — op_filter with negate keeps non-matching items
TC-SOP-003 — op_count returns total count
TC-SOP-004 — op_count with group_by returns grouped counts
TC-SOP-005 — op_group_by groups items by key
TC-SOP-006 — op_sample returns n items
TC-SOP-007 — op_sample with seed is deterministic
TC-SOP-008 — BRT-SOP-001 raised for unknown operator
TC-SOP-009 — BRT-SOP-002 raised when required param missing
TC-SOP-010 — BRT-SOP-003 raised when items not in dependency_input
TC-SOP-011 — BRT-SOP-005 raised when group_by key missing from item
TC-SOP-012 — dispatch_state_op returns resolved InboundEnvelope
"""
import uuid

import pytest

from src.orchestrator.state_ops import (
    dispatch_state_op,
    op_count,
    op_filter,
    op_group_by,
    op_sample,
)
from src.envelopes.schema import WrappedTask

ITEMS = [
    {"bucket": "bug", "id": "i1"},
    {"bucket": "feature", "id": "i2"},
    {"bucket": "bug", "id": "i3"},
    {"bucket": "question", "id": "i4"},
    {"bucket": "bug", "id": "i5"},
]


def _state_op_task(op: str, items: list = None, params: dict = None) -> WrappedTask:
    return WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="state_op",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        instruction=f"Apply {op} operator.",
        output_schema={"result": "any"},
        dependency_input={
            "op": op,
            "items": items if items is not None else ITEMS,
            "params": params or {},
        },
    )


@pytest.mark.asyncio
async def test_op_filter_keeps_matching():
    """TC-SOP-001 — filter returns only matching items."""
    result = await op_filter(ITEMS, {"key": "bucket", "value": "bug"})
    assert len(result) == 3
    assert all(r["bucket"] == "bug" for r in result)


@pytest.mark.asyncio
async def test_op_filter_negate():
    """TC-SOP-002 — filter with negate keeps non-matching."""
    result = await op_filter(ITEMS, {"key": "bucket", "value": "bug", "negate": True})
    assert len(result) == 2
    assert all(r["bucket"] != "bug" for r in result)


@pytest.mark.asyncio
async def test_op_count_total():
    """TC-SOP-003 — count without group_by returns total."""
    result = await op_count(ITEMS, {})
    assert result == {"count": 5}


@pytest.mark.asyncio
async def test_op_count_grouped():
    """TC-SOP-004 — count with group_by returns per-value counts."""
    result = await op_count(ITEMS, {"group_by": "bucket"})
    assert result["bug"] == 3
    assert result["feature"] == 1
    assert result["question"] == 1


@pytest.mark.asyncio
async def test_op_group_by():
    """TC-SOP-005 — group_by partitions items by key value."""
    result = await op_group_by(ITEMS, {"key": "bucket"})
    assert len(result["bug"]) == 3
    assert len(result["feature"]) == 1


@pytest.mark.asyncio
async def test_op_sample_count():
    """TC-SOP-006 — sample returns exactly n items (or all if n > len)."""
    result = await op_sample(ITEMS, {"n": 2})
    assert len(result) == 2
    result_all = await op_sample(ITEMS, {"n": 100})
    assert len(result_all) == 5


@pytest.mark.asyncio
async def test_op_sample_deterministic():
    """TC-SOP-007 — sample with same seed produces same result."""
    r1 = await op_sample(ITEMS, {"n": 3, "seed": 42})
    r2 = await op_sample(ITEMS, {"n": 3, "seed": 42})
    assert [i["id"] for i in r1] == [i["id"] for i in r2]


@pytest.mark.asyncio
async def test_unknown_operator_raises():
    """TC-SOP-008 — BRT-SOP-001 raised for unknown operator."""
    task = _state_op_task("unknown_op")
    with pytest.raises(ValueError, match="BRT-SOP-001"):
        await dispatch_state_op(task)


@pytest.mark.asyncio
async def test_missing_filter_key_param_raises():
    """TC-SOP-009 — BRT-SOP-002 raised when filter missing 'key'."""
    with pytest.raises(ValueError, match="BRT-SOP-002"):
        await op_filter(ITEMS, {"value": "bug"})


@pytest.mark.asyncio
async def test_missing_items_raises():
    """TC-SOP-010 — BRT-SOP-003 raised when items not in dependency_input."""
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="state_op",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        instruction="op",
        output_schema={},
        dependency_input={"op": "count", "params": {}},
    )
    with pytest.raises(ValueError, match="BRT-SOP-003"):
        await dispatch_state_op(task)


@pytest.mark.asyncio
async def test_group_by_missing_key_raises():
    """TC-SOP-011 — BRT-SOP-005 raised when group_by key absent from item."""
    bad_items = [{"no_bucket": "x"}]
    with pytest.raises(ValueError, match="BRT-SOP-005"):
        await op_group_by(bad_items, {"key": "bucket"})


@pytest.mark.asyncio
async def test_dispatch_returns_resolved_envelope():
    """TC-SOP-012 — dispatch_state_op returns resolved InboundEnvelope."""
    task = _state_op_task("count")
    env = await dispatch_state_op(task)
    assert env.status == "resolved"
    assert env.output["result"]["count"] == 5
