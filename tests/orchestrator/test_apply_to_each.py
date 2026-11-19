"""Tests for src/orchestrator/apply_to_each.py.

TC-APE-001 — expand_and_run dispatches N child envelopes and aggregates results
TC-APE-002 — failures are collected in failures[], not raised
TC-APE-003 — BRT-APE-001 raised when items not resolvable
TC-APE-004 — BRT-APE-003 raised for invalid concurrency_limit
TC-APE-005 — BRT-APE-004 raised when current_depth >= max_depth
TC-APE-006 — results are ordered by index
TC-APE-007 — 100% failure still returns resolved envelope
TC-APE-008 — _level_suffix produces correct alphabetic suffixes
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.orchestrator.apply_to_each import _level_suffix, expand_and_run
from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.db.schema import create_all


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "ape_test.db")
    await create_all(path)
    return path


def _ape_task(items: list, depth: int = 1, max_depth: int = 5, **kwargs) -> WrappedTask:
    return WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="apply_to_each",
        current_depth=depth,
        max_depth=max_depth,
        level_path="1",
        instruction="Classify this item.",
        output_schema={"bucket": "bug|feature|question"},
        dependency_input={
            "items": items,
            "concurrency_limit": 5,
            **kwargs.pop("extra_dep", {}),
        },
        model_endpoint="http://localhost:1234",
        model_name="test-model",
        **kwargs,
    )


def _resolved_env(task: WrappedTask, output: dict) -> InboundEnvelope:
    return InboundEnvelope(
        envelope_id=uuid.uuid4(),
        subtask_id=task.subtask_id,
        status="resolved",
        output=output,
        confidence=0.9,
    )


def _failed_env(task: WrappedTask) -> InboundEnvelope:
    return InboundEnvelope(
        envelope_id=uuid.uuid4(),
        subtask_id=task.subtask_id,
        status="failed",
        output={},
        failure_reason="Model timed out.",
    )


@pytest.mark.asyncio
async def test_expand_and_run_aggregates(db):
    """TC-APE-001 — 10 items, all succeed → results has 10 entries."""
    items = [{"id": f"i{i}", "text": f"item {i}"} for i in range(10)]
    task = _ape_task(items)

    call_count = [0]
    async def mock_dispatch(child_task, db_path):
        call_count[0] += 1
        return _resolved_env(child_task, {"bucket": "bug"})

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch):
        env = await expand_and_run(task, db_path=db)

    assert env.status == "resolved"
    assert env.output["total"] == 10
    assert env.output["success_count"] == 10
    assert env.output["failure_count"] == 0
    assert len(env.output["results"]) == 10
    assert call_count[0] == 10


@pytest.mark.asyncio
async def test_failures_collected_not_raised(db):
    """TC-APE-002 — 2 failures out of 5 go to failures[], not raised."""
    items = list(range(5))
    task = _ape_task(items)

    async def mock_dispatch(child_task, db_path):
        idx = int(child_task.trigger_reason.split("item_")[1])
        if idx in (1, 3):
            return _failed_env(child_task)
        return _resolved_env(child_task, {"bucket": "bug"})

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch):
        env = await expand_and_run(task, db_path=db)

    assert env.status == "resolved"
    assert env.output["success_count"] == 3
    assert env.output["failure_count"] == 2
    assert env.output["failures"][0]["index"] == 1
    assert env.output["failures"][1]["index"] == 3


@pytest.mark.asyncio
async def test_items_not_resolvable_raises(db):
    """TC-APE-003 — BRT-APE-001 raised when dependency_input has no items."""
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="apply_to_each",
        current_depth=1,
        max_depth=5,
        level_path="1",
        instruction="do",
        output_schema={},
        dependency_input={"concurrency_limit": 5},  # no items
    )
    with pytest.raises(ValueError, match="BRT-APE-001"):
        await expand_and_run(task, db_path=db)


@pytest.mark.asyncio
async def test_invalid_concurrency_limit_raises(db):
    """TC-APE-003 — BRT-APE-003 raised for zero concurrency_limit."""
    items = [1, 2, 3]
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="apply_to_each",
        current_depth=1,
        max_depth=5,
        level_path="1",
        instruction="do",
        output_schema={},
        dependency_input={"items": items, "concurrency_limit": 0},
    )
    with pytest.raises(ValueError, match="BRT-APE-003"):
        await expand_and_run(task, db_path=db)


@pytest.mark.asyncio
async def test_depth_limit_exceeded_raises(db):
    """TC-APE-005 — BRT-APE-004 raised when current_depth >= max_depth."""
    task = _ape_task([1, 2], depth=5, max_depth=5)
    with pytest.raises(ValueError, match="BRT-APE-004"):
        await expand_and_run(task, db_path=db)


@pytest.mark.asyncio
async def test_results_ordered_by_index(db):
    """TC-APE-006 — results are sorted by index regardless of async completion order."""
    items = list(range(5))
    task = _ape_task(items)

    async def mock_dispatch(child_task, db_path):
        return _resolved_env(child_task, {"bucket": "bug"})

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch):
        env = await expand_and_run(task, db_path=db)

    indices = [r["index"] for r in env.output["results"]]
    assert indices == sorted(indices)


@pytest.mark.asyncio
async def test_all_failures_returns_resolved(db):
    """TC-APE-007 — 100% failure still returns a resolved envelope."""
    items = [1, 2, 3]
    task = _ape_task(items)

    async def mock_dispatch(child_task, db_path):
        return _failed_env(child_task)

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch):
        env = await expand_and_run(task, db_path=db)

    assert env.status == "resolved"
    assert env.output["success_count"] == 0
    assert env.output["failure_count"] == 3


def test_level_suffix():
    """TC-APE-008 — _level_suffix produces correct alphabetic suffixes."""
    assert _level_suffix(0) == "a"
    assert _level_suffix(1) == "b"
    assert _level_suffix(25) == "z"
    assert _level_suffix(26) == "aa"
    assert _level_suffix(27) == "ab"


@pytest.mark.asyncio
async def test_concurrency_limit_above_cap_is_noted_and_warned(db):
    """Round-2 review (3): a capped concurrency_limit is recorded on the step."""
    from src.lane.outbound import MAX_CONCURRENCY

    task = _ape_task([{"id": "a"}, {"id": "b"}], extra_dep={"concurrency_limit": 500})
    events = []

    async def fake_event(event_type, **kw):
        events.append((event_type, kw))

    async def mock_dispatch(child_task, db_path):
        return _resolved_env(child_task, {"bucket": "bug"})

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch), \
            patch("src.logging.writer.write_event", new=fake_event):
        env = await expand_and_run(task, db_path=db)

    assert env.output["notes"] == [f"concurrency_limit 500 capped at {MAX_CONCURRENCY}"]
    warn = [kw for t, kw in events if (kw.get("decision") or {}).get("severity") == "warning"]
    assert len(warn) == 1 and warn[0]["run_id"] == task.run_id


@pytest.mark.asyncio
async def test_concurrency_limit_within_cap_has_no_note(db):
    task = _ape_task([{"id": "a"}])

    async def mock_dispatch(child_task, db_path):
        return _resolved_env(child_task, {"bucket": "bug"})

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch):
        env = await expand_and_run(task, db_path=db)
    assert "notes" not in env.output
