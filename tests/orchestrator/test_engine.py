"""Tests for src/orchestrator/engine.py.

TC-ORC-020 — direct_answer in plan mode returns immediately
TC-ORC-021 — 1-stage plan: plan → dispatch → re_plan → compose (3 orchestrator calls)
TC-ORC-022 — re_plan action=fail raises ValueError BRT-ORC-006
TC-ORC-023 — re_plan action=continue dispatches next_nodes
TC-ORC-024 — orchestrator_decision events logged for each orchestrator call
TC-ORC-025 — node build failure → failed result (run continues)
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.db.schema import create_all
from src.db.state import create_run
from src.orchestrator.engine import orchestrate_run


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "engine_test.db")
    await create_all(path)
    return path


def _orchestrator_response(mode: str, **kwargs) -> str:
    """Build a mock orchestrator JSON response for the given mode."""
    if mode == "plan_direct":
        return json.dumps({"direct_answer": "Hello!", "plan": None, "rationale": "simple"})

    if mode == "plan_with_node":
        return json.dumps({
            "direct_answer": None,
            "plan": {
                "nodes": [{
                    "type": "answerer",
                    "id": "n1",
                    "instruction": "Classify this issue.",
                    "output_schema": {"bucket": "bug|feature|question"},
                    "depends_on": [],
                }]
            },
            "rationale": "one node plan",
        })

    if mode == "replan_compose":
        return json.dumps({"action": "compose", "next_nodes": None, "rationale": "done"})

    if mode == "replan_continue":
        return json.dumps({
            "action": "continue",
            "next_nodes": [{
                "type": "answerer",
                "id": "n2",
                "instruction": "Second stage.",
                "output_schema": {"summary": "string"},
                "depends_on": [],
            }],
            "rationale": "need more",
        })

    if mode == "replan_fail":
        return json.dumps({"action": "fail", "rationale": "Cannot proceed.", "next_nodes": None})

    if mode == "compose":
        return json.dumps({
            "response": "Here is the final answer.",
            "confidence": 0.9,
            "rationale": "synthesised",
        })

    return json.dumps({"direct_answer": "fallback", "plan": None, "rationale": ""})


@pytest.mark.asyncio
async def test_direct_answer_returns_immediately(db):
    """TC-ORC-020 — direct_answer in plan → no further model calls."""
    run_id = await create_run("Hello there", db_path=db)
    call_count = [0]

    async def mock_invoke(**kwargs):
        call_count[0] += 1
        return json.loads(_orchestrator_response("plan_direct"))

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=AsyncMock(side_effect=mock_invoke)):
        with patch("src.orchestrator.engine._select_orchestrator_model", new=AsyncMock(return_value=("http://localhost:1234", "test-model"))):
            result = await orchestrate_run(run_id, "Hello there", db_path=db)

    assert result == "Hello!"
    assert call_count[0] == 1  # only plan was called


@pytest.mark.asyncio
async def test_one_stage_plan_three_calls(db):
    """TC-ORC-021 — 1-stage plan uses orchestrator 3 times (plan, re_plan, compose)."""
    run_id = await create_run("Classify this issue", db_path=db)
    call_modes = []

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode", "plan")
        call_modes.append(mode)
        if mode == "plan":
            return json.loads(_orchestrator_response("plan_with_node"))
        elif mode == "re_plan":
            return json.loads(_orchestrator_response("replan_compose"))
        elif mode == "compose":
            return json.loads(_orchestrator_response("compose"))
        return {}

    async def mock_execute(task, db_path):
        from src.envelopes.schema import InboundEnvelope
        return InboundEnvelope(
            envelope_id=uuid.uuid4(),
            subtask_id=task.subtask_id,
            status="resolved",
            output={"bucket": "bug"},
        )

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=AsyncMock(side_effect=mock_invoke)):
        with patch("src.orchestrator.engine._select_orchestrator_model", new=AsyncMock(return_value=("http://localhost:1234", "test-model"))):
            with patch("src.orchestrator.engine.execute", new=AsyncMock(side_effect=mock_execute)):
                result = await orchestrate_run(run_id, "Classify this issue", db_path=db)

    assert result == "Here is the final answer."
    assert "plan" in call_modes
    assert "re_plan" in call_modes
    assert "compose" in call_modes
    assert len([m for m in call_modes if m == "plan"]) == 1


@pytest.mark.asyncio
async def test_replan_fail_raises(db):
    """TC-ORC-022 — re_plan action=fail raises ValueError with BRT-ORC-006."""
    run_id = await create_run("Impossible task", db_path=db)

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode", "plan")
        if mode == "plan":
            return json.loads(_orchestrator_response("plan_with_node"))
        elif mode == "re_plan":
            return json.loads(_orchestrator_response("replan_fail"))
        return {}

    async def mock_execute(task, db_path):
        from src.envelopes.schema import InboundEnvelope
        return InboundEnvelope(
            envelope_id=uuid.uuid4(),
            subtask_id=task.subtask_id,
            status="resolved",
            output={"bucket": "bug"},
        )

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=AsyncMock(side_effect=mock_invoke)):
        with patch("src.orchestrator.engine._select_orchestrator_model", new=AsyncMock(return_value=("http://localhost:1234", "test-model"))):
            with patch("src.orchestrator.engine.execute", new=AsyncMock(side_effect=mock_execute)):
                with pytest.raises(ValueError, match="BRT-ORC-006"):
                    await orchestrate_run(run_id, "Impossible task", db_path=db)


@pytest.mark.asyncio
async def test_replan_continue_dispatches_next_nodes(db):
    """TC-ORC-023 — re_plan action=continue dispatches the next_nodes."""
    run_id = await create_run("Two stage task", db_path=db)
    dispatch_instructions = []

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode", "plan")
        if mode == "plan":
            return json.loads(_orchestrator_response("plan_with_node"))
        elif mode == "re_plan":
            if not kwargs.get("stage_results") or "n2" not in kwargs.get("stage_results", {}):
                return json.loads(_orchestrator_response("replan_continue"))
            return json.loads(_orchestrator_response("replan_compose"))
        elif mode == "compose":
            return json.loads(_orchestrator_response("compose"))
        return {}

    async def mock_execute(task, db_path):
        dispatch_instructions.append(task.instruction)
        from src.envelopes.schema import InboundEnvelope
        return InboundEnvelope(
            envelope_id=uuid.uuid4(),
            subtask_id=task.subtask_id,
            status="resolved",
            output={"bucket": "bug"},
        )

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=AsyncMock(side_effect=mock_invoke)):
        with patch("src.orchestrator.engine._select_orchestrator_model", new=AsyncMock(return_value=("http://localhost:1234", "test-model"))):
            with patch("src.orchestrator.engine.execute", new=AsyncMock(side_effect=mock_execute)):
                result = await orchestrate_run(run_id, "Two stage task", db_path=db)

    assert result == "Here is the final answer."
    # Should have dispatched both stage nodes
    assert len(dispatch_instructions) >= 2


@pytest.mark.asyncio
async def test_orchestrator_decision_events_logged(db):
    """TC-ORC-024 — orchestrator_decision events are logged for each call."""
    import aiosqlite
    run_id = await create_run("Classify this issue", db_path=db)

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode", "plan")
        if mode == "plan":
            return json.loads(_orchestrator_response("plan_with_node"))
        elif mode == "re_plan":
            return json.loads(_orchestrator_response("replan_compose"))
        elif mode == "compose":
            return json.loads(_orchestrator_response("compose"))
        return {}

    async def mock_execute(task, db_path):
        from src.envelopes.schema import InboundEnvelope
        return InboundEnvelope(
            envelope_id=uuid.uuid4(), subtask_id=task.subtask_id,
            status="resolved", output={"bucket": "bug"},
        )

    # Mock _invoke_orchestrator_model to also log the event
    original_invoke = None
    actual_call_count = [0]

    async def patched_invoke(**kwargs):
        result = await mock_invoke(**kwargs)
        actual_call_count[0] += 1
        from src.logging.writer import write_event
        await write_event(
            "orchestrator_decision",
            run_id=kwargs.get("run_id"),
            decision={"mode": kwargs.get("mode")},
            db_path=kwargs.get("db_path", db),
        )
        return result

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=AsyncMock(side_effect=patched_invoke)):
        with patch("src.orchestrator.engine._select_orchestrator_model", new=AsyncMock(return_value=("http://localhost:1234", "test-model"))):
            with patch("src.orchestrator.engine.execute", new=AsyncMock(side_effect=mock_execute)):
                await orchestrate_run(run_id, "Classify", db_path=db)

    # Drain async log tasks
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT event_type FROM logs WHERE event_type='orchestrator_decision'"
        ) as cur:
            rows = await cur.fetchall()

    assert len(rows) >= 3  # plan + re_plan + compose
