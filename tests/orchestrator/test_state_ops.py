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


# --- Planner ruling 25 -------------------------------------------------------

APE_ROWS = [
    {"index": 0, "item": "e0", "output": {"bucket": "bug", "topic": "crash"}},
    {"index": 1, "item": "e1", "output": {"bucket": "billing", "topic": "refund"}},
    {"index": 2, "item": "e2", "output": {"bucket": "bug", "topic": "sync"}},
]


@pytest.mark.asyncio
async def test_field_read_from_output_then_row_and_output_prefix_same():
    """25a — 'bucket' and 'output.bucket' read the row's output; 'item' the row."""
    assert len(await op_filter(APE_ROWS, {"key": "bucket", "value": "bug"})) == 2
    kept = await op_filter(APE_ROWS, {"key": "output.bucket", "value": "bug"})
    assert kept == [APE_ROWS[0], APE_ROWS[2]]  # whole rows, full shape
    assert await op_filter(APE_ROWS, {"key": "item", "value": "e1"}) == [APE_ROWS[1]]
    assert await op_count(APE_ROWS, {"key": "bucket"}) == {"bug": 2, "billing": 1}


@pytest.mark.asyncio
async def test_group_by_counts_and_rows():
    """25b/c — group_by takes key or group_by; count: true gives counts."""
    assert await op_group_by(APE_ROWS, {"group_by": "bucket", "count": True}) == {
        "bug": 2, "billing": 1}
    groups = await op_group_by(APE_ROWS, {"key": "bucket"})
    assert groups == {"bug": [APE_ROWS[0], APE_ROWS[2]], "billing": [APE_ROWS[1]]}


@pytest.mark.asyncio
async def test_two_different_grouping_fields_fail():
    with pytest.raises(ValueError, match="BRT-SOP-002.*one field"):
        await op_group_by(APE_ROWS, {"key": "bucket", "group_by": "topic"})


@pytest.mark.asyncio
async def test_sample_per_group_from_field_or_grouped_source():
    """25d — n from each group, one flat list."""
    flat = await op_sample(APE_ROWS, {"n": 1, "key": "bucket", "seed": 1})
    assert len(flat) == 2 and {r["output"]["bucket"] for r in flat} == {"bug", "billing"}
    task = _state_op_task("sample", params={"n": 5})
    task.dependency_input.pop("items")
    task.dependency_input["groups"] = {"bug": [APE_ROWS[0], APE_ROWS[2]],
                                       "billing": [APE_ROWS[1]]}
    env = await dispatch_state_op(task)
    assert env.output["result"] == [APE_ROWS[0], APE_ROWS[2], APE_ROWS[1]]


@pytest.mark.asyncio
async def test_counts_are_not_rows_for_the_next_op():
    """A state_op reading counts per group fails loudly, naming its source."""
    task = _state_op_task("sample", params={"n": 1})
    task.dependency_input.pop("items")
    task.dependency_input["source_ref"] = "n2"
    with pytest.raises(ValueError, match="BRT-SOP-003: source 'n2' gave no rows"):
        await dispatch_state_op(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("op, params, code", [
    ("count", {"field": "bucket"}, "BRT-SOP-006"),
    ("sample", {"n": "three"}, "BRT-SOP-002"),
    ("sample", {"n": -1}, "BRT-SOP-002"),
    ("group_by", "bucket", "BRT-SOP-002"),
])
async def test_bad_params_fail_and_name_the_params(op, params, code):
    task = _state_op_task(op, items=APE_ROWS, params=None)
    task.dependency_input["params"] = params
    with pytest.raises(ValueError) as info:
        await dispatch_state_op(task)
    text = str(info.value)
    assert text.startswith(code) and "Its params:" in text and len(text) <= 187


def test_sample_result_is_a_row_marker_list_step():
    """25g — the compose row-marker block reads sample/filter results as rows."""
    from src.envelopes.schema import InboundEnvelope
    from src.orchestrator import row_markers as rm

    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                          status="resolved",
                          output={"result": [APE_ROWS[2], APE_ROWS[0]]})
    env._node_type = "state_op"
    rows = rm.rows_for(env, None)
    assert rows == [{"item": "e0", "bucket": "bug", "topic": "crash"},
                    {"item": "e2", "bucket": "bug", "topic": "sync"}]
    env.output = {"result": {"bug": 2, "billing": 1}}
    assert rm.rows_for(env, None) is None  # counts are not a list step


# --- Review of ac1c932: params are taken strictly -----------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("op, params", [
    ("group_by", {"key": "bucket", "count": "true"}),
    ("group_by", {"key": "bucket", "count": 1}),
    ("filter", {"key": "bucket", "value": "bug", "negate": "false"}),
])
async def test_count_and_negate_must_be_real_booleans(op, params):
    task = _state_op_task(op, items=APE_ROWS, params=params)
    with pytest.raises(ValueError, match=r"^BRT-SOP-002: .*Its params:"):
        await dispatch_state_op(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("op, params", [
    ("filter", {"key": "", "value": "bug"}),
    ("group_by", {"key": ""}),
    ("group_by", {"group_by": ""}),
])
async def test_an_empty_key_is_a_missing_param(op, params):
    task = _state_op_task(op, items=APE_ROWS, params=params)
    with pytest.raises(ValueError, match=r"^BRT-SOP-002: .*Its params:"):
        await dispatch_state_op(task)


@pytest.mark.asyncio
@pytest.mark.parametrize("op", [5, ["count"], {"op": "count"}])
async def test_an_op_that_is_not_a_string_is_brt_sop_001(op):
    task = _state_op_task("count", items=APE_ROWS)
    task.dependency_input["op"] = op
    with pytest.raises(ValueError) as info:
        await dispatch_state_op(task)
    text = str(info.value)
    assert text.startswith("BRT-SOP-001") and "group_by(" in text and "sample(" in text
