"""Work order 1.5, round-2 review fixes.

Same stand-in model servers and temp database as the round-1 review tests.
"""
import json
import uuid

import pytest

from src.orchestrator import engine
from tests.e2e.stub_model_server import StubModels, three_tier_matrix
from tests.e2e.test_claim_routing import COMPOSED
from tests.e2e.test_routing_review_fixes import _go, _setup


@pytest.mark.asyncio
async def test_baseline_estimate_is_null_not_zero_when_the_token_estimate_is_missing(
    tmp_path, monkeypatch
):
    await _setup(tmp_path, monkeypatch)
    seen = []

    async def capture(event_type, **kwargs):
        if event_type == "orchestrator_decision":
            seen.append(kwargs["decision"])

    monkeypatch.setattr(engine, "write_event", capture)
    decision = {
        "chosen": "openrouter/stub/cheap", "reason": "fallback_default",
        "est_input_tokens": None, "est_output_tokens": None, "est_dollars": None,
    }
    await engine._log_route(
        uuid.uuid4(), "n1", uuid.uuid4(), decision, str(tmp_path / "review.db"),
    )
    assert seen, "no routing decision was logged"
    assert seen[0]["baseline"]  # a baseline model was found ...
    assert seen[0]["baseline_est_cost"] is None  # ... but no estimate: null, never 0.0


@pytest.mark.asyncio
async def test_grandchild_envelopes_appear_in_child_envelopes_with_run_id_and_routing(
    tmp_path, monkeypatch
):
    db = await _setup(tmp_path, monkeypatch)
    plans = {
        1: [{"type": "orchestrate", "id": "n1", "instruction": "Handle the whole job.",
             "task_type": "reasoning", "tier": "heavy", "output_schema": {"result": "string"}}],
        2: [{"type": "orchestrate", "id": "c1", "instruction": "Handle the middle part.",
             "task_type": "reasoning", "tier": "heavy", "output_schema": {"result": "string"}}],
        3: [{"type": "answerer", "id": "g1", "instruction": "Do the leaf step.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}],
    }
    calls = {"n": 0}

    def plan_for(_req):
        calls["n"] += 1
        return plans[min(calls["n"], 3)]

    stub = StubModels(plan_nodes=plans[1], compose_text=COMPOSED, plan_for=plan_for)
    result, body = await _go(db, stub, monkeypatch)

    parent = next(e for e in body["envelopes"] if e["type"] == "orchestrate")
    child_id = parent["routing"]["child_run_id"]
    kids = body["child_envelopes"]
    child_orch = next(k for k in kids if k["type"] == "orchestrate")
    assert child_orch["run_id"] == child_id
    grandchild_id = child_orch["routing"]["child_run_id"]
    assert grandchild_id not in (child_id, result["run_id"])
    leaf = [k for k in kids if k["run_id"] == grandchild_id]
    assert leaf, "grandchild envelopes are missing from child_envelopes"
    assert leaf[0]["routing"]["mode"] == "route"
    assert leaf[0]["routing"]["chosen"] == "openrouter/stub/cheap"


def _items_stub(second_node):
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": [second_node], "rationale": "next"}

    list_step = {"type": "answerer", "id": "n1", "instruction": "List the reviews.",
                 "task_type": "extraction", "tier": "light",
                 "output_schema": {"items": "list"}}

    def answer(req):
        if "List the reviews." in req.system:
            return {"items": ["a", "b", "c"]}
        return {"result": "ok"}

    return StubModels(plan_nodes=[list_step], compose_text=COMPOSED,
                      answer_for=answer, replan_for=replan)


@pytest.mark.asyncio
@pytest.mark.parametrize("node_type", [None, "answerer"])
async def test_skill_node_that_runs_as_apply_to_each_gets_item_figures(
    tmp_path, monkeypatch, node_type
):
    db = await _setup(tmp_path, monkeypatch)
    node = {"id": "n2", "skill_id": "extract_fields",
            "skill_args": {"items_ref": "n1", "concurrency_limit": 3,
                           "fields": [{"name": "product", "type": "string",
                                       "description": "the product"}]}}
    if node_type:
        node["type"] = node_type
    _result, body = await _go(db, _items_stub(node), monkeypatch)
    step = next(e for e in body["envelopes"] if e["type"] == "apply_to_each")
    routing = step["routing"]
    assert routing["items"] == 3
    assert routing["est_dollars"] == pytest.approx(routing["per_item_est_dollars"] * 3)
    assert routing["baseline_est_cost"] == pytest.approx(
        routing["per_item_baseline_est_cost"] * 3
    )


@pytest.mark.asyncio
async def test_decompose_task_skill_is_served_by_a_child_run(tmp_path, monkeypatch):
    db = await _setup(tmp_path, monkeypatch)
    root_plan = [{"id": "n1", "skill_id": "decompose_task",
                  "skill_args": {"sub_task_instruction": "Handle the sub-job.",
                                 "context_for_child": "none",
                                 "expected_return_schema": {"result": "string"}}}]
    child_plan = [{"type": "answerer", "id": "c1", "instruction": "Do the child step.",
                   "task_type": "extraction", "tier": "light",
                   "output_schema": {"result": "string"}}]
    calls = {"n": 0}

    def plan_for(_req):
        calls["n"] += 1
        return root_plan if calls["n"] == 1 else child_plan

    stub = StubModels(plan_nodes=root_plan, compose_text=COMPOSED, plan_for=plan_for)
    _result, body = await _go(db, stub, monkeypatch)
    step = next(e for e in body["envelopes"] if e["type"] == "orchestrate")
    routing = step["routing"]
    assert routing["served_by"] == "child_run"
    assert routing["child_run_id"]
    assert routing["est_dollars"] is None and routing["baseline_est_cost"] is None


@pytest.mark.asyncio
async def test_orchestrate_step_demoted_at_max_depth_is_recorded_as_a_single_call(
    tmp_path, monkeypatch
):
    db = await _setup(tmp_path, monkeypatch)
    monkeypatch.setattr(engine, "MAX_DEPTH", 1)  # the root's steps sit at the cap
    plan = [{"type": "orchestrate", "id": "n1", "instruction": "Handle the whole sub-job.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}]
    stub = StubModels(plan_nodes=plan, compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    step = body["envelopes"][0]
    routing = step["routing"]
    assert "served_by" not in routing and "child_run_id" not in routing
    assert routing["note"] == "demoted at max depth"
    assert routing["est_dollars"] is not None and routing["est_dollars"] > 0
    assert routing["baseline_est_cost"] is not None and routing["baseline_est_cost"] > 0
    assert not body["child_envelopes"]
    json.dumps(body)  # the whole record stays serialisable
