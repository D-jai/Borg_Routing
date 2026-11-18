"""Planner ruling 32d(ii)-(v) and readings under ruling 32 (3): a plan node key,
a skill_args key or a node type that nothing accepts is a plan error,
BRT-ORC-036, sent to re_plan once per run (shared with BRT-ORC-035). If the
plan repeats the fault, an unknown key is dropped and the step runs, recorded
three ways; an unknown type fails the step. Change 1: an accepted field that
builds nothing on the node's path is recorded (``accepted_unused``), not refused.
"""
import json
import re
import sqlite3

import pytest

from src.api.tasks import _redact_events
from src.db.schema import create_all
from src.orchestrator import engine
from src.orchestrator.plan_validation import (
    COMMON_FIELDS, NODE_FIELDS, accepted_unused, validate_plan,
)
from src.pipeline.runner import run_pipeline
from src.skills.library import SkillLibrary
from tests.e2e.test_privacy_lane import (  # noqa: F401  (lane is a fixture)
    CANARIES, _run as _lane_run, lane,
)
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

LIB = SkillLibrary()
LIB.load("data/skills.yaml")

BASE = {
    "answerer": {"type": "answerer", "id": "n1", "instruction": "Say hi.",
                 "output_schema": {"result": "string"}},
    "orchestrate": {"type": "orchestrate", "id": "n1", "instruction": "Sub-problem.",
                    "context": "c", "output_schema": {"result": "string"}},
    "apply_to_each": {"type": "apply_to_each", "id": "n1", "instruction": "Rate one.",
                      "items": ["a", "b"], "output_schema": {"r": "string"}},
    "state_op": {"type": "state_op", "id": "n1", "op": "count", "source": "n0",
                 "params": {}},
}


def _skill(**args_over):
    args = {"buckets": ["billing", "bug"], "items_ref": None}
    args.update(args_over)
    return {"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets",
            "skill_version": 1, "items": ["a", "b"], "skill_args": args,
            "tier": "light", "task_type": "extraction"}


def _issues(node):
    return validate_plan([node], LIB)


# --- (ii) unknown node keys ------------------------------------------------------

@pytest.mark.parametrize("node_type", sorted(BASE))
def test_an_unknown_node_key_is_a_plan_error_on_each_type(node_type):
    node = dict(BASE[node_type], colour="blue")
    issues = _issues(node)
    assert len(issues) == 1, issues
    issue = issues[0]
    assert issue.code == "BRT-ORC-036" and issue.fields == ("colour",)
    assert issue.node_id == "n1" and issue.skill_id == ""
    msg = issue.message
    assert msg.startswith("BRT-ORC-036") and len(msg) <= 200, msg
    assert "colour" in msg and node_type in msg
    own = [f for f in NODE_FIELDS[node_type] if f not in COMMON_FIELDS]
    assert own[0] in msg, msg     # names the accepted fields for that type


@pytest.mark.parametrize("node_type", sorted(BASE))
def test_the_base_node_of_each_type_passes(node_type):
    assert _issues(BASE[node_type]) == []


def test_a_node_without_a_type_is_read_as_an_answerer():
    node = dict(BASE["answerer"])
    del node["type"]
    assert _issues(node) == []
    assert _issues(dict(node, op="count"))[0].fields == ("op",)


def test_a_field_of_another_type_is_unknown_on_this_one():
    issues = _issues(dict(BASE["state_op"], output_schema={"x": "y"}))
    assert issues[0].code == "BRT-ORC-036" and issues[0].fields == ("output_schema",)


def test_state_query_stays_accepted():
    assert _issues(dict(BASE["answerer"], state_query="SELECT 1")) == []


def test_failed_items_only_is_accepted_at_node_level_and_in_skill_args():
    assert _issues(dict(BASE["apply_to_each"], failed_items_only=True)) == []
    assert _issues(_skill(failed_items_only=True)) == []


def test_extra_fields_items_and_any_ref_arg_are_accepted_in_skill_args():
    assert _issues(_skill(extra_fields=["urgency"], items=["a"], evidence_ref="n0",
                          failed_items_ref="n0")) == []


# --- (iii) unknown skill_args keys -----------------------------------------------

def test_round_1_instruction_inside_classify_skill_args_is_a_plan_error():
    issues = _issues(_skill(instruction="Classify by urgency."))
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "BRT-ORC-036" and issue.fields == ("instruction",)
    assert issue.skill_id == "classify_into_buckets"
    msg = issue.message
    assert len(msg) <= 200 and "instruction" in msg
    for name in ("buckets", "items_ref", "concurrency_limit", "extra_fields", "failed_items_only"):
        assert name in msg, (name, msg)


def test_a_declared_arg_of_another_skill_is_accepted_there():
    node = {"id": "n1", "type": "apply_to_each", "skill_id": "defer_and_collect", "skill_version": 1,
            "skill_args": {"items_ref": "n0", "instruction": "x", "confidence_threshold": 0.7,
                           "output_schema": {"label": "string"}}}
    assert _issues(node) == []


def test_an_unknown_skill_is_left_to_the_builder():
    node = _skill(anything="x")
    node["skill_id"] = "no_such_skill"
    assert _issues(node) == []


# --- (iv) unknown type -----------------------------------------------------------

def test_an_unknown_type_is_a_plan_error_not_a_silent_answerer():
    issues = _issues({"type": "widget", "id": "n1", "instruction": "x", "colour": "blue"})
    assert len(issues) == 1
    issue = issues[0]
    assert issue.code == "BRT-ORC-036" and issue.fields == ("type",)
    msg = issue.message
    assert len(msg) <= 200 and "widget" in msg
    for t in ("answerer", "apply_to_each", "state_op", "orchestrate"):
        assert t in msg


# --- message limits --------------------------------------------------------------

def test_long_names_are_cut_at_40_and_the_message_at_200():
    long = "k" * 90
    node = dict(BASE["answerer"], **{long: 1}, **{f"bad_key_{i}_" + "z" * 30: 1 for i in range(8)})
    msg = _issues(node)[0].message
    assert len(msg) <= 200, len(msg)
    assert long not in msg and "k" * 37 in msg
    sk = _skill(**{long: 1})
    msg = _issues(sk)[0].message
    assert len(msg) <= 200 and long not in msg


# --- the planner prompt's own examples are accepted -------------------------------
# Each node is copied by hand from src/prompts/orchestrator.py (protected; read only).
# Placeholders ({{ ... }}) are filled with a small value of the shape shown.

PROMPT_EXAMPLES = [
    # "### 1. `answerer`" example (orchestrator.py ~186-199)
    {"type": "answerer", "id": "n1", "instruction": "Clear, atomic, unambiguous.",
     "context": "Only what this node NEEDS.", "depends_on": ["n0"],
     "output_schema": {"answer": "string"}, "side_effects": ["scratch"],
     "state_query": "optional SQL run before dispatch", "task_type": "reasoning",
     "tier": "light", "recommended_model": None, "trigger_reason": "initial_plan"},
    # "### 2. `apply_to_each`" example (~208-220)
    {"type": "apply_to_each", "id": "n1", "instruction": "Do this to ONE item: ...",
     "inputs_ref": "n0", "items": ["a"], "output_schema": {"label": "string"},
     "concurrency_limit": 20, "side_effects": ["deferrals"], "task_type": "extraction",
     "tier": "light", "recommended_model": None},
    # "### 3. `state_op`" example (~233-240)
    {"type": "state_op", "id": "n1", "op": "count", "source": "n0", "params": {},
     "depends_on": ["n0"]},
    # "### 4. `orchestrate`" example (~263-269)
    {"type": "orchestrate", "id": "n1", "instruction": "A bounded sub-problem...",
     "context": "Distilled context.", "output_schema": {"summary": "string"}},
    # "When a skill matches" example (~282-288)
    {"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets",
     "skill_version": 1, "skill_args": {"buckets": ["a", "b"], "items_ref": "n0",
                                        "concurrency_limit": 20}},
    # web_search example (~300-308)
    {"type": "answerer", "id": "n1",
     "instruction": "Summarise the current state of X using the search results.",
     "output_schema": {"summary": "string"},
     "tool_requests": [{"tool": "web_search", "args": {"query": "X - current status 2026"}}]},
    # "Pattern example" plan (~333-340)
    {"type": "answerer", "id": "n2",
     "tool_requests": [{"tool": "web_search", "args": {"query": "<narrow angle>"}}],
     "instruction": "Summarise what you found.",
     "output_schema": {"summary": "string", "key_urls": "list"}},
]


@pytest.mark.parametrize("node", PROMPT_EXAMPLES, ids=lambda n: f"{n['type']}-{n.get('skill_id', 'own')}")
def test_every_field_in_the_planner_prompts_examples_is_accepted(node):
    assert _issues(node) == []


# --- Change 1: accepted but unused (instrument only) ------------------------------

def test_accepted_unused_names_a_skill_nodes_node_level_fields_and_state_query():
    sk = dict(_skill(), instruction="x", output_schema={"bucket": "string"},
              concurrency_limit=5, context="c", side_effects=["deferrals"])
    got = dict(accepted_unused([sk, dict(BASE["answerer"], id="n2", state_query="SELECT 1"),
                                BASE["apply_to_each"]]))
    assert got == {
        "n1": ("concurrency_limit", "context", "instruction", "output_schema", "side_effects"),
        "n2": ("state_query",),
    }


# --- end to end --------------------------------------------------------------------

@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "field_check.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-field-check")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    built: list = []
    original = engine._build_task_from_node

    def _spy(node, *a, **kw):
        built.append(dict(node))
        return original(node, *a, **kw)

    monkeypatch.setattr(engine, "_build_task_from_node", _spy)
    return db_path, monkeypatch, built


def _failure(stub, node_id="n1"):
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    body = replans[0].system + "\n" + replans[0].user
    at = body.index(f'"{node_id}": {{')
    code = re.search(r'"failure": \{[^{}]*?"code": "(BRT-[A-Z]+-\d+)"', body[at:])
    assert code, body[-600:]
    msg = re.search(r'"message": ("(?:[^"\\]|\\.)*")', body[at + code.start():])
    assert msg, body[-600:]
    return code.group(1), json.loads(msg.group(1))


def _events(db_path, event_type):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT decision_json FROM logs WHERE event_type=?", (event_type,)).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


def _answer(req):
    return {"bucket": "billing", "confidence": 0.9, "defer_reason": ""}


def _again(node):
    """The re_plan answer: the same plan once, then compose (ordinary re_plans go on)."""
    calls: list = []

    def reply(req):
        calls.append(1)
        if len(calls) > 1:
            return {"action": "compose", "rationale": "done"}
        return {"action": "continue", "rationale": "same again", "next_nodes": [node]}
    return reply


@pytest.mark.asyncio
async def test_first_hit_goes_to_re_plan_with_the_message_and_no_call(env):
    db_path, mp, _ = env
    stub = StubModels(plan_nodes=[dict(BASE["answerer"], colour="blue")]).install(mp)
    await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    code, message = _failure(stub)
    assert code == "BRT-ORC-036" and "colour" in message and len(message) <= 200
    assert not stub.of_kind("answerer")


@pytest.mark.asyncio
async def test_first_hit_on_a_skill_arg_goes_to_re_plan(env):
    db_path, mp, _ = env
    stub = StubModels(plan_nodes=[_skill(instruction="by urgency")]).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    code, message = _failure(stub)
    assert code == "BRT-ORC-036" and "classify_into_buckets" in message
    assert not stub.of_kind("answerer")


@pytest.mark.asyncio
async def test_second_hit_runs_without_the_keys_and_records_them_three_ways(env):
    db_path, mp, built = env
    bad = dict(_skill(instruction="by urgency"), id="n2", colour="blue")
    stub = StubModels(plan_nodes=[_skill(instruction="by urgency")], replan_for=_again(bad),
                      answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    # one plan-validation re_plan, then the ordinary one after the step ran
    assert len(stub.of_kind("re_plan")) == 2
    assert stub.of_kind("answerer"), "the second-hit step did not run"
    ran = [n for n in built if n.get("id") == "n2"]
    assert ran and "colour" not in ran[0] and "instruction" not in ran[0]["skill_args"]
    assert ran[0]["skill_args"]["buckets"] == ["billing", "bug"]
    notes = [d for d in _events(db_path, "orchestrator_decision")
             if d.get("code") == "BRT-ORC-036" and d.get("action") == "ran_without_fields"]
    assert sorted((n["skill_id"], tuple(n["fields"])) for n in notes) == [
        ("", ("colour",)), ("classify_into_buckets", ("instruction",))]
    assert all(n["node_id"] == "n2" for n in notes)
    assert sorted(engine.asked_but_not_produced(result["run_id"]), key=lambda e: e["field"]) == [
        {"step": "n2", "skill": "", "field": "colour"},
        {"step": "n2", "skill": "classify_into_buckets", "field": "instruction"}]


@pytest.mark.asyncio
async def test_035_and_036_share_one_re_plan(env):
    db_path, mp, _ = env
    first = dict(_skill(), output_schema={"urgency": "string"})
    stub = StubModels(plan_nodes=[first], replan_for=_again(dict(_skill(instruction="x"), id="n2")),
                      answer_for=_answer).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 2   # the shared refusal, then the ordinary one
    assert stub.of_kind("answerer"), "the 036 fault after a 035 re_plan is a second hit"


@pytest.mark.asyncio
async def test_second_hit_with_an_unknown_type_fails_the_step(env):
    db_path, mp, built = env
    widget = {"type": "widget", "id": "n1", "instruction": "x"}
    stub = StubModels(plan_nodes=[widget], replan_for=_again(dict(widget, id="n2"))).install(mp)
    await run_pipeline("Do x.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 1
    assert _failure(stub)[0] == "BRT-ORC-036"
    assert not stub.of_kind("answerer") and not built
    errs = [d for d in _events(db_path, "error") if d.get("code") == "BRT-ORC-036"]
    assert {d["node_id"] for d in errs} == {"n1", "n2"}


@pytest.mark.asyncio
async def test_accepted_unused_is_noted_and_changes_nothing(env):
    db_path, mp, built = env
    sk = dict(_skill(), instruction="ignored", output_schema={"bucket": "string"},
              concurrency_limit=3)
    ans = dict(BASE["answerer"], id="n2", state_query="SELECT 1")
    stub = StubModels(plan_nodes=[sk, ans], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    notes = {d["node_id"]: d["fields"] for d in _events(db_path, "orchestrator_decision")
             if d.get("action") == "accepted_unused"}
    assert notes == {"n1": ["concurrency_limit", "instruction", "output_schema"],
                     "n2": ["state_query"]}
    # no receipt line, nothing refused, and the skill keeps its own concurrency
    assert not [d for d in _events(db_path, "error") if d.get("code") == "BRT-ORC-036"]
    assert engine.asked_but_not_produced(result["run_id"]) == []
    task = engine._build_task_from_node(
        sk, result["run_id"], 1, 5, "1a", "http://x", "m", "Sort these.")
    assert task.dependency_input["concurrency_limit"] != 3


async def test_lane_raw_local_036_reaches_re_plan_without_document_text(lane):
    from tests.e2e.stub_model_server import lane_matrix
    plan = [{"type": "apply_to_each", "id": "n1", "inputs_ref": "documents",
             "instruction": "Extract the client.", "task_type": "extraction", "tier": "light",
             "output_schema": {"client": "string"}, "colour": "blue"}]
    stub, _ = await _lane_run(lane, "raw_local", lane_matrix(), plan=plan)
    replan = stub.of_kind("re_plan")[0]
    body = replan.system + "\n" + replan.user
    assert "BRT-ORC-036" in body and "colour" in body
    text = json.dumps(replan.body)
    for canary in CANARIES:
        assert canary not in text
    assert "relationship desk" not in text
    assert stub.of_kind("answerer") == []


# --- redaction of the 036 note ------------------------------------------------------

def test_the_036_note_keeps_its_five_keys_under_redaction():
    d = {"code": "BRT-ORC-036", "action": "ran_without_fields", "node_id": "n" * 60,
         "skill_id": "", "fields": ["colour"] + [f"f{i}" for i in range(20)],
         "msg": "BRT-ORC-036: ...", "extra": "doc text"}
    ev = {"event_type": "orchestrator_decision", "decision_json": json.dumps(d)}
    kept = json.loads(_redact_events([ev])[0]["decision_json"])
    assert set(kept) == {"code", "action", "node_id", "skill_id", "fields"}
    assert kept["code"] == "BRT-ORC-036" and kept["node_id"] == "n" * 40
    assert len(kept["fields"]) == 10 and kept["fields"][0] == "colour"
