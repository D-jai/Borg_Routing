"""Planner ruling 1a (1.10): lists in the operator's message, and refs.

- Inline ``items`` on a plan-written apply_to_each, or in a skill step's
  args, run one call per item.
- An ``inputs_ref``, a skill's ``items_ref`` or a state_op ``source`` that
  names a step in the same plan makes the step wait for that step.
- A ref naming nothing fails the step with BRT-APE-006, makes no model call,
  and reaches re_plan as an ordinary step failure.

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
PER_ITEM = "Rate the sentiment of this review."
EXTRACT = "Extract the following fields"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "inline_refs.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-inline")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _lister(req):
    if "List the reviews." in req.system:
        return {"items": REVIEWS}
    return {"result": "ok"}


LIST_STEP = {"type": "answerer", "id": "n0", "instruction": "List the reviews.",
             "task_type": "extraction", "tier": "light", "output_schema": {"items": "list"}}


def _plain_step(**extra):
    node = {"type": "apply_to_each", "id": "n1", "instruction": PER_ITEM,
            "task_type": "extraction", "tier": "light",
            "output_schema": {"sentiment": "string"}}
    node.update(extra)
    return node


def _skill_step(skill_args_extra=None, **node_extra):
    args = {"concurrency_limit": 3,
            "fields": [{"name": "product", "type": "string", "description": "the product"}]}
    args.update(skill_args_extra or {})
    node = {"type": "apply_to_each", "id": "n1", "skill_id": "extract_fields",
            "skill_args": args}
    node.update(node_extra)
    return node


def _calls_with(stub, fragment):
    return [r for r in stub.of_kind("answerer") if fragment in r.system]


def _rows(db_path, env_type):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE type=? ORDER BY rowid",
            (env_type,),
        ).fetchall()


def _resend_waiting_once(waiting):
    """re_plan: continue once with the steps that waited, then compose."""
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": waiting, "rationale": "wave 2"}
    return replan


# --- 1. inline items, plain path ------------------------------------------

@pytest.mark.asyncio
async def test_plan_apply_to_each_with_inline_items_runs_one_call_per_item(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain_step(items=REVIEWS)]).install(mp)
    # The reviews stay out of the operator's text: it is in every prompt.
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    calls = _calls_with(stub, PER_ITEM)
    assert len(calls) == len(REVIEWS), f"{len(calls)} per-item calls"
    for review in REVIEWS:
        assert sum(review in r.system for r in calls) == 1, review


# --- 2. inline items, skill path ------------------------------------------

@pytest.mark.parametrize("shape", ["skill_args_items", "items_ref_list", "node_items"])
@pytest.mark.asyncio
async def test_skill_step_with_inline_items_runs_one_call_per_item(env, shape):
    db_path, mp = env
    if shape == "skill_args_items":
        step = _skill_step({"items": REVIEWS})
    elif shape == "items_ref_list":
        step = _skill_step({"items_ref": REVIEWS})
    else:
        step = _skill_step(items=REVIEWS)
    stub = StubModels(plan_nodes=[step]).install(mp)
    result = await run_pipeline("Extract the product from each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    calls = _calls_with(stub, EXTRACT)
    assert len(calls) == len(REVIEWS), f"{len(calls)} per-item calls"
    for review in REVIEWS:
        assert sum(review in r.system for r in calls) == 1, review
    rows = _rows(db_path, "apply_to_each")
    assert [r[0] for r in rows] == ["resolved"], rows


# --- 3. same-plan refs count as dependencies --------------------------------

@pytest.mark.asyncio
async def test_inputs_ref_to_a_step_in_the_same_plan_waits_for_it(env):
    db_path, mp = env
    step = _plain_step(inputs_ref="n0")
    stub = StubModels(plan_nodes=[LIST_STEP, step], answer_for=_lister,
                      replan_for=_resend_waiting_once([step])).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    rows = _rows(db_path, "apply_to_each")
    # One run of n1, after n0: no early, empty-handed run in the first wave.
    assert [r[0] for r in rows] == ["resolved"], rows
    calls = _calls_with(stub, PER_ITEM)
    assert len(calls) == len(REVIEWS)
    for review in REVIEWS:
        assert sum(review in r.system for r in calls) == 1, review


@pytest.mark.asyncio
async def test_skill_items_ref_to_a_step_in_the_same_plan_waits_for_it(env):
    db_path, mp = env
    step = _skill_step({"items_ref": "n0"})
    stub = StubModels(plan_nodes=[LIST_STEP, step], answer_for=_lister,
                      replan_for=_resend_waiting_once([step])).install(mp)
    result = await run_pipeline("Extract the product from each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    rows = _rows(db_path, "apply_to_each")
    assert [r[0] for r in rows] == ["resolved"], rows
    assert len(_calls_with(stub, EXTRACT)) == len(REVIEWS)


@pytest.mark.asyncio
async def test_state_op_source_in_the_same_plan_waits_for_it(env):
    db_path, mp = env
    step = {"type": "state_op", "id": "n1", "op": "count", "source": "n0", "params": {}}
    StubModels(plan_nodes=[LIST_STEP, step], answer_for=_lister,
               replan_for=_resend_waiting_once([step])).install(mp)
    result = await run_pipeline("Count the reviews.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    rows = _rows(db_path, "state_op")
    assert [r[0] for r in rows] == ["resolved"], rows
    assert '"count":3' in rows[0][1].replace(" ", "")


# --- 4. a ref naming nothing fails loudly -----------------------------------

def _assert_unresolved_failure(stub, db_path, name):
    assert not stub.of_kind("answerer"), "a step with no items must make no model call"
    rows = _rows(db_path, "apply_to_each")
    assert len(rows) == 1 and rows[0][0] == "failed", rows
    payload = rows[0][1]
    assert "BRT-APE-006" in payload
    assert f"Not a step id: '{name}'" in payload
    assert "items" in payload
    # The failure reaches re_plan as an ordinary step failure.
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    assert any("BRT-APE-006" in (r.system + r.user) for r in replans)


@pytest.mark.asyncio
async def test_inputs_ref_naming_nothing_fails_with_ape_006_and_no_model_call(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain_step(inputs_ref="reviews")]).install(mp)
    await run_pipeline("Rate each review: a; b; c", db_path=db_path)
    await drain_log_writes()
    _assert_unresolved_failure(stub, db_path, "reviews")


@pytest.mark.asyncio
async def test_skill_items_ref_naming_nothing_fails_with_ape_006_and_no_model_call(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_skill_step({"items_ref": "reviews_list"})]).install(mp)
    await run_pipeline("Extract the product from each review: a; b; c", db_path=db_path)
    await drain_log_writes()
    _assert_unresolved_failure(stub, db_path, "reviews_list")


@pytest.mark.asyncio
async def test_inline_items_win_over_a_ref_naming_nothing(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain_step(inputs_ref="reviews", items=REVIEWS)]).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(_calls_with(stub, PER_ITEM)) == len(REVIEWS)
