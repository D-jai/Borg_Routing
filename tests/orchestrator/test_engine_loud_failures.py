"""Tests for BRT-ORC-02 — silent-fallback elimination in orchestrate_run.

TC-ORC-030 — no orchestrator model available → BRT-ORC-007 propagates
             (previously silently replaced with endpoint="" which crashed
             deep in httpx with no run trace).
TC-ORC-031 — default answerer selection failure → BRT-ORC-015 warning
             event written; run continues using orchestrator model.
TC-ORC-032 — envelope save failure → BRT-ORC-016 error event written;
             run continues and returns compose result.
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest

from src.db.schema import create_all
from src.db.state import create_run
from src.envelopes.schema import InboundEnvelope
from src.orchestrator.engine import orchestrate_run
from src.registry.models import ModelInfo


def _fake_model(
    endpoint: str = "http://localhost:1234",
    model_id: str = "lmstudio/test-model",
) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        source="lmstudio",
        endpoint=endpoint,
        context_length=4096,
    )


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "loud_fail.db")
    await create_all(path)
    return path


def _resp_plan_one_node() -> dict:
    return {
        "direct_answer": None,
        "plan": {"nodes": [{
            "type": "answerer",
            "id": "n1",
            "instruction": "Do a thing.",
            "output_schema": {"bucket": "x|y"},
            "depends_on": [],
        }]},
        "rationale": "one-node plan",
    }


def _resp_replan_compose() -> dict:
    return {"action": "compose", "next_nodes": None, "rationale": "done"}


def _resp_compose() -> dict:
    return {"response": "final", "confidence": 0.8, "rationale": "ok"}


async def _mock_execute(task, db_path):
    return InboundEnvelope(
        envelope_id=uuid.uuid4(),
        subtask_id=task.subtask_id,
        status="resolved",
        output={"bucket": "x"},
    )


@pytest.mark.asyncio
async def test_no_orch_model_propagates_007(db):
    """TC-ORC-030 — BRT-ORC-007 from _select_orchestrator_model propagates
    instead of being swallowed into a silent bad-endpoint fallback."""
    run_id = await create_run("any prompt", db_path=db)

    async def _raise_007(*a, **kw):
        raise ValueError("BRT-ORC-007: no orchestrator model available")

    with patch(
        "src.orchestrator.engine._select_orchestrator_model",
        new=AsyncMock(side_effect=_raise_007),
    ):
        with pytest.raises(ValueError, match="BRT-ORC-007"):
            await orchestrate_run(run_id, "any prompt", db_path=db)


@pytest.mark.asyncio
async def test_default_answerer_fallback_logs_015(db, monkeypatch):
    """TC-ORC-031 — when select_model for the default answerer raises, a
    BRT-ORC-015 warning event is written and the run still completes."""
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    run_id = await create_run("any", db_path=db)

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode")
        if mode == "plan":
            return _resp_plan_one_node()
        if mode == "re_plan":
            return _resp_replan_compose()
        if mode == "compose":
            return _resp_compose()
        return {}

    def _raise_select(*a, **kw):
        raise RuntimeError("no model qualifies")

    with patch(
        "src.orchestrator.engine._invoke_orchestrator_model",
        new=AsyncMock(side_effect=mock_invoke),
    ), patch(
        "src.orchestrator.engine._select_orchestrator_model",
        new=AsyncMock(return_value=_fake_model()),
    ), patch(
        "src.orchestrator.engine.select_model",
        side_effect=_raise_select,
    ), patch(
        "src.orchestrator.engine.execute",
        new=AsyncMock(side_effect=_mock_execute),
    ):
        result = await orchestrate_run(run_id, "any", db_path=db)

    assert result == "final"

    # Drain async log writes
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error'"
        ) as cur:
            rows = await cur.fetchall()

    assert any("BRT-ORC-015" in (r[0] or "") for r in rows), \
        f"expected a BRT-ORC-015 event; got: {rows}"


@pytest.mark.asyncio
async def test_envelope_save_failure_logs_016(db, monkeypatch):
    """TC-ORC-032 — when save_envelope raises, BRT-ORC-016 is logged and the
    run still completes (error is visible in the trace, not swallowed)."""
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    run_id = await create_run("any", db_path=db)

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode")
        if mode == "plan":
            return _resp_plan_one_node()
        if mode == "re_plan":
            return _resp_replan_compose()
        if mode == "compose":
            return _resp_compose()
        return {}

    async def _broken_save(*a, **kw):
        raise RuntimeError("DB is on fire")

    with patch(
        "src.orchestrator.engine._invoke_orchestrator_model",
        new=AsyncMock(side_effect=mock_invoke),
    ), patch(
        "src.orchestrator.engine._select_orchestrator_model",
        new=AsyncMock(return_value=_fake_model()),
    ), patch(
        "src.orchestrator.engine.save_envelope",
        new=AsyncMock(side_effect=_broken_save),
    ), patch(
        "src.orchestrator.engine.execute",
        new=AsyncMock(side_effect=_mock_execute),
    ):
        result = await orchestrate_run(run_id, "any", db_path=db)

    assert result == "final"

    # Drain async log writes
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error'"
        ) as cur:
            rows = await cur.fetchall()

    assert any("BRT-ORC-016" in (r[0] or "") for r in rows), \
        f"expected a BRT-ORC-016 error event; got: {rows}"
