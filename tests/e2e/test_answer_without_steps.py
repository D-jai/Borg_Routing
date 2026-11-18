"""Planner ruling 26: an answer with no step behind it says so, and with
documents it does not exist.

(a) No documents, steps planned, none finished, compose wrote text: the run
    completes with ``answer_kind = "planner_only"`` and a BRT-ORC-033 warning
    event listing the steps' failure codes (the receipt shows it).
(b) The run has documents and no step that read them finished: the run
    fails with BRT-ORC-034, compose is never called, no compose text comes
    back, and the error lists the failure codes (a raw step as code and
    document id only).

The real engine, executor and router run against the scripted stand-in
model server. Nothing here needs a key or the network.
"""
import json
import sqlite3
from uuid import UUID

import pytest

from src.db.schema import create_all
from src.db.state import get_run
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import (
    StubModels, drain_log_writes, lane_matrix, three_tier_matrix,
)
from tests.e2e.test_privacy_lane import (  # noqa: F401  (lane is a fixture)
    CANARIES, PLAN_RAW, _run, lane,
)

COMPOSED = "Final answer from the planner."


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "no_steps.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-no-steps")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _events(db_path, code):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error' ORDER BY rowid"
        ).fetchall()
    out = [json.loads(r[0]) for r in rows if r[0]]
    return [e for e in out if e.get("code") == code]


async def _answer_kind(db_path, run_id):
    return (await get_run(UUID(run_id), db_path=db_path)).get("answer_kind")


BAD_STEP = {"type": "state_op", "id": "n1", "op": "sort", "items": [{"a": 1}]}
GOOD_STEP = {"type": "state_op", "id": "n1", "op": "count", "items": [{"a": 1}]}


@pytest.mark.asyncio
async def test_a_every_step_failed_the_answer_says_the_planner_wrote_it_alone(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[BAD_STEP, {**BAD_STEP, "id": "n2", "op": "pick"}],
                      compose_text=COMPOSED).install(mp)
    result = await run_pipeline("Sort these rows.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert result["response"] == COMPOSED
    assert stub.of_kind("compose"), "compose still writes the answer"
    assert await _answer_kind(db_path, result["run_id"]) == "planner_only"
    events = _events(db_path, "BRT-ORC-033")
    assert len(events) == 1, events
    assert events[0]["severity"] == "warning"
    assert events[0]["failed_steps"] == 2
    assert events[0]["step_codes"] == ["BRT-ORC-004", "BRT-ORC-004"]


@pytest.mark.asyncio
async def test_a_a_step_finished_the_answer_is_composed(env):
    db_path, mp = env
    StubModels(plan_nodes=[GOOD_STEP, {**BAD_STEP, "id": "n2"}],
               compose_text=COMPOSED).install(mp)
    result = await run_pipeline("Count these rows.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert await _answer_kind(db_path, result["run_id"]) == "composed"
    assert _events(db_path, "BRT-ORC-033") == []


async def test_b_no_document_step_finished_the_run_fails_with_no_compose_text(lane):
    # raw_local and no local model: the raw step fails (BRT-LANE-002), and
    # the step that reads it fails too.
    stub, result = await _run(lane, "raw_local", three_tier_matrix(), compose_text=COMPOSED)
    assert result["status"] == "failed", result
    assert stub.of_kind("compose") == [], "compose must not be called"
    assert COMPOSED not in (result["response"] or "")
    error = result["error"] or ""
    assert "BRT-ORC-034" in error and "BRT-LANE-002" in error, error
    assert "doc_001" in error  # the document id, never its name or text
    assert "acme_note" not in error
    for canary in CANARIES:
        assert canary not in error
    assert await _answer_kind(lane["db"], result["run_id"]) is None
    events = _events(lane["db"], "BRT-ORC-034")
    assert len(events) == 1 and events[0]["step_codes"], events
    raw = [s for s in events[0]["step_codes"] if isinstance(s, dict)]
    assert raw and all(set(s) == {"code", "doc"} for s in raw), events


async def test_b_documents_but_no_step_read_them_fails(lane):
    plan = [{"type": "answerer", "id": "n1", "instruction": "Say hello.",
             "task_type": "reasoning", "tier": "light"}]
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=plan,
                              compose_text=COMPOSED)
    assert result["status"] == "failed", result
    assert stub.of_kind("compose") == []
    assert "BRT-ORC-034" in (result["error"] or "")


async def test_b_a_document_step_finished_the_run_completes(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix(), compose_text=COMPOSED)
    assert result["status"] == "completed", result.get("error")
    assert result["response"] == COMPOSED
    assert await _answer_kind(lane["db"], result["run_id"]) == "composed"
    assert _events(lane["db"], "BRT-ORC-034") == []
