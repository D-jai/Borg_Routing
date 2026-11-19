"""Review of 158c424 (ruling 1a, 10 findings): BUILD.md section 8.

1. Within one plan the engine dispatches waves until no step is ready, then
   calls re_plan: a step held back by a ref or depends_on is never dropped.
2. A step that can never run fails loudly before re_plan: a ref cycle gets
   BRT-ORC-024; a ref to a failed step gets BRT-APE-005.
3. ``items_ref ""`` is filled only for apply_to_each skills.
4. Any ``*_ref`` skill arg given as a list is inline items.
5. An empty list is BRT-APE-005; a non-string, non-list ref is BRT-APE-006
   with a type message.
6. Plain path: ``inputs_ref`` given as a list is inline items.
7. ``documents`` is reserved only while the lane is on.
8. A raw apply_to_each's BRT-APE-005/006 failure uses the lane's code+doc shape.
9. The widened taint check (``evidence_ref``, ``failed_items_ref``).
10. Child runs: BRT-APE-006 says "in this run"; parent step ids do not resolve.

The real engine, executor and router run against the scripted stand-in
model server and temp databases. Nothing here needs a key or the network.
"""
import json
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, lane_matrix
from tests.e2e.test_inline_items_and_refs import (  # noqa: F401  (env is a fixture)
    EXTRACT, LIST_STEP, PER_ITEM, REVIEWS, _calls_with, _lister, _plain_step, _rows,
    _skill_step, env,
)


def _replan_block(stub, index: int = 0) -> dict:
    """The stage_results block of a re_plan request, parsed."""
    replans = stub.of_kind("re_plan")
    assert len(replans) > index, f"{len(replans)} re_plan calls"
    text = replans[index].system
    start = text.index("{", text.index("Stage results so far"))
    block, _ = json.JSONDecoder().raw_decode(text[start:])
    return block


# --- 1. held steps are never dropped ----------------------------------------

@pytest.mark.asyncio
async def test_inputs_ref_step_runs_before_replan_when_replan_composes_at_once(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[LIST_STEP, _plain_step(inputs_ref="n0")],
                      answer_for=_lister).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    calls = _calls_with(stub, PER_ITEM)
    assert len(calls) == len(REVIEWS), f"{len(calls)} per-item calls"
    for review in REVIEWS:
        assert sum(review in r.system for r in calls) == 1, review
    assert [r[0] for r in _rows(db_path, "apply_to_each")] == ["resolved"]
    # n1 ran before the one re_plan call, which sees both steps.
    assert len(stub.of_kind("re_plan")) == 1
    assert set(_replan_block(stub)) == {"n0", "n1"}


@pytest.mark.asyncio
async def test_depends_on_step_runs_before_replan_when_replan_composes_at_once(env):
    db_path, mp = env
    second = {"type": "answerer", "id": "n1", "instruction": "Summarise the list.",
              "depends_on": ["n0"], "task_type": "extraction", "tier": "light",
              "output_schema": {"result": "string"}}
    stub = StubModels(plan_nodes=[LIST_STEP, second], answer_for=_lister).install(mp)
    result = await run_pipeline("Summarise the reviews.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    summary = stub.step_request("Summarise the list.")
    assert REVIEWS[0] in summary.system, "n1 did not see n0's output"
    assert len(stub.of_kind("re_plan")) == 1
    assert set(_replan_block(stub)) == {"n0", "n1"}


@pytest.mark.asyncio
async def test_replan_next_nodes_join_steps_still_held(env):
    """A step held in round 1 is not lost when re_plan adds steps."""
    db_path, mp = env
    extra = {"type": "answerer", "id": "n9", "instruction": "Say thanks.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}
    sent = []

    def replan(_req):
        if sent:
            return {"action": "compose", "rationale": "done"}
        sent.append(1)
        return {"action": "continue", "next_nodes": [extra], "rationale": "one more"}

    stub = StubModels(plan_nodes=[LIST_STEP, _plain_step(inputs_ref="n0")],
                      answer_for=_lister, replan_for=replan).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(_calls_with(stub, PER_ITEM)) == len(REVIEWS)
    assert len(_calls_with(stub, "Say thanks.")) == 1


# --- 2. a step that can never run fails loudly ------------------------------

@pytest.mark.asyncio
async def test_ref_cycle_fails_both_steps_with_orc_024_before_replan(env):
    db_path, mp = env
    a = _plain_step(inputs_ref="n2")
    b = _plain_step(inputs_ref="n1")
    b["id"] = "n2"
    stub = StubModels(plan_nodes=[a, b]).install(mp)
    await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer"), "a step in a cycle must make no model call"
    block = _replan_block(stub)
    for nid in ("n1", "n2"):
        failure = block[nid]["failure"]
        assert failure["code"] == "BRT-ORC-024", failure
        assert "n1 -> n2 -> n1" in failure["message"] or "n2 -> n1 -> n2" in failure["message"]


@pytest.mark.asyncio
async def test_depends_on_cycle_fails_with_orc_024(env):
    db_path, mp = env
    a = {"type": "answerer", "id": "n1", "instruction": "A.", "depends_on": ["n2"]}
    b = {"type": "answerer", "id": "n2", "instruction": "B.", "depends_on": ["n1"]}
    stub = StubModels(plan_nodes=[a, b]).install(mp)
    await run_pipeline("Do it.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer")
    block = _replan_block(stub)
    assert block["n1"]["failure"]["code"] == "BRT-ORC-024"
    assert block["n2"]["failure"]["code"] == "BRT-ORC-024"


def _fail_lister(req):
    return "length" if "List the reviews." in req.system else "stop"


@pytest.mark.parametrize("path", ["plain", "skill"])
@pytest.mark.asyncio
async def test_ref_to_a_failed_step_fails_with_ape_005_and_no_model_call(env, path):
    db_path, mp = env
    step = _plain_step(inputs_ref="n0") if path == "plain" else _skill_step({"items_ref": "n0"})
    stub = StubModels(plan_nodes=[LIST_STEP, step], answer_for=_lister,
                      finish_for=_fail_lister).install(mp)
    await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert not _calls_with(stub, PER_ITEM) and not _calls_with(stub, EXTRACT)
    rows = _rows(db_path, "apply_to_each")
    assert len(rows) == 1 and rows[0][0] == "failed", rows
    assert "BRT-APE-005" in rows[0][1]
    assert "'n0'" in rows[0][1] and "failed" in rows[0][1]
    assert _replan_block(stub)["n1"]["failure"]["code"] == "BRT-APE-005"


# --- 3. items_ref "" for apply_to_each skills and data-reading answerers ----
# Skill render review (16-Nov-2026): an answerer skill that reads data
# (compare_and_rank, verify_claim) takes inline items in its context, so its
# ref may be left out when the step carries items. With neither, SKL-005.

@pytest.mark.asyncio
async def test_answerer_skill_with_inline_items_and_no_items_ref_runs_on_them(env):
    db_path, mp = env
    step = {"type": "answerer", "id": "n1", "skill_id": "compare_and_rank",
            "skill_args": {"items": REVIEWS,
                           "criteria": [{"name": "q", "weight": 1.0, "description": "quality"}]}}
    stub = StubModels(plan_nodes=[step]).install(mp)
    await run_pipeline("Rank these.", db_path=db_path)
    await drain_log_writes()
    calls = stub.of_kind("answerer")
    assert len(calls) == 1
    for review in REVIEWS:
        assert review in calls[0].system


@pytest.mark.asyncio
async def test_answerer_skill_with_no_items_and_no_items_ref_keeps_skl_005(env):
    db_path, mp = env
    step = {"type": "answerer", "id": "n1", "skill_id": "compare_and_rank",
            "skill_args": {"criteria": [{"name": "q", "weight": 1.0, "description": "quality"}]}}
    stub = StubModels(plan_nodes=[step]).install(mp)
    await run_pipeline("Rank these.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer"), "the skill must not run without items_ref"
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-ORC-008"
    assert "BRT-SKL-005" in failure["message"]


# --- 4. any *_ref arg given as a list is inline items -----------------------

@pytest.mark.asyncio
async def test_retry_escalate_failed_items_ref_list_runs_one_call_per_item(env):
    db_path, mp = env
    step = {"type": "apply_to_each", "id": "n1", "skill_id": "retry_escalate",
            "skill_args": {"failed_items_ref": REVIEWS, "original_instruction": PER_ITEM,
                           "original_output_schema": {"s": "string"}, "escalation_note": ""}}
    stub = StubModels(plan_nodes=[step]).install(mp)
    result = await run_pipeline("Retry these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    calls = _calls_with(stub, PER_ITEM)
    assert len(calls) == len(REVIEWS), f"{len(calls)} per-item calls"
    for review in REVIEWS:
        assert sum(review in r.system for r in calls) == 1, review
    assert [r[0] for r in _rows(db_path, "apply_to_each")] == ["resolved"]


# --- 5. empty list and wrong-type refs --------------------------------------

@pytest.mark.parametrize("path", ["plain", "skill"])
@pytest.mark.asyncio
async def test_empty_list_ref_fails_with_ape_005(env, path):
    db_path, mp = env
    step = _plain_step(inputs_ref=[]) if path == "plain" else _skill_step({"items_ref": []})
    stub = StubModels(plan_nodes=[step]).install(mp)
    await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer")
    rows = _rows(db_path, "apply_to_each")
    assert len(rows) == 1 and rows[0][0] == "failed", rows
    assert "BRT-APE-005" in rows[0][1]
    assert "BRT-APE-006" not in rows[0][1]


@pytest.mark.parametrize("path,value", [("plain", {"a": 1}), ("plain", 5),
                                        ("skill", {"a": 1}), ("skill", 5)])
@pytest.mark.asyncio
async def test_non_string_non_list_ref_fails_with_a_type_message(env, path, value):
    db_path, mp = env
    step = _plain_step(inputs_ref=value) if path == "plain" else _skill_step({"items_ref": value})
    stub = StubModels(plan_nodes=[step]).install(mp)
    await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer")
    rows = _rows(db_path, "apply_to_each")
    assert len(rows) == 1 and rows[0][0] == "failed", rows
    assert "BRT-APE-006" in rows[0][1]
    assert "must be a step id or a list of items" in rows[0][1]


# --- 6. plain inputs_ref given as a list ------------------------------------

@pytest.mark.asyncio
async def test_plain_inputs_ref_list_runs_one_call_per_item(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain_step(inputs_ref=REVIEWS)]).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    calls = _calls_with(stub, PER_ITEM)
    assert len(calls) == len(REVIEWS), f"{len(calls)} per-item calls"
    rows = _rows(db_path, "apply_to_each")
    assert [r[0] for r in rows] == ["resolved"], rows
    assert json.loads(rows[0][1])["output"]["total"] == len(REVIEWS)


# --- 7. "documents" is reserved only with the lane on -----------------------

@pytest.mark.asyncio
async def test_documents_ref_with_the_lane_off_and_no_such_step_fails_ape_006(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain_step(inputs_ref="documents")]).install(mp)
    await run_pipeline("Rate each doc.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer")
    rows = _rows(db_path, "apply_to_each")
    assert len(rows) == 1 and rows[0][0] == "failed", rows
    assert "BRT-APE-006" in rows[0][1] and "'documents'" in rows[0][1]


@pytest.mark.asyncio
async def test_step_named_documents_with_the_lane_off_is_waited_for(env):
    db_path, mp = env
    lister = dict(LIST_STEP, id="documents")
    stub = StubModels(plan_nodes=[lister, _plain_step(inputs_ref="documents")],
                      answer_for=_lister).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(_calls_with(stub, PER_ITEM)) == len(REVIEWS)
    assert [r[0] for r in _rows(db_path, "apply_to_each")] == ["resolved"]


# --- 8 and 9. the lane: raw failures and the widened taint -------------------

NOTE = ("Client note for Acme Holdings. Exposure grew in the third quarter after two "
        "late payments on the equipment lease, and the treasurer has resigned.")
READ_NOTE = "Give the client name."


@pytest.fixture
async def lane_env(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "acme_note.txt").write_text(NOTE, encoding="utf-8")
    db_path = str(tmp_path / "lane_refs.db")
    await create_all(db_path)
    for name in ("BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
                 "BORG_LANE_MAX_OUTPUT_CHARS", "BORG_LANE_MAX_VERBATIM_CHARS",
                 "BORG_LANE_MAX_NGRAM_SHARE", "BORG_LANE_PART_CHARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-lane-refs")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", lane_matrix(local_score=9.0))
    return db_path, monkeypatch


# n1 reads the document and copies it whole: the guard withholds it, so n1
# stays raw and unguarded, and every step that reads n1 is raw too.
RAW_READER = {"type": "apply_to_each", "id": "n1", "inputs_ref": "documents",
              "instruction": READ_NOTE, "task_type": "extraction", "tier": "light",
              "output_schema": {"client": "string"}}


def _copy_note(req):
    if READ_NOTE in req.system:
        return {"client": NOTE}
    return {"result": "ok"}


async def _lane_run(lane_env, plan):
    db_path, mp = lane_env
    stub = StubModels(plan_nodes=plan, answer_for=_copy_note).install(mp)
    await run_pipeline("Read the note.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    return stub


@pytest.mark.asyncio
async def test_raw_apply_to_each_ref_failure_uses_the_lane_code_shape(lane_env):
    n2 = {"type": "apply_to_each", "id": "n2", "depends_on": ["n1"], "inputs_ref": "nope",
          "instruction": "Rate it.", "task_type": "extraction", "tier": "light",
          "output_schema": {"r": "string"}}
    stub = await _lane_run(lane_env, [RAW_READER, n2])
    replan = stub.of_kind("re_plan")[0]
    assert _replan_block(stub)["n2"] == {"failure": {"code": "BRT-APE-006", "doc": None}}
    body = json.dumps(replan.body)
    assert "belongs in `items`" not in body


def _envelope_for(db_path, fragment):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT outbound_payload FROM envelopes").fetchall()
    hits = [json.loads(r[0]) for r in rows if fragment in r[0]]
    assert hits, f"no envelope carries {fragment!r}"
    return hits[0]


@pytest.mark.asyncio
async def test_verify_claim_evidence_ref_to_a_raw_step_is_raw_and_local(lane_env):
    n2 = {"type": "answerer", "id": "n2", "skill_id": "verify_claim",
          "skill_args": {"claim": "Acme is low risk.", "evidence_ref": "n1"}}
    stub = await _lane_run(lane_env, [RAW_READER, n2])
    calls = [r for r in stub.of_kind("answerer") if "Acme is low risk." in r.system]
    assert len(calls) == 1, f"{len(calls)} verify_claim calls"
    assert calls[0].is_local, "a step reading a raw, unguarded step must stay local"
    assert _envelope_for(lane_env[0], "Acme is low risk.")["data_scope"] == "raw"


@pytest.mark.asyncio
async def test_retry_escalate_failed_items_ref_to_a_raw_step_is_raw_and_local(lane_env):
    n2 = {"type": "apply_to_each", "id": "n2", "skill_id": "retry_escalate",
          "skill_args": {"failed_items_ref": "n1", "original_instruction": "Retry RETRY-MARK.",
                         "original_output_schema": {"s": "string"}, "escalation_note": ""}}
    stub = await _lane_run(lane_env, [RAW_READER, n2])
    assert _envelope_for(lane_env[0], "RETRY-MARK")["data_scope"] == "raw"
    calls = [r for r in stub.of_kind("answerer") if "RETRY-MARK" in r.system]
    assert all(r.is_local for r in calls), "a retry of raw items must stay local"


# --- 10. child runs ----------------------------------------------------------

CHILD = "Rate each review the parent listed."


@pytest.mark.asyncio
async def test_child_run_ref_to_a_parent_step_fails_ape_006_naming_this_run(env):
    db_path, mp = env
    parent_plan = [LIST_STEP, {"type": "orchestrate", "id": "n1", "instruction": CHILD,
                               "depends_on": ["n0"]}]
    child_plan = [_plain_step(inputs_ref="n0")]
    plans_sent = []

    def plan_for(_req):
        # The first plan call is the parent's; the next is the child run's.
        plans_sent.append(1)
        return parent_plan if len(plans_sent) == 1 else child_plan

    stub = StubModels(plan_nodes=parent_plan, answer_for=_lister, plan_for=plan_for).install(mp)
    await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert not _calls_with(stub, PER_ITEM), "the child must not reach the parent's n0"
    rows = _rows(db_path, "apply_to_each")
    assert len(rows) == 1 and rows[0][0] == "failed", rows
    payload = rows[0][1]
    assert "BRT-APE-006" in payload
    assert "of this run, not a parent" in payload
    assert "parent" in payload

