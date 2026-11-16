"""A state_op step works on the list of the earlier step it names as `source` (R4).

The real engine, executor and router run against the scripted stand-in
model server. Nothing here needs a key or the network.
"""
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

REVIEWS = ["Kettle: boils fast", "Toaster: burnt everything", "Blender: loud but fine"]


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "upstream.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-upstream")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _second_wave(node):
    """A re_plan answer that sends ``node`` once, then composes."""
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": [node], "rationale": "next step"}
    return replan


def _lister(req):
    if "List the reviews." in req.system:
        return {"items": REVIEWS}
    return {"result": "ok"}


LIST_STEP = {"type": "answerer", "id": "n1", "instruction": "List the reviews.",
             "task_type": "extraction", "tier": "light", "output_schema": {"items": "list"}}


@pytest.mark.asyncio
async def test_state_op_reads_the_list_of_the_step_it_names_as_source(env):
    db_path, mp = env
    node = {"type": "state_op", "id": "n2", "op": "count", "source": "n1", "params": {}}
    StubModels(plan_nodes=[LIST_STEP], answer_for=_lister,
               replan_for=_second_wave(node)).install(mp)
    result = await run_pipeline("Count the reviews.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE type='state_op'"
        ).fetchall()
    assert rows, "state_op envelope missing"
    assert rows[0][0] == "resolved", rows[0][1]
    assert '"count":3' in rows[0][1].replace(" ", "")


@pytest.mark.asyncio
async def test_state_op_with_an_unresolved_source_fails_instead_of_counting_nothing(env):
    db_path, mp = env
    node = {"type": "state_op", "id": "n2", "op": "count", "source": "nope", "params": {}}
    StubModels(plan_nodes=[LIST_STEP], answer_for=_lister,
               replan_for=_second_wave(node)).install(mp)
    await run_pipeline("Count.", db_path=db_path)
    await drain_log_writes()
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE type='state_op'"
        ).fetchone()
    assert row is not None and row[0] == "failed", row
    assert "BRT-SOP-003" in row[1]
