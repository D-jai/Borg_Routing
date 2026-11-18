"""Planner ruling 32d(ii)-(v), readings under ruling 32 (3) and ruling 35b:
BRT-ORC-036. A plan node key or a skill_args key that nothing accepts is left
out on the FIRST hit: the step runs without it, recorded three ways, with no
re_plan. A node type that is not one of the four stays a plan error: one
re_plan per run (its message names the value first and never cuts it, and the
re_plan is shown the node it wrote), then the step fails. Change 1: an accepted
field that builds nothing on the node's path is recorded (``accepted_unused``),
not refused.
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
    assert issue.code == "BRT-ORC-036" and issue.fields == ("type",) and issue.where == "type"
    msg = issue.message
    assert msg == ("BRT-ORC-036: step type 'widget' is not a node type. "
                   "Types: answerer, apply_to_each, state_op, orchestrate")
    # Ruling 35b: the node it wrote travels with the issue, for the re_plan.
    assert json.loads(issue.node) == {"type": "widget", "id": "n1", "instruction": "x",
                                      "colour": "blue"}


def test_an_unknown_type_value_comes_first_and_is_never_cut():
    value = "a_made_up_type_" + "v" * 300
    issue = _issues({"type": value, "id": "n1"})[0]
    assert issue.message.startswith(f"BRT-ORC-036: step type '{value}' is not a node type.")
    # the list of valid types gives way instead
    assert issue.message.endswith("Types: ...")


def test_a_long_types_list_is_cut_by_whole_names():
    value = "w" * 120
    msg = _issues({"type": value, "id": "n1"})[0].message
    assert len(msg) <= 200 and f"'{value}'" in msg
    assert msg.endswith("Types: answerer, ...") or msg.endswith("Types: answerer, apply_to_each, ...")


def test_the_node_shown_to_re_plan_is_cut_sensibly():
    node = {"type": "widget", "id": "n1", "instruction": "i" * 5000}
    shown = _issues(node)[0].node
    assert len(shown) <= 600 and shown.startswith('{"type": "widget", "id": "n1"')
    assert shown.endswith("...")


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


def _no_036_refusal(db_path, stub):
    assert not [d for d in _events(db_path, "error") if d.get("code") == "BRT-ORC-036"]
    for r in stub.of_kind("re_plan"):
        assert "BRT-ORC-036" not in r.system + r.user


@pytest.mark.asyncio
async def test_first_hit_on_a_node_key_runs_without_it_and_sends_no_re_plan(env):
    db_path, mp, built = env
    stub = StubModels(plan_nodes=[dict(BASE["answerer"], colour="blue")],
                      answer_for=lambda r: {"result": "hi"}).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert stub.of_kind("answerer"), "the step did not run on its first hit"
    assert len(stub.of_kind("re_plan")) <= 1           # at most the ordinary one
    _no_036_refusal(db_path, stub)
    assert [n for n in built if n.get("id") == "n1"][0].get("colour") is None
    assert engine.asked_but_not_produced(result["run_id"]) == [
        {"step": "n1", "skill": "", "field": "colour"}]


@pytest.mark.asyncio
async def test_first_hit_on_a_skill_arg_runs_without_it(env):
    db_path, mp, built = env
    stub = StubModels(plan_nodes=[_skill(instruction="by urgency")], answer_for=_answer).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("answerer")) == 2
    _no_036_refusal(db_path, stub)
    ran = [n for n in built if n.get("id") == "n1"]
    assert ran and "instruction" not in ran[0]["skill_args"]


@pytest.mark.asyncio
async def test_first_hit_runs_without_the_keys_and_records_them_three_ways(env):
    db_path, mp, built = env
    bad = dict(_skill(instruction="by urgency"), id="n2", colour="blue")
    stub = StubModels(plan_nodes=[bad], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    # no plan-validation re_plan: only the ordinary one after the step ran
    assert len(stub.of_kind("re_plan")) == 1
    assert stub.of_kind("answerer"), "the step did not run"
    _no_036_refusal(db_path, stub)
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
async def test_035_and_036_keys_never_use_up_the_type_re_plan(env):
    """Ruling 35b: the once-per-run state concerns unknown types only. A 035
    field and a 036 key run first; a later unknown type still gets its re_plan."""
    db_path, mp, _ = env
    first = dict(_skill(instruction="x"), output_schema={"urgency": "string"})
    widget = {"type": "widget", "id": "n2", "instruction": "x"}
    stub = StubModels(plan_nodes=[first], replan_for=_seq(_cont(widget)),
                      answer_for=_answer).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("answerer")) == 2
    # re_plan 1: ordinary, after n1 ran. re_plan 2: the refusal of n2's type.
    assert len(stub.of_kind("re_plan")) == 2
    assert _failure_at(stub, 1, "n2")[0] == "BRT-ORC-036"


@pytest.mark.asyncio
async def test_first_hit_with_an_unknown_type_re_plans_with_the_node_it_wrote(env):
    db_path, mp, built = env
    value = "widget_" + "q" * 250           # longer than the 200-character cap
    widget = {"type": value, "id": "n1", "instruction": "Count the widgets."}
    stub = StubModels(plan_nodes=[widget]).install(mp)
    await run_pipeline("Do x.", db_path=db_path)
    await drain_log_writes()
    assert not built and not stub.of_kind("answerer")
    code, message = _failure(stub)
    assert code == "BRT-ORC-036"
    assert message.startswith(f"BRT-ORC-036: step type '{value}' is not a node type.")
    node = _failure_field(stub, "node")
    assert '"instruction": "Count the widgets."' in node and value[:100] in node


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


@pytest.mark.parametrize("mode", ["raw_local", "all_local"])
async def test_lane_unknown_type_reaches_re_plan_with_message_and_node_and_no_document_text(lane, mode):
    """The lane class rule (readings under ruling 32 (2)) on the 035b message:
    built from the plan and the registry only, it reaches re_plan in every
    lane, with the node it wrote; no document text; BRT-LANE-006 still runs."""
    from tests.e2e.stub_model_server import lane_matrix
    plan = [{"type": "extractor", "id": "n1", "inputs_ref": "documents",
             "instruction": "Extract the client.", "task_type": "extraction", "tier": "light",
             "output_schema": {"client": "string"}}]
    stub, _ = await _lane_run(lane, mode, lane_matrix(), plan=plan)
    replan = stub.of_kind("re_plan")[0]
    body = replan.system + "\n" + replan.user
    assert "BRT-ORC-036: step type 'extractor' is not a node type." in body
    assert _failure_field(stub, "node").startswith('{"type": "extractor", "id": "n1"')
    text = json.dumps(replan.body)
    for canary in CANARIES:
        assert canary not in text
    assert "relationship desk" not in text
    assert stub.of_kind("answerer") == []


async def test_lane_raw_local_unknown_key_runs_without_it(lane):
    from tests.e2e.stub_model_server import lane_matrix
    plan = [{"type": "apply_to_each", "id": "n1", "inputs_ref": "documents",
             "instruction": "Extract the client.", "task_type": "extraction", "tier": "light",
             "output_schema": {"client": "string", "risk": "string"}, "colour": "blue"}]
    stub, result = await _lane_run(lane, "raw_local", lane_matrix(), plan=plan)
    assert [r for r in stub.of_kind("answerer") if r.is_local], "the step did not run"
    for r in stub.of_kind("re_plan"):
        assert "BRT-ORC-036" not in r.system + r.user
    assert engine.asked_but_not_produced(result["run_id"]) == [
        {"step": "n1", "skill": "", "field": "colour"}]


async def test_lane_a_step_that_fails_while_running_still_gives_code_and_ids_only(lane):
    from tests.e2e.stub_model_server import three_tier_matrix as no_local
    stub, result = await _lane_run(lane, "raw_local", no_local())
    body = stub.of_kind("re_plan")[0].system
    assert "BRT-LANE-002" in body and '"node": "{' not in body
    for canary in CANARIES:
        assert canary not in body


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


def test_failed_items_only_is_accepted_on_a_skill_node_of_any_type_and_only_there():
    node = {"id": "n3", "skill_id": "classify_into_buckets", "failed_items_only": True,
            "skill_args": {"buckets": ["a", "b"], "items_ref": "n1"}}
    assert validate_plan([node], LIB) == []
    assert validate_plan([dict(node, type="answerer")], LIB) == []
    plain = {"id": "n4", "type": "answerer", "instruction": "x", "failed_items_only": True}
    assert validate_plan([plain], LIB)[0].fields == ("failed_items_only",)


def test_accepted_unused_gaps_items_skill_args_and_state_op_routing_keys():
    ans = {"type": "answerer", "id": "n1", "instruction": "Rank these",
           "items": ["a", "b", "c"], "output_schema": {"rank": "list"}}
    orch = dict(ans, type="orchestrate", id="n2")
    no_skill = {"type": "answerer", "id": "n3", "instruction": "x",
                "skill_args": {"a": 1}, "skill_version": 1}
    sop = {"type": "state_op", "id": "n4", "op": "count", "source": "n1", "tier": "light",
           "task_type": "extraction", "recommended_model": "m", "instruction": "x"}
    skill_answerer = dict(_skill(), id="n5", items=["a"])
    assert dict(accepted_unused([ans, orch, no_skill, sop, skill_answerer])) == {
        "n1": ("items",), "n2": ("items",), "n3": ("skill_args", "skill_version"),
        "n4": ("instruction", "recommended_model", "task_type", "tier"),
    }


@pytest.mark.asyncio
async def test_items_on_a_plain_answerer_is_noted_and_changes_nothing(env):
    db_path, mp, built = env
    ans = {"type": "answerer", "id": "n1", "instruction": "Rank these",
           "items": ["a", "b", "c"], "output_schema": {"rank": "list"}}
    stub = StubModels(plan_nodes=[ans], answer_for=lambda r: {"rank": ["a", "b", "c"]}).install(mp)
    result = await run_pipeline("Rank these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    notes = [d for d in _events(db_path, "orchestrator_decision") if d.get("action") == "accepted_unused"]
    assert [(n["node_id"], n["fields"]) for n in notes] == [("n1", ["items"])]
    assert not [e for e in _events(db_path, "error") if e.get("code") == "BRT-ORC-036"]


def _seq(*replies):
    """re_plan answers in order, then compose."""
    left = list(replies)
    return lambda req: left.pop(0) if left else {"action": "compose", "rationale": "done"}


def _cont(*nodes, action="continue"):
    return {"action": action, "rationale": "next", "next_nodes": list(nodes)}


def _failure_at(stub, k, node_id):
    body = _replan_body(stub, k)
    at = body.index(f'"{node_id}": {{')
    code = re.search(r'"failure": \{[^{}]*?"code": "(BRT-[A-Z]+-\d+)"', body[at:])
    assert code, body[-600:]
    return code.group(1), body[at:]


def _failure_field(stub, name, node_id="n1"):
    body = _replan_body(stub, 0)
    at = body.index(f'"{node_id}": {{')
    found = re.search(rf'"{name}": ("(?:[^"\\]|\\.)*")', body[at:])
    assert found, body[-800:]
    return json.loads(found.group(1))


def _replan_body(stub, k):
    r = stub.of_kind("re_plan")[k]
    return r.system + "\n" + r.user


@pytest.mark.asyncio
async def test_an_unknown_type_on_a_later_hit_sends_no_plan_validation_re_plan_of_its_own(env):
    """Ruling: its failure shows in the next ordinary re_plan, bounded by MAX_LOOPS."""
    db_path, mp, built = env
    widget = {"type": "widget", "id": "n1", "instruction": "x"}
    ans = dict(BASE["answerer"], id="n3")
    stub = StubModels(
        plan_nodes=[widget],
        replan_for=_seq(_cont(dict(widget, id="n2"), ans), _cont(dict(widget, id="n4"))),
        answer_for=lambda r: {"result": "hi"}).install(mp)
    await run_pipeline("Do x.", db_path=db_path)
    await drain_log_writes()
    # re_plan 1: the refusal of n1. re_plan 2: ordinary, after n3 ran, showing n2's failure.
    # n4 (a third unknown type) fails with no re_plan of its own.
    assert len(stub.of_kind("re_plan")) == 2
    assert [r for r in stub.of_kind("answerer")], "n3 did not run"
    second = _replan_body(stub, 1)
    at = second.index('"n2": {')
    assert "BRT-ORC-036" in second[at:at + 1500]
    errs = {d["node_id"] for d in _events(db_path, "error") if d.get("code") == "BRT-ORC-036"}
    assert errs == {"n1", "n2", "n4"}


def test_accepted_unused_on_a_state_op_with_a_skill_id_and_on_an_answerer_skill():
    sop = {"type": "state_op", "id": "n1", "op": "count", "params": {}, "skill_id": "classify_into_buckets",
           "instruction": "x", "tier": "light", "skill_args": {"buckets": ["a"], "items_ref": "n0"}}
    # a skill that renders as an answerer never reads node-level failed_items_only
    answerer_skill = {"type": "answerer", "id": "n2", "skill_id": "compare_and_rank",
                      "failed_items_only": True, "skill_args": {}}
    apply_skill = dict(_skill(), id="n3", failed_items_only=True)
    got = dict(accepted_unused([sop, answerer_skill, apply_skill], LIB))
    assert got["n1"] == ("instruction", "op", "params", "tier")
    assert got["n2"] == ("failed_items_only",)
    assert "n3" not in got


@pytest.mark.asyncio
async def test_a_typeless_retry_in_a_re_plan_retries_only_the_failed_items(env):
    db_path, mp, built = env
    items = [f"note {i}" for i in range(6)]
    first = {"type": "apply_to_each", "id": "n1", "instruction": "Tag this note.",
             "items": items, "output_schema": {"tag": "string"}}
    retry = {"id": "n2", "skill_id": "classify_into_buckets", "failed_items_only": True,
             "skill_args": {"buckets": ["a", "b"], "items_ref": "n1"}}

    def answer(req):
        if "Tag this note." in req.system:
            return {} if any(f"note {i}" in req.system + req.user for i in (1, 4)) else {"tag": "t"}
        return {"bucket": "a", "confidence": 0.9, "defer_reason": ""}

    stub = StubModels(plan_nodes=[first], replan_for=_seq(_cont(retry)), answer_for=answer).install(mp)
    await run_pipeline("Tag these.", db_path=db_path)
    await drain_log_writes()
    assert not [d for d in _events(db_path, "error") if d.get("code") == "BRT-ORC-036"]
    reqs = [r for r in stub.of_kind("answerer") if "EXACTLY ONE" in r.system]
    texts = " ".join(r.system + r.user for r in reqs)
    assert len(reqs) == 2 and "note 1" in texts and "note 4" in texts
    assert all(f"note {i}" not in texts for i in (0, 2, 3, 5))


@pytest.mark.asyncio
async def test_a_cascade_re_plan_after_an_unknown_type_runs_its_new_steps(env):
    db_path, mp, built = env
    bad = {"type": "widget", "id": "n1", "instruction": "x"}
    stub = StubModels(
        plan_nodes=[bad],
        replan_for=_seq(_cont(dict(BASE["answerer"], id="n3", instruction="Then this."),
                              action="cascade")),
        answer_for=lambda r: {"result": "hi"}).install(mp)
    await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert any("Then this." in r.system for r in stub.of_kind("answerer")), "the cascade step did not run"
