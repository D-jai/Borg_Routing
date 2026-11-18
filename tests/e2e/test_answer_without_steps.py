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


# --- Planner ruling 26f: an apply_to_each with no successful item is not a
# finished step; one successful item is. ----------------------------------------

APE_ALL_FAIL = {"type": "apply_to_each", "id": "n1", "instruction": "Tag this row.",
                "items": ["row one", "row two"], "output_schema": {"tag": "string"},
                "tier": "light", "task_type": "extraction"}


def _tag_only_row_one(req):
    # A reply without "tag" fails the item (BRT-ANS-005, missing required key).
    return {"tag": "one"} if "row one" in req.system else {"nothing": True}


@pytest.mark.asyncio
async def test_f_all_items_failed_no_documents_is_planner_only(env):
    db_path, mp = env
    StubModels(plan_nodes=[APE_ALL_FAIL], answer_for=lambda r: {"nothing": True},
               compose_text=COMPOSED).install(mp)
    result = await run_pipeline("Tag these rows.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert await _answer_kind(db_path, result["run_id"]) == "planner_only"
    events = _events(db_path, "BRT-ORC-033")
    assert len(events) == 1 and events[0]["failed_steps"] == 1, events
    assert events[0]["step_codes"], events


@pytest.mark.asyncio
async def test_f_one_successful_item_is_a_finished_step(env):
    db_path, mp = env
    StubModels(plan_nodes=[APE_ALL_FAIL], answer_for=_tag_only_row_one,
               compose_text=COMPOSED).install(mp)
    result = await run_pipeline("Tag these rows.", db_path=db_path)
    await drain_log_writes()
    assert await _answer_kind(db_path, result["run_id"]) == "composed"
    assert _events(db_path, "BRT-ORC-033") == []


async def test_f_all_items_failed_over_documents_is_brt_orc_034(lane):
    # The local model answers every document item without the schema's keys.
    stub, result = await _run(lane, "raw_local", lane_matrix(), compose_text=COMPOSED,
                              answer_for=lambda r: {"nothing": True})
    assert result["status"] == "failed", result
    assert stub.of_kind("compose") == []
    assert "BRT-ORC-034" in (result["error"] or "")
    for canary in CANARIES:
        assert canary not in (result["error"] or "")


# --- Edge under 26b: a child run given documents in all_local ------------------

CHILD_TEXT = "INVENTED BY THE CHILD PLANNER"


async def test_child_with_documents_whose_steps_all_fail_hands_the_parent_a_failure(lane):
    parent_plan = [{"type": "orchestrate", "id": "n1", "instruction": "Assess the notes.",
                    "documents": ["doc_001"], "task_type": "reasoning", "tier": "light",
                    "output_schema": {"result": "string"}}]
    child_plan = [{"type": "state_op", "id": "c1", "op": "sort", "items": [{"a": 1}]}]

    def plan_for(req):
        return child_plan if "Your Assignment In This Run" in req.system else parent_plan

    def compose_for(req):
        if "Your Assignment In This Run" in req.system:
            return {"response": CHILD_TEXT}
        return {"response": COMPOSED}

    stub, result = await _run(lane, "all_local", lane_matrix(), plan=parent_plan,
                              plan_for=plan_for, compose_for=compose_for)
    assert len(stub.of_kind("plan")) == 2, "the child planner should have run"
    # The child's invented text never reaches the parent as a step result.
    for req in stub.requests:
        if "Your Assignment In This Run" not in req.system:
            assert CHILD_TEXT not in req.system + req.user, req.kind
    assert CHILD_TEXT not in (result["response"] or "")
    # The child planned no document step, so 26b does not apply inside it
    # (round 2 of 82d3da4): it ends planner_only and keeps its answer (ruling
    # 26g), and the parent's step fails with BRT-ORC-022 carrying BRT-ORC-033.
    with sqlite3.connect(lane["db"]) as conn:
        child = conn.execute(
            "SELECT run_id, status, answer_kind FROM runs WHERE parent_run_id=?",
            (result["run_id"],),
        ).fetchall()
        child_events = [
            json.loads(r[0]) for r in conn.execute(
                "SELECT decision_json FROM logs WHERE event_type='error' AND run_id=?",
                (child[0][0],),
            ).fetchall()
        ]
    assert [(c[1], c[2]) for c in child] == [("completed", "planner_only")]
    assert any(e.get("code") == "BRT-ORC-033" for e in child_events), child_events
    replan = [r for r in stub.of_kind("re_plan")
              if "Your Assignment In This Run" not in r.system]
    assert replan and "BRT-ORC-022" in replan[0].system + replan[0].user
    # ... and the parent, with no document step that finished, fails too.
    assert result["status"] == "failed", result
    assert "BRT-ORC-034" in (result["error"] or "")
    assert "not defined" not in json.dumps(child_events)


# --- Round 2 of 82d3da4: a child of a step that names no documents ------------

PARENT_READ = "Summarise the client note."
CHILD_READ = "Child: rate the client in the note."


def _is_child(req):
    return "Your Assignment In This Run" in req.system


async def test_child_that_plans_its_own_document_step_answers_to_26b(lane):
    """all_local: n1 reads doc_001 and finishes; n2 is an orchestrate step with
    no documents; its child plans one answerer over doc_001, which fails
    (BRT-ANS-005). The child fails with BRT-ORC-034; its text never reaches
    the parent."""
    parent_plan = [
        {"type": "answerer", "id": "n1", "instruction": PARENT_READ, "documents": ["doc_001"],
         "task_type": "reasoning", "tier": "light", "output_schema": {"result": "string"}},
        {"type": "orchestrate", "id": "n2", "instruction": "Rate the client.",
         "task_type": "reasoning", "tier": "light", "output_schema": {"result": "string"}},
    ]
    child_plan = [{"type": "answerer", "id": "c1", "instruction": CHILD_READ,
                   "documents": ["doc_001"], "task_type": "reasoning", "tier": "light",
                   "output_schema": {"risk": "string"}}]

    def answers(req):
        if CHILD_READ in req.system:
            return {"nothing": True}          # misses "risk": BRT-ANS-005
        return {"result": "a short summary"}

    stub, result = await _run(
        lane, "all_local", lane_matrix(), plan=parent_plan,
        plan_for=lambda r: child_plan if _is_child(r) else parent_plan,
        compose_for=lambda r: {"response": CHILD_TEXT if _is_child(r) else COMPOSED},
        answer_for=answers)
    with sqlite3.connect(lane["db"]) as conn:
        child = conn.execute("SELECT run_id, status FROM runs WHERE parent_run_id=?",
                             (result["run_id"],)).fetchall()
        child_codes = [json.loads(r[0]).get("code") for r in conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error' AND run_id=?",
            (child[0][0],)).fetchall()]
    assert [c[1] for c in child] == ["failed"], child
    assert "BRT-ORC-034" in child_codes, child_codes
    for req in stub.requests:
        if not _is_child(req):
            assert CHILD_TEXT not in req.system + req.user, req.kind
    assert CHILD_TEXT not in (result["response"] or "")
    assert result["status"] == "completed", result.get("error")   # n1 finished


# --- Planner ruling 26g: a planner_only child is a failed step in its parent ----

@pytest.mark.asyncio
async def test_g_planner_only_child_is_a_failed_step_and_its_text_stays_down(env):
    db_path, mp = env
    parent_plan = [{"type": "orchestrate", "id": "n1", "instruction": "Solve the sub-problem.",
                    "task_type": "reasoning", "tier": "light",
                    "output_schema": {"result": "string"}}]
    child_plan = [BAD_STEP]
    stub = StubModels(
        plan_nodes=parent_plan,
        plan_for=lambda r: child_plan if _is_child(r) else parent_plan,
        compose_for=lambda r: {"response": CHILD_TEXT if _is_child(r) else COMPOSED},
    ).install(mp)
    result = await run_pipeline("Work it out.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    for req in stub.requests:
        if not _is_child(req):
            assert CHILD_TEXT not in req.system + req.user, req.kind
    assert CHILD_TEXT not in (result["response"] or "")
    with sqlite3.connect(db_path) as conn:
        env_row = conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE run_id=? AND type='orchestrate'",
            (result["run_id"],)).fetchone()
        child = conn.execute("SELECT run_id, status, answer_kind FROM runs WHERE parent_run_id=?",
                             (result["run_id"],)).fetchone()
    assert env_row[0] == "failed", env_row
    reason = json.loads(env_row[1])["failure_reason"]
    assert "BRT-ORC-022" in reason and "BRT-ORC-033" in reason and "BRT-ORC-004" in reason
    # re_plan sees the failure and the child's step codes within 200 characters.
    replan = stub.of_kind("re_plan")
    replan = [r for r in replan if not _is_child(r)][0]
    assert "BRT-ORC-033" in replan.system + replan.user
    assert len(reason) <= 213
    # The child keeps its planner_only answer; the parent, with no other step, is planner_only.
    assert child[1] == "completed" and child[2] == "planner_only"
    assert await _answer_kind(db_path, result["run_id"]) == "planner_only"


@pytest.mark.asyncio
async def test_g_child_that_answers_directly_still_resolves(env):
    db_path, mp = env
    parent_plan = [{"type": "orchestrate", "id": "n1", "instruction": "Solve the sub-problem.",
                    "task_type": "reasoning", "tier": "light",
                    "output_schema": {"result": "string"}}]

    def plan_content(req):
        if _is_child(req):
            return {"direct_answer": "The child answered directly.", "plan": None,
                    "rationale": "no steps needed"}
        return {"plan": {"nodes": parent_plan}, "rationale": "scripted plan"}

    StubModels(plan_nodes=parent_plan, plan_content_for=plan_content,
               compose_text=COMPOSED).install(mp)
    result = await run_pipeline("Work it out.", db_path=db_path)
    await drain_log_writes()
    with sqlite3.connect(db_path) as conn:
        env_row = conn.execute(
            "SELECT status FROM envelopes WHERE run_id=? AND type='orchestrate'",
            (result["run_id"],)).fetchone()
        child = conn.execute("SELECT answer_kind FROM runs WHERE parent_run_id=?",
                             (result["run_id"],)).fetchone()
    assert child[0] == "direct"
    assert env_row[0] == "resolved"
    assert await _answer_kind(db_path, result["run_id"]) == "composed"
