"""Tests for BRT-PR-01a engine piece — depends_on injection + BRT-ORC-017.

TC-PR-010 — answerer node with depends_on=[n1] + results={n1: env} →
            built task has n1's output in dependency_input.upstream_results
            and in the context string.
TC-PR-011 — missing depends_on id → BRT-ORC-017 event logged during
            dispatch; run still completes.
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
from src.orchestrator.engine import _build_task_from_node, orchestrate_run
from src.registry.models import ModelInfo


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "engine_deps.db")
    await create_all(path)
    return path


def _fake_model() -> ModelInfo:
    return ModelInfo(
        model_id="lmstudio/test",
        source="lmstudio",
        endpoint="http://localhost:1234",
        context_length=4096,
    )


def test_depends_on_injects_context_and_dep_input():
    """TC-PR-010 — upstream output lands in dependency_input['upstream_results']
    and in the context block."""
    upstream_env = InboundEnvelope(
        envelope_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        status="resolved",
        output={"summary": "upstream said hi", "score": 0.9},
    )
    node = {
        "type": "answerer",
        "id": "n2",
        "instruction": "Synthesise from n1.",
        "depends_on": ["n1"],
        "output_schema": {"text": "string"},
        "context": "base context",
    }

    task = _build_task_from_node(
        node=node,
        run_id=uuid.uuid4(),
        current_depth=1,
        max_depth=5,
        level_path="1a",
        model_endpoint="http://localhost:1234",
        model_name="test",
        user_instruction="root",
        results={"n1": upstream_env},
    )

    assert "upstream_results" in task.dependency_input
    assert task.dependency_input["upstream_results"] == {
        "n1": {"summary": "upstream said hi", "score": 0.9}
    }
    assert "base context" in task.context
    assert "Upstream stage outputs" in task.context
    assert "upstream said hi" in task.context


@pytest.mark.asyncio
async def test_depends_on_missing_emits_017(db, monkeypatch):
    """TC-PR-011 — a node references a depends_on id that isn't present in
    results → BRT-ORC-017 event written during dispatch, run completes."""
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    run_id = await create_run("x", db_path=db)

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode")
        if mode == "plan":
            return {
                "direct_answer": None,
                "plan": {"nodes": [{
                    "type": "answerer",
                    "id": "n1",
                    "instruction": "Synthesise.",
                    "depends_on": ["ghost_node_that_never_runs"],
                    "output_schema": {"text": "string"},
                }]},
                "rationale": "synth from ghost",
            }
        if mode == "re_plan":
            return {"action": "compose", "next_nodes": None, "rationale": "done"}
        if mode == "compose":
            return {"response": "final", "confidence": 0.7, "rationale": "ok"}
        return {}

    async def mock_execute(task, db_path):
        return InboundEnvelope(
            envelope_id=uuid.uuid4(),
            subtask_id=task.subtask_id,
            status="resolved",
            output={"text": "child output"},
        )

    with patch(
        "src.orchestrator.engine._invoke_orchestrator_model",
        new=AsyncMock(side_effect=mock_invoke),
    ), patch(
        "src.orchestrator.engine._select_orchestrator_model",
        new=AsyncMock(return_value=_fake_model()),
    ), patch(
        "src.orchestrator.engine.execute",
        new=AsyncMock(side_effect=mock_execute),
    ):
        result = await orchestrate_run(run_id, "x", db_path=db)

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

    assert any("BRT-ORC-017" in (r[0] or "") for r in rows), \
        f"expected a BRT-ORC-017 event; got: {rows}"
