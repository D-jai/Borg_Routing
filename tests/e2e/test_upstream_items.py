"""Steps that work on an earlier step's list: state_op `source` (R4) and a
skill apply_to_each step's `items_ref` (R14).

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


def _skill_step(items_ref):
    return {"type": "apply_to_each", "id": "n2", "skill_id": "extract_fields",
            "skill_args": {"items_ref": items_ref, "concurrency_limit": 3,
                           "fields": [{"name": "product", "type": "string",
                                       "description": "the product"}]}}


def _per_item_calls(stub):
    return [r for r in stub.of_kind("answerer") if "Extract the following fields" in r.system]


@pytest.mark.asyncio
async def test_skill_apply_to_each_step_gets_the_items_of_an_earlier_step(env):
    db_path, mp = env
    step = _skill_step("n1")
    stub = StubModels(plan_nodes=[LIST_STEP], answer_for=_lister,
                      replan_for=_second_wave(step)).install(mp)
    result = await run_pipeline("Extract the product from each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    per_item = _per_item_calls(stub)
    assert len(per_item) == len(REVIEWS), f"{len(per_item)} per-item calls"
    joined = " ".join(r.system for r in per_item)
    for review in REVIEWS:
        assert review in joined


@pytest.mark.asyncio
async def test_skill_apply_to_each_with_an_unknown_items_ref_fails_loudly(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[LIST_STEP], answer_for=_lister,
                      replan_for=_second_wave(_skill_step("nope"))).install(mp)
    await run_pipeline("Extract.", db_path=db_path)
    await drain_log_writes()
    assert not _per_item_calls(stub)
    with sqlite3.connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE type='apply_to_each'"
        ).fetchone()
    assert row is not None and row[0] == "failed", row
    assert "BRT-APE-006" in row[1]   # planner ruling 1a: a ref naming nothing


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


@pytest.mark.asyncio
async def test_filter_matching_nothing_then_count_gives_zero(env):
    """A resolved empty list is valid input: the count is 0, not BRT-SOP-003."""
    db_path, mp = env
    waves = [
        {"type": "state_op", "id": "n2", "op": "filter", "source": "n1",
         "params": {"key": "product", "value": "nothing-matches"}},
        {"type": "state_op", "id": "n3", "op": "count", "source": "n2", "params": {}},
    ]

    def replan(_req):
        if waves:
            return {"action": "continue", "next_nodes": [waves.pop(0)], "rationale": "next"}
        return {"action": "compose", "rationale": "done"}

    def lister(req):
        if "List the reviews." in req.system:
            return {"items": [{"product": "kettle"}, {"product": "toaster"}]}
        return {"result": "ok"}

    StubModels(plan_nodes=[LIST_STEP], answer_for=lister, replan_for=replan).install(mp)
    result = await run_pipeline("Filter then count.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE type='state_op' ORDER BY rowid"
        ).fetchall()
    assert len(rows) == 2, rows
    assert all(r[0] == "resolved" for r in rows), rows
    assert '"count":0' in rows[1][1].replace(" ", ""), rows[1][1]
