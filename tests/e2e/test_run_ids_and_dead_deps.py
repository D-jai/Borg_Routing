"""Review of 3ce6e16, round 2 (3): run-unique step ids and dead dependencies.

1. An id the engine gives a step that has none is unique across the whole
   run: never a key already in the results, an id already in the plan, or an
   id given earlier in the run. Explicit planner ids are kept unchanged.
2. A step whose ``depends_on`` step failed (cycle-failed included) fails with
   BRT-ORC-025 before any model call; the message names the failed step. A raw
   (on-premise lane) step uses the lane's code+doc shape. Item refs keep
   BRT-APE-005.

The real engine, executor and router run against the scripted stand-in
model server and temp databases. Nothing here needs a key or the network.
"""
import pytest

from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes
from tests.e2e.test_inline_items_and_refs import (  # noqa: F401  (env is a fixture)
    EXTRACT, LIST_STEP, PER_ITEM, REVIEWS, _calls_with, _lister, _plain_step, _rows,
    _skill_step, env,
)
from tests.e2e.test_inline_refs_review import (  # noqa: F401  (lane_env is a fixture)
    _lane_run, _replan_block, lane_env,
)


def _ans(instr, **extra):
    node = {"type": "answerer", "instruction": instr, "task_type": "extraction",
            "tier": "light", "output_schema": {"result": "string"}}
    node.update(extra)
    return node


def _once(nodes):
    """re_plan: continue once with ``nodes``, then compose."""
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": nodes, "rationale": "more"}
    return replan


def _length_for(fragment):
    """finish_for: cut the reply of the step whose prompt carries ``fragment``."""
    def finish(req):
        return "length" if fragment in req.system else "stop"
    return finish


# --- 1. run-unique generated ids ---------------------------------------------

def test_with_ids_never_reuses_a_taken_explicit_or_earlier_generated_id():
    taken = {"n0", "n2"}
    first = engine._with_ids([{"instruction": "a"}, {"id": "n1"}, {"instruction": "c"}], taken)
    ids = [n["id"] for n in first]
    assert ids[1] == "n1", "an explicit id is kept"
    assert len(set(ids)) == 3
    assert not ({ids[0], ids[2]} & {"n0", "n1", "n2"})
    second = engine._with_ids([{"instruction": "d"}, {"instruction": "e"}], taken)
    second_ids = {n["id"] for n in second}
    assert len(second_ids) == 2
    assert not (second_ids & (set(ids) | {"n0", "n2"}))


@pytest.mark.asyncio
async def test_idless_replan_step_runs_after_an_idless_plan(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_ans("First AAA.")],
                      replan_for=_once([_ans("Second BBB.")])).install(mp)
    await run_pipeline("Do two things.", db_path=db_path)
    await drain_log_writes()
    assert len(_calls_with(stub, "First AAA.")) == 1
    assert len(_calls_with(stub, "Second BBB.")) == 1, "re_plan's id-less step never ran"


@pytest.mark.asyncio
async def test_held_step_runs_when_an_idless_sibling_follows(env):
    db_path, mp = env
    plan = [_ans("Alpha GGG.", id="a"), _ans("Held HHH.", id="n2", depends_on=["a"]),
            _ans("Idless III.")]
    stub = StubModels(plan_nodes=plan).install(mp)
    await run_pipeline("Do three things.", db_path=db_path)
    await drain_log_writes()
    assert len(_calls_with(stub, "Idless III.")) == 1
    assert len(_calls_with(stub, "Held HHH.")) == 1, "held n2 never ran"
    assert len(_replan_block(stub)) == 3


@pytest.mark.asyncio
async def test_explicit_n1_and_an_idless_sibling_keep_both_results(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_ans("Alpha DDD.", id="n1"), _ans("Beta EEE.")]).install(mp)
    await run_pipeline("Do two things.", db_path=db_path)
    await drain_log_writes()
    assert len(_calls_with(stub, "Alpha DDD.")) == 1
    assert len(_calls_with(stub, "Beta EEE.")) == 1
    block = _replan_block(stub)
    assert len(block) == 2, f"re_plan sees {sorted(block)}: a result was overwritten"
    assert "n1" in block


# --- 2. a step whose depends_on step failed ----------------------------------

FAILS = "List the reviews."   # LIST_STEP's instruction; cut by the token cap


@pytest.mark.asyncio
async def test_answerer_depending_on_a_failed_step_makes_no_call(env):
    db_path, mp = env
    plan = [LIST_STEP, _ans("Summarise FFF.", id="n1", depends_on=["n0"])]
    stub = StubModels(plan_nodes=plan, answer_for=_lister,
                      finish_for=_length_for(FAILS)).install(mp)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    assert not _calls_with(stub, "Summarise FFF."), "ran against a failed upstream"
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-ORC-025", failure
    assert "'n0'" in failure["message"]
    assert len(failure["message"]) < 200


@pytest.mark.asyncio
async def test_chain_of_three_stops_paying_after_the_first_failure(env):
    db_path, mp = env
    plan = [LIST_STEP, _ans("Step two JJJ.", id="n1", depends_on=["n0"]),
            _ans("Step three KKK.", id="n2", depends_on=["n1"])]
    stub = StubModels(plan_nodes=plan, answer_for=_lister,
                      finish_for=_length_for(FAILS)).install(mp)
    await run_pipeline("Do the chain.", db_path=db_path)
    await drain_log_writes()
    assert not _calls_with(stub, "Step two JJJ.")
    assert not _calls_with(stub, "Step three KKK.")
    block = _replan_block(stub)
    assert block["n1"]["failure"]["code"] == "BRT-ORC-025"
    assert block["n2"]["failure"]["code"] == "BRT-ORC-025"
    assert "'n1'" in block["n2"]["failure"]["message"]


@pytest.mark.asyncio
async def test_passing_dependency_still_runs_normally(env):
    db_path, mp = env
    plan = [LIST_STEP, _ans("Summarise LLL.", id="n1", depends_on=["n0"])]
    stub = StubModels(plan_nodes=plan, answer_for=_lister).install(mp)
    result = await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert REVIEWS[0] in stub.step_request("Summarise LLL.").system
    assert "failure" not in _replan_block(stub)["n1"]


def _dependent(kind):
    if kind == "apply_to_each":
        return _plain_step(items=REVIEWS, depends_on=["n0"])
    if kind == "skill":
        return _skill_step({"items_ref": REVIEWS}, depends_on=["n0"])
    if kind == "state_op":
        return {"type": "state_op", "id": "n1", "op": "count", "items": REVIEWS,
                "depends_on": ["n0"]}
    if kind == "orchestrate":
        return {"type": "orchestrate", "id": "n1", "instruction": "Plan MMM.",
                "depends_on": ["n0"]}
    return _ans("Answer MMM.", id="n1", depends_on=["n0"])


@pytest.mark.parametrize("kind", ["answerer", "apply_to_each", "skill", "state_op",
                                  "orchestrate"])
@pytest.mark.asyncio
async def test_every_step_type_depending_on_a_failed_step_fails_with_orc_025(env, kind):
    db_path, mp = env
    stub = StubModels(plan_nodes=[LIST_STEP, _dependent(kind)], answer_for=_lister,
                      finish_for=_length_for(FAILS)).install(mp)
    await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert [r for r in stub.of_kind("answerer") if FAILS not in r.system] == []
    assert len(stub.of_kind("plan")) == 1, "a child run was started"
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-ORC-025", failure
    assert "'n0'" in failure["message"]


@pytest.mark.asyncio
async def test_step_depending_on_a_cycle_failed_step_fails_with_orc_025(env):
    db_path, mp = env
    plan = [_ans("A.", id="n1", depends_on=["n2"]), _ans("B.", id="n2", depends_on=["n1"]),
            _ans("After NNN.", id="n3", depends_on=["n1"])]
    stub = StubModels(plan_nodes=plan).install(mp)
    await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer")
    block = _replan_block(stub)
    assert block["n1"]["failure"]["code"] == "BRT-ORC-024"
    assert block["n3"]["failure"]["code"] == "BRT-ORC-025"
    assert "'n1'" in block["n3"]["failure"]["message"]


@pytest.mark.asyncio
async def test_item_ref_to_a_failed_dependency_keeps_ape_005(env):
    db_path, mp = env
    step = _plain_step(inputs_ref="n0", depends_on=["n0"])
    stub = StubModels(plan_nodes=[LIST_STEP, step], answer_for=_lister,
                      finish_for=_length_for(FAILS)).install(mp)
    await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert not _calls_with(stub, PER_ITEM)
    assert _replan_block(stub)["n1"]["failure"]["code"] == "BRT-APE-005"


@pytest.mark.asyncio
async def test_raw_step_depending_on_a_failed_raw_step_uses_the_lane_shape(lane_env):
    # n1 reads the documents on an orchestrate step outside all_local: refused
    # (BRT-LANE-005). n2 reads n1, so it is raw; its failure is code + doc only.
    n1 = {"type": "orchestrate", "id": "n1", "inputs_ref": "documents",
          "instruction": "Plan over the note."}
    n2 = _ans("Use it OOO.", id="n2", depends_on=["n1"])
    stub = await _lane_run(lane_env, [n1, n2])
    assert not _calls_with(stub, "Use it OOO.")
    block = _replan_block(stub)
    assert block["n1"]["failure"]["code"] == "BRT-LANE-005"
    assert block["n2"] == {"failure": {"code": "BRT-ORC-025", "doc": None}}
