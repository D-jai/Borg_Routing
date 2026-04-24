"""Tests for BRT-ZERO-01 — rule-based Rule Zero shortcut.

TC-ZERO-001 — short conversational prompts ("hi") skip plan/re_plan and go
              straight to compose (one orchestrator call total).
TC-ZERO-002 — long prompts (>= 500 chars) fall through to the full plan
              path even when they look conversational.
TC-ZERO-003 — BORG_RULE_ZERO_SHORTCUT=false forces the full plan path for
              every prompt (debugging backstop).
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
from src.orchestrator.engine import (
    _is_trivial_conversational,
    orchestrate_run,
)
from src.registry.models import ModelInfo


def _fake_model() -> ModelInfo:
    return ModelInfo(
        model_id="lmstudio/test",
        source="lmstudio",
        endpoint="http://localhost:1234",
        context_length=4096,
    )


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "zero.db")
    await create_all(path)
    return path


# --- Heuristic unit tests -------------------------------------------------


def test_heuristic_short_greeting_is_trivial():
    assert _is_trivial_conversational("hi") is True
    assert _is_trivial_conversational("tell me a joke") is True
    assert _is_trivial_conversational("what is photosynthesis?") is True


def test_heuristic_long_prompt_is_not_trivial():
    long_prompt = "analyse this " + ("x " * 300)
    assert len(long_prompt) >= 500
    assert _is_trivial_conversational(long_prompt) is False


def test_heuristic_list_marker_is_not_trivial():
    prompt = "do these things:\n- one\n- two\n- three"
    assert _is_trivial_conversational(prompt) is False


def test_heuristic_bulk_phrase_is_not_trivial():
    assert _is_trivial_conversational("for each email below, classify it") is False
    assert _is_trivial_conversational("compare these three stacks") is False


# --- End-to-end shortcut tests --------------------------------------------


@pytest.mark.asyncio
async def test_short_greeting_takes_shortcut(db, monkeypatch):
    """TC-ZERO-001 — 'hi' → one orchestrator call (compose only);
    orchestrator_decision event with mode=rule_zero_shortcut is written."""
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "true")
    run_id = await create_run("hi", db_path=db)

    call_modes: list[str] = []

    async def mock_invoke(**kwargs):
        call_modes.append(kwargs.get("mode"))
        return {"response": "Hi there!", "confidence": 0.9, "rationale": "greet"}

    with patch(
        "src.orchestrator.engine._invoke_orchestrator_model",
        new=AsyncMock(side_effect=mock_invoke),
    ), patch(
        "src.orchestrator.engine._select_orchestrator_model",
        new=AsyncMock(return_value=_fake_model()),
    ):
        result = await orchestrate_run(run_id, "hi", db_path=db)

    assert result == "Hi there!"
    assert call_modes == ["compose"]

    # Drain log tasks
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.15)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT decision_json FROM logs "
            "WHERE event_type='orchestrator_decision'"
        ) as cur:
            rows = await cur.fetchall()

    assert any("rule_zero_shortcut" in (r[0] or "") for r in rows), \
        f"expected rule_zero_shortcut event; got: {rows}"


@pytest.mark.asyncio
async def test_long_prompt_uses_full_plan(db, monkeypatch):
    """TC-ZERO-002 — a 600-char prompt goes through plan→re_plan→compose
    (three orchestrator calls, not one)."""
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "true")
    long_prompt = "classify these emails below: " + ("spam word " * 80)
    assert len(long_prompt) > 500
    run_id = await create_run(long_prompt, db_path=db)

    call_modes: list[str] = []

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode")
        call_modes.append(mode)
        if mode == "plan":
            return {
                "direct_answer": None,
                "plan": {"nodes": [{
                    "type": "answerer",
                    "id": "n1",
                    "instruction": "do it",
                    "output_schema": {"ok": "bool"},
                }]},
                "rationale": "plan",
            }
        if mode == "re_plan":
            return {"action": "compose", "next_nodes": None, "rationale": "done"}
        if mode == "compose":
            return {"response": "full answer", "confidence": 0.8, "rationale": "ok"}
        return {}

    async def mock_execute(task, db_path):
        return InboundEnvelope(
            envelope_id=uuid.uuid4(),
            subtask_id=task.subtask_id,
            status="resolved",
            output={"ok": True},
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
        result = await orchestrate_run(run_id, long_prompt, db_path=db)

    assert result == "full answer"
    assert "plan" in call_modes
    assert "re_plan" in call_modes
    assert "compose" in call_modes


@pytest.mark.asyncio
async def test_shortcut_disabled_by_env(db, monkeypatch):
    """TC-ZERO-003 — BORG_RULE_ZERO_SHORTCUT=false forces the full plan
    path even for a 'hi'."""
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    run_id = await create_run("hi", db_path=db)

    call_modes: list[str] = []

    async def mock_invoke(**kwargs):
        mode = kwargs.get("mode")
        call_modes.append(mode)
        if mode == "plan":
            return {"direct_answer": "Hello", "plan": None, "rationale": "greet"}
        return {}

    with patch(
        "src.orchestrator.engine._invoke_orchestrator_model",
        new=AsyncMock(side_effect=mock_invoke),
    ), patch(
        "src.orchestrator.engine._select_orchestrator_model",
        new=AsyncMock(return_value=_fake_model()),
    ):
        result = await orchestrate_run(run_id, "hi", db_path=db)

    assert result == "Hello"
    assert call_modes == ["plan"]  # plan mode ran (not compose-only shortcut)
