"""Work order 1.5 review fixes: routing records tell the truth.

Same stand-in model servers as the cost test; runs are read back through
the real GET /runs/{id} function against a temp database.
"""
import asyncio
import json

import pytest

from src.api import tasks as tasks_api
from src.db.state import set_config
from src.envelopes.schema import InboundEnvelope
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from src.registry.cost import call_dollars, resolve_baseline_with_note
from tests.e2e.stub_model_server import (
    StubModels,
    drain_log_writes,
    stub_model,
    three_tier_matrix,
)
from tests.e2e.test_claim_routing import COMPOSED, OPERATOR_INSTRUCTION

TOP_PRICES = (5e-6, 2.5e-5)


async def _setup(tmp_path, monkeypatch, matrix=None):
    from src.db.schema import create_all

    db = str(tmp_path / "review.db")
    await create_all(db)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-review")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.delenv("BASELINE_MODEL_ID", raising=False)
    for name in ("LEAF_QUALITY_LIGHT", "LEAF_QUALITY_MID", "LEAF_QUALITY_HEAVY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", matrix or three_tier_matrix())
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    return db


async def _go(db, stub, monkeypatch):
    stub.install(monkeypatch)
    result = await run_pipeline(OPERATOR_INSTRUCTION, db_path=db)
    await drain_log_writes()
    resp = await tasks_api.get_run_state(result["run_id"])
    return result, json.loads(resp.body)


def _ape_plan(items):
    return [{
        "type": "apply_to_each", "id": "n1", "instruction": "Classify this review.",
        "items": items, "task_type": "extraction", "tier": "light",
        "output_schema": {"result": "string"},
    }]


@pytest.mark.asyncio
async def test_apply_to_each_estimates_cover_every_item(tmp_path, monkeypatch):
    db = await _setup(tmp_path, monkeypatch)
    stub = StubModels(plan_nodes=_ape_plan(["a", "b", "c", "d"]), compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    routing = next(e["routing"] for e in body["envelopes"] if e["type"] == "apply_to_each")
    assert routing["items"] == 4
    pin, pout = TOP_PRICES
    per_base = routing["est_input_tokens"] * pin + routing["est_output_tokens"] * pout
    assert routing["per_item_baseline_est_cost"] == pytest.approx(per_base)
    assert routing["baseline_est_cost"] == pytest.approx(per_base * 4)
    assert routing["est_dollars"] == pytest.approx(routing["per_item_est_dollars"] * 4)
    assert routing["est_dollars"] > 0


@pytest.mark.asyncio
async def test_orchestrate_step_is_served_by_a_child_run_and_run_detail_shows_it(
    tmp_path, monkeypatch
):
    db = await _setup(tmp_path, monkeypatch)
    root_plan = [{
        "type": "orchestrate", "id": "n1", "instruction": "Handle the whole sub-job.",
        "task_type": "reasoning", "tier": "heavy", "output_schema": {"result": "string"},
    }]
    child_plan = [{
        "type": "answerer", "id": "c1", "instruction": "Do the child step.",
        "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"},
    }]
    calls = {"n": 0}

    def plan_for(_req):
        calls["n"] += 1
        return root_plan if calls["n"] == 1 else child_plan

    stub = StubModels(plan_nodes=root_plan, compose_text=COMPOSED, plan_for=plan_for)
    result, body = await _go(db, stub, monkeypatch)

    parent = next(e for e in body["envelopes"] if e["type"] == "orchestrate")
    routing = parent["routing"]
    assert routing["served_by"] == "child_run"
    assert routing["note"]
    assert routing["baseline_est_cost"] is None and routing["est_dollars"] is None
    child_id = routing["child_run_id"]
    assert child_id and child_id != result["run_id"]

    kids = body["child_envelopes"]
    assert kids and all(k["run_id"] == child_id for k in kids)
    routed = [k for k in kids if k["routing"]]
    assert routed
    assert routed[0]["routing"]["chosen"] == "openrouter/stub/cheap"
    assert routed[0]["routing"]["mode"] == "route"
    assert routed[0]["routing"]["candidates"]


@pytest.mark.asyncio
async def test_config_key_overrides_env_and_default_for_the_baseline(tmp_path, monkeypatch):
    db = await _setup(tmp_path, monkeypatch)
    monkeypatch.setenv("BASELINE_MODEL_ID", "openrouter/stub/cheap")
    await set_config("baseline_model_id", "openrouter/stub/mid", db_path=db)
    baseline, note = await resolve_baseline_with_note(three_tier_matrix(), db)
    assert baseline.model_id == "openrouter/stub/mid"
    assert note is None
    stub = StubModels(plan_nodes=_ape_plan(["a"]), compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    for env in body["envelopes"]:
        assert env["routing"]["baseline"] == "openrouter/stub/mid"
        assert "baseline_note" not in env["routing"]


@pytest.mark.asyncio
async def test_a_configured_baseline_missing_from_the_matrix_is_noted(tmp_path, monkeypatch):
    db = await _setup(tmp_path, monkeypatch)
    await set_config("baseline_model_id", "openrouter/does/not-exist", db_path=db)
    stub = StubModels(plan_nodes=_ape_plan(["a"]), compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    for env in body["envelopes"]:
        routing = env["routing"]
        assert "does/not-exist" in routing["baseline_note"]
        assert routing["baseline"] == "openrouter/stub/top"  # the fallback, said out loud


@pytest.mark.asyncio
async def test_a_plan_node_naming_a_checked_model_is_a_planner_pick_with_candidates(
    tmp_path, monkeypatch
):
    db = await _setup(tmp_path, monkeypatch)
    plan = [{
        "type": "answerer", "id": "n1", "instruction": "Do it.", "task_type": "extraction",
        "tier": "light", "recommended_model": "openrouter/stub/mid",
        "output_schema": {"result": "string"},
    }]
    stub = StubModels(plan_nodes=plan, compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    routing = body["envelopes"][0]["routing"]
    assert routing["reason"] == "planner_pick"
    assert routing["chosen"] == "openrouter/stub/mid"
    assert len(routing["candidates"]) >= 2


class _ReplanOnce(StubModels):
    """First re_plan asks for one more step; every later one composes."""

    next_nodes: list = []
    _done = False

    def _answer(self, url, payload):
        res = super()._answer(url, payload)
        if self.requests[-1].kind == "re_plan" and not self._done:
            self._done = True
            res._body["choices"][0]["message"]["content"] = json.dumps(
                {"action": "continue", "next_nodes": self.next_nodes, "rationale": "one more"}
            )
        return res


@pytest.mark.asyncio
async def test_steps_added_by_a_re_plan_are_routed_and_shown(tmp_path, monkeypatch):
    db = await _setup(tmp_path, monkeypatch)
    first = [{"type": "answerer", "id": "n1", "instruction": "First step.",
              "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}]
    more = [{"type": "answerer", "id": "n2", "instruction": "Second step from re_plan.",
             "task_type": "coding", "tier": "mid", "output_schema": {"result": "string"}}]
    stub = _ReplanOnce(plan_nodes=first, compose_text=COMPOSED)
    stub.next_nodes = more
    _result, body = await _go(db, stub, monkeypatch)
    assert len(body["envelopes"]) == 2
    chosen = sorted(e["routing"]["chosen"] for e in body["envelopes"])
    assert chosen == ["openrouter/stub/cheap", "openrouter/stub/mid"]
    assert all(e["routing"]["reason"] == "routed" for e in body["envelopes"])


@pytest.mark.asyncio
async def test_latency_excludes_pricing_and_database_work_after_execute(tmp_path, monkeypatch):
    db = await _setup(tmp_path, monkeypatch)
    plan = [{"type": "answerer", "id": "n1", "instruction": "Do it.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}]

    async def fake_execute(task, db_path="x"):
        return InboundEnvelope(
            envelope_id=task.envelope_id, subtask_id=task.subtask_id, status="resolved",
            output={"result": "ok"}, tokens_used={"input": 10, "output": 5},
        )

    async def slow_pricing(*_a, **_k):
        await asyncio.sleep(0.4)
        return 0.0, None

    monkeypatch.setattr(engine, "execute", fake_execute)
    monkeypatch.setattr(engine, "price_with_baseline", slow_pricing)
    stub = StubModels(plan_nodes=plan, compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    latencies = [e["latency_ms"] for e in body["envelopes"]]
    assert latencies and all(ms is not None and ms < 300 for ms in latencies)


@pytest.mark.asyncio
async def test_local_chosen_step_estimates_zero_dollars_but_the_baseline_is_positive(
    tmp_path, monkeypatch
):
    matrix = three_tier_matrix()
    # A measured, fast local model (an unmeasured one is never free: ruling 28).
    matrix.upsert(stub_model("localbest", 9.0, 0, 0, source="ollama").model_copy(update={"throughput_tps": 1e6}))
    db = await _setup(tmp_path, monkeypatch, matrix=matrix)
    plan = [{"type": "answerer", "id": "n1", "instruction": "Do it.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}]
    stub = StubModels(plan_nodes=plan, compose_text=COMPOSED)
    _result, body = await _go(db, stub, monkeypatch)
    routing = body["envelopes"][0]["routing"]
    assert routing["chosen"].startswith("ollama/")
    assert routing["est_dollars"] == 0.0
    assert routing["baseline_est_cost"] > 0
    top = three_tier_matrix().get("openrouter/stub/top")
    assert routing["baseline_est_cost"] == pytest.approx(
        call_dollars(top, routing["est_input_tokens"], routing["est_output_tokens"])
    )
