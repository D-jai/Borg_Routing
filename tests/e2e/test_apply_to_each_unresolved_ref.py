"""An apply_to_each step whose ``inputs_ref`` names no finished step.

Check 1 attempt 1: the planner wrote ``"inputs_ref": "reviews"``, naming a
list inside the operator's own prompt. The step resolved to no items and
failed with no model call. Ruling (16-Nov-2026): such a step runs as one
routed answerer call over the operator's input, keeping its task type and
tier, with a warning code (BRT-ORC-024) and a note on the route decision.

The real engine, executor and router run against the scripted stand-in
model server and a temp database. Nothing here needs a key or the network.
"""
import json
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

OPERATOR = (
    "Extract the product name and sentiment from each review. reviews = "
    "['Kettle: boils fast, love it', 'Toaster: burnt everything', "
    "'Blender: loud but fine']"
)
INSTRUCTION = (
    "Extract product name and sentiment (positive, neutral, or negative) "
    "from this review."
)
PER_ITEM_SCHEMA = {"product": "string", "sentiment": "string"}
NODE_CONTEXT = "Node context marker 7f3a."

UNRESOLVED_NODE = {
    "type": "apply_to_each", "id": "n1", "instruction": INSTRUCTION,
    "inputs_ref": "reviews", "task_type": "extraction", "tier": "light",
    "context": NODE_CONTEXT, "output_schema": PER_ITEM_SCHEMA,
}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "unresolved_ref.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-unresolved")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _answer(req):
    if "one result per item" in req.system:
        return {"results": [
            {"product": "Kettle", "sentiment": "positive"},
            {"product": "Toaster", "sentiment": "negative"},
            {"product": "Blender", "sentiment": "neutral"},
        ]}
    return {"result": "ok"}


def _context_of(req) -> str:
    text = req.system + "\n" + req.user
    start = text.index("Context:\n") + len("Context:\n")
    end = text.index("State query result", start)
    return text[start:end]


def _decisions(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT event_type, decision_json FROM logs WHERE decision_json IS NOT NULL"
        ).fetchall()
    return [(et, json.loads(d)) for et, d in rows]


async def _run_unresolved(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[UNRESOLVED_NODE], answer_for=_answer).install(mp)
    result = await run_pipeline(OPERATOR, db_path=db_path)
    await drain_log_writes()
    return db_path, stub, result


@pytest.mark.asyncio
async def test_unresolved_inputs_ref_runs_one_routed_call_over_the_operators_input(env):
    db_path, stub, result = await _run_unresolved(env)
    assert result["status"] == "completed", result.get("error")
    calls = [r for r in stub.of_kind("answerer") if INSTRUCTION in r.system]
    assert len(calls) == 1, f"{len(calls)} answerer calls for the step"
    call = calls[0]
    assert call.model == "stub/cheap", call.model
    assert OPERATOR in _context_of(call)
    assert NODE_CONTEXT in _context_of(call)
    # Law 1: the governance block is the operator's words, verbatim.
    assert call.governance_instruction == OPERATOR


@pytest.mark.asyncio
async def test_unresolved_inputs_ref_is_noted_on_the_route_and_warned(env):
    db_path, _stub, _result = await _run_unresolved(env)
    events = _decisions(db_path)
    routes = [d for et, d in events
              if et == "orchestrator_decision" and d.get("mode") == "route"
              and d.get("node_id") == "n1"]
    assert len(routes) == 1, routes
    route = routes[0]
    assert route["chosen"] == "openrouter/stub/cheap"
    assert "inputs_ref 'reviews' names no step" in route.get("note", ""), route
    assert "items" not in route  # a single call, not a fan-out
    assert route.get("est_dollars") is not None
    warns = [d for et, d in events if et == "error" and d.get("code") == "BRT-ORC-024"]
    assert len(warns) == 1, events
    assert warns[0]["severity"] == "warning"
    assert warns[0]["node_id"] == "n1"
    assert warns[0]["inputs_ref"] == "reviews"


@pytest.mark.asyncio
async def test_unresolved_inputs_ref_step_succeeds_and_costs_dollars(env):
    db_path, _stub, _result = await _run_unresolved(env)
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT type, status, dollars, inbound_payload FROM envelopes"
        ).fetchall()
    answerers = [r for r in rows if r[0] == "answerer"]
    assert len(answerers) == 1, rows
    _type, status, dollars, inbound = answerers[0]
    assert status == "resolved", inbound
    assert dollars is not None and float(dollars) > 0, dollars
    assert "Toaster" in inbound
    assert not [r for r in rows if r[0] == "apply_to_each"], rows


@pytest.mark.asyncio
async def test_apply_to_each_with_inline_items_still_fans_out_per_item(env):
    db_path, mp = env
    node = dict(UNRESOLVED_NODE, items=["Kettle: good", "Toaster: bad"])
    stub = StubModels(plan_nodes=[node], answer_for=lambda r: {"product": "x"}).install(mp)
    result = await run_pipeline(OPERATOR, db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    per_item = [r for r in stub.of_kind("answerer") if INSTRUCTION in r.system]
    assert len(per_item) == 2, f"{len(per_item)} per-item calls"
    assert not [d for et, d in _decisions(db_path) if d.get("code") == "BRT-ORC-024"]


@pytest.mark.asyncio
async def test_inputs_ref_naming_a_finished_step_still_resolves_its_list(env):
    db_path, mp = env
    lister = {"type": "answerer", "id": "n0", "instruction": "List the reviews.",
              "task_type": "extraction", "tier": "light", "output_schema": {"items": "list"}}
    node = dict(UNRESOLVED_NODE, inputs_ref="n0")
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": [node], "rationale": "next"}

    def answer(req):
        if "List the reviews." in req.system:
            return {"items": ["Kettle: good", "Toaster: bad", "Blender: ok"]}
        return {"product": "x"}

    stub = StubModels(plan_nodes=[lister], answer_for=answer, replan_for=replan).install(mp)
    result = await run_pipeline(OPERATOR, db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    per_item = [r for r in stub.of_kind("answerer") if INSTRUCTION in r.system]
    assert len(per_item) == 3, f"{len(per_item)} per-item calls"
    assert not [d for et, d in _decisions(db_path) if d.get("code") == "BRT-ORC-024"]
