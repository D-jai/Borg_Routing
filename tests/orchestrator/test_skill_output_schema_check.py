"""BRT-ORC-035 (planner ruling 32c, reversed by ruling 35a): a skill step whose
output_schema names fields the skill does not return RUNS as written, first
time, with no re_plan. Each such name goes on the run's "asked for but not
produced" list, with a plan note (``ran_without_fields``) and a receipt line.

Why reversed: names cannot tell a rename from a new field. In trial 32g the
first plan already asked for urgency through ``extra_fields``; 035 refused it
for ``file`` and ``category`` (renames of ``item`` and ``bucket``), and the
re_plan dropped the correct field (docs/design/bench_round2_measurements.md,
"Trial 32g").
"""
import ast
import json
import sqlite3
from pathlib import Path

import pytest

from src.db.schema import create_all
from src.orchestrator import engine, plan_validation
from src.orchestrator.plan_validation import validate_plan
from src.pipeline.runner import run_pipeline
from src.skills.library import SkillLibrary
from tests.e2e.test_privacy_lane import (  # noqa: F401  (lane is a fixture)
    CANARIES, _run as _lane_run, lane,
)
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

ARGS = {"buckets": ["billing", "bug"], "items_ref": None}
LIB = SkillLibrary()
LIB.load("data/skills.yaml")


def _node(output_schema=None, extra_fields=None, **over):
    args = dict(ARGS)
    if extra_fields is not None:
        args["extra_fields"] = extra_fields
    node = {"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets",
            "skill_version": 1, "items": ["a", "b"], "skill_args": args,
            "tier": "light", "task_type": "extraction"}
    if output_schema is not None:
        node["output_schema"] = output_schema
    node.update(over)
    return node


JOB_A = {"file": "string", "category": "string", "urgency": "string"}


def _issues(node):
    return validate_plan([node], LIB)


# --- which names are listed (unchanged by ruling 35a) ------------------------------

def test_job_a_shape_lists_the_three_names():
    issues = _issues(_node(JOB_A))
    assert len(issues) == 1 and issues[0].code == "BRT-ORC-035"
    assert issues[0].fields == ("file", "category", "urgency")
    assert issues[0].skill_id == "classify_into_buckets" and issues[0].node_id == "n1"


def test_extra_fields_are_produced_and_not_listed():
    assert _issues(_node(JOB_A, extra_fields=["urgency"]))[0].fields == ("file", "category")
    assert _issues(_node({"bucket": "string", "urgency": "string"}, extra_fields=["urgency"])) == []


def test_the_re_plan_message_and_its_builder_are_gone():
    """Ruling 35a: 035 sends nothing to re_plan, so its 200-character builder
    and both hints are removed."""
    for name in ("skill_schema_message", "HINT_RENAME", "HINT_OTHER_FIELDS"):
        assert not hasattr(plan_validation, name), name


def test_no_output_schema_passes_unchanged():
    assert _issues(_node()) == []


def test_output_schema_naming_only_the_skills_fields_passes():
    assert _issues(_node({"bucket": "string", "confidence": "float"})) == []


def test_item_and_error_count_as_returned_fields():
    assert _issues(_node({"item": "string", "error": "string", "bucket": "string"})) == []


def test_json_schema_object_and_array_forms_are_read_by_real_field_names():
    ok = {"type": "object", "properties": {"bucket": {"type": "string"}}}
    bad = {"type": "object", "properties": {"urgency": {"type": "string"}}}
    arr = {"type": "array", "items": {"type": "object", "properties": {
        "file": {"type": "string"}, "bucket": {"type": "string"}}}}
    assert _issues(_node(ok)) == []
    assert _issues(_node(bad))[0].fields == ("urgency",)
    assert _issues(_node(arr))[0].fields == ("file",)


def test_plain_apply_to_each_with_its_own_schema_is_unchanged():
    node = {"type": "apply_to_each", "id": "n1", "instruction": "Rate each.",
            "items": ["a"], "output_schema": JOB_A}
    assert _issues(node) == []


def test_unreadable_skill_is_left_to_the_builder():
    assert _issues(_node(JOB_A, skill_id="no_such_skill")) == []


# One test per skill kind whose fields come from an argument (the exemption).

def test_skill_fields_from_an_arg_extract_fields():
    node = {"id": "n1", "type": "apply_to_each", "skill_id": "extract_fields", "skill_version": 1,
            "skill_args": {"items_ref": None, "fields": [{"name": "full_name", "type": "string"}]},
            "output_schema": {"full_name": "string"}}
    assert _issues(node) == []
    node["output_schema"] = {"full_name": "string", "age": "int"}
    assert _issues(node)[0].fields == ("age",)


def test_skill_fields_from_an_arg_defer_and_collect():
    node = {"id": "n1", "type": "apply_to_each", "skill_id": "defer_and_collect", "skill_version": 1,
            "skill_args": {"items_ref": None, "instruction": "x", "confidence_threshold": 0.7,
                           "output_schema": {"label": "string"}},
            "output_schema": {"label": "string", "confidence": "float"}}
    assert _issues(node) == []
    node["output_schema"] = {"label": "string", "category": "string"}
    assert _issues(node)[0].fields == ("category",)


def test_skill_fields_from_an_arg_decompose_task():
    node = {"id": "n1", "type": "orchestrate", "skill_id": "decompose_task", "skill_version": 1,
            "skill_args": {"sub_task_instruction": "x", "context_for_child": "y",
                           "expected_return_schema": {"summary": "string"}},
            "output_schema": {"summary": "string"}}
    assert _issues(node) == []
    node["output_schema"] = {"summary": "string", "owner": "string"}
    assert _issues(node)[0].fields == ("owner",)


def test_skill_fields_from_an_arg_retry_escalate():
    node = {"id": "n1", "type": "apply_to_each", "skill_id": "retry_escalate", "skill_version": 1,
            "skill_args": {"failed_items_ref": None, "original_instruction": "x",
                           "original_output_schema": {"label": "string"}},
            "output_schema": {"label": "string"}}
    assert _issues(node) == []
    node["output_schema"] = {"label": "string", "extra": "string"}
    assert _issues(node)[0].fields == ("extra",)


def test_an_unreadable_schema_arg_leaves_the_step_unchecked():
    node = {"id": "n1", "type": "orchestrate", "skill_id": "decompose_task", "skill_version": 1,
            "skill_args": {"sub_task_instruction": "x", "expected_return_schema": "free text"},
            "output_schema": {"anything": "string"}}
    assert _issues(node) == []


def test_validate_plan_signature_and_imports_hold_the_lane_rule():
    path = Path("src/orchestrator/plan_validation.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    mods = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom):
            mods.append(n.module or "")
    assert all(not m.startswith("src") for m in mods), mods
    import inspect
    assert list(inspect.signature(validate_plan).parameters) == ["nodes", "skills"]


# --- end to end ------------------------------------------------------------------

@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skill_schema.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-skill-schema")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _events(db_path, event_type):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT decision_json FROM logs WHERE event_type=?", (event_type,)).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


def _answer(req):
    return {"bucket": "billing", "confidence": 0.9, "defer_reason": ""}


def _replies(*batches):
    """re_plan answers in order: each batch of nodes, then compose."""
    left = list(batches)

    def reply(req):
        if left:
            return {"action": "continue", "rationale": "next", "next_nodes": [left.pop(0)]}
        return {"action": "compose", "rationale": "done"}
    return reply


def _no_035_refusal(db_path, stub):
    assert not [d for d in _events(db_path, "error") if d.get("code") == "BRT-ORC-035"]
    for r in stub.of_kind("re_plan"):
        assert "BRT-ORC-035" not in r.system + r.user


@pytest.mark.asyncio
async def test_first_hit_runs_the_step_and_records_it_three_ways(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node(JOB_A)], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("answerer")) == 2, "the step did not run on its first hit"
    # Only the ordinary re_plan after the wave; no plan-validation re_plan.
    assert len(stub.of_kind("re_plan")) == 1
    _no_035_refusal(db_path, stub)
    # 1. the plan note
    notes = [d for d in _events(db_path, "orchestrator_decision") if d.get("code") == "BRT-ORC-035"]
    assert len(notes) == 1
    note = notes[0]
    assert note["node_id"] == "n1" and note["skill_id"] == "classify_into_buckets"
    assert note["fields"] == ["file", "category", "urgency"] and note["action"] == "ran_without_fields"
    # 3. the run-level list (2, the receipt line, is read from the note: test_workspace_js)
    assert engine.asked_but_not_produced(result["run_id"]) == [
        {"step": "n1", "skill": "classify_into_buckets", "field": f} for f in ("file", "category", "urgency")]


@pytest.mark.asyncio
async def test_two_listed_steps_in_the_first_plan_both_run(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node(JOB_A), _node(JOB_A, id="n2")], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("answerer")) == 4
    _no_035_refusal(db_path, stub)
    assert {e["step"] for e in engine.asked_but_not_produced(result["run_id"])} == {"n1", "n2"}


@pytest.mark.asyncio
async def test_a_valid_skill_step_still_runs_and_lists_nothing(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node({"bucket": "string"})], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert stub.of_kind("answerer")
    assert engine.asked_but_not_produced(result["run_id"]) == []


@pytest.mark.asyncio
async def test_a_listed_step_in_a_later_batch_runs_too_and_ordinary_re_plans_go_on(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node(JOB_A)],
                      replan_for=_replies(_node(JOB_A, id="n2"), _node(JOB_A, id="n3")),
                      answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 3          # one after each wave
    assert len(stub.of_kind("answerer")) == 6         # n1, n2 and n3, 2 items each
    assert {e["step"] for e in engine.asked_but_not_produced(result["run_id"])} == {"n1", "n2", "n3"}
    ran = {d.get("node_id") for d in _events(db_path, "orchestrator_decision")
           if d.get("code") == "BRT-ORC-035" and d.get("action") == "ran_without_fields"}
    assert ran == {"n1", "n2", "n3"}
    _no_035_refusal(db_path, stub)


@pytest.mark.asyncio
async def test_a_036_key_on_the_first_hit_runs_without_the_key(env):
    db_path, mp = env
    bad = {"colour": "blue"}
    stub = StubModels(plan_nodes=[_node(**bad)], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 1
    assert len(stub.of_kind("answerer")) == 2
    assert engine.asked_but_not_produced(result["run_id"]) == [
        {"step": "n1", "skill": "", "field": "colour"}]


# --- the trial's round-2-data plan (ruling 35a) -------------------------------------

TRIAL_R2_PLAN = [{
    # Recorded: bench/results/trial32g_r2data/dbs/A_borg_cloud_1.db, the first plan.
    "type": "apply_to_each", "id": "classify_emails", "skill_id": "classify_into_buckets",
    "skill_version": 1,
    "skill_args": {"buckets": ["billing", "technical", "account_access", "shipping", "feedback"],
                   "items_ref": "documents",
                   "extra_fields": {"urgency": "integer from 1 (lowest) to 5 (highest)"}},
    "output_schema": {"file": "string", "category": "string", "urgency": "integer"},
    "concurrency_limit": 30, "tier": "light",
    "recommended_model": "openrouter/google/gemini-3.1-flash-lite-preview",
}]

# Ruling 35d: the asked-for section of compose's block for this plan, pinned.
TRIAL_A_ASKED = "\n".join([
    "Asked for but not produced (the plan asked a step for these fields and the step does not "
    "return them under these names):",
    "- classify_emails (skill classify_into_buckets): file, category. The step's own fields: "
    "item (file name), bucket, confidence, defer_reason, urgency",
    "For each of these names, choose from the step's own fields: where one of "
    "them holds what the name asks for, place it under that name with a rows marker, for "
    "example @@ROWS:classify_emails | file=<field>, ...@@. Where none does, leave the name out "
    "and tell the reader it was not produced. Do not supply values for these fields yourself.",
])


def _trial_answer(req):
    return {"bucket": "billing", "confidence": 0.9, "defer_reason": "", "urgency": 4}


async def test_the_trial_r2_plan_runs_first_time_and_produces_urgency(lane):
    stub, result = await _lane_run(lane, "by_reference", three_tier_matrix(), plan=TRIAL_R2_PLAN,
                                   instruction="Classify each email and give its urgency.",
                                   answer_for=_trial_answer)
    db_path = lane["db"]
    assert result["status"] == "completed", result.get("error")
    # Runs first time: no plan-validation re_plan, no refusal.
    _no_035_refusal(db_path, stub)
    items = [r for r in stub.of_kind("answerer") if "EXACTLY ONE" in r.system or "Also return" in r.system]
    assert items, "the step made no item call"
    for req in items:
        # The worker is asked for urgency (extra_fields), never for file or category.
        assert "Also return these fields: urgency (integer from 1 (lowest) to 5 (highest))." in req.system
        assert '"urgency": "integer from 1 (lowest) to 5 (highest)"' in req.system
        assert '"file"' not in req.system and '"category"' not in req.system
    # urgency is in the results.
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT inbound_payload FROM envelopes WHERE run_id=? AND type=?",
                            (str(result["run_id"]), "apply_to_each")).fetchall()
    outs = [json.loads(r[0])["output"] for r in rows if r[0]]
    # One result per document (acme_note.txt, birch_note.md; the PDF is not
    # read): every item resolved, none failed, each with its urgency.
    assert len(outs) == 1
    agg = outs[0]
    assert agg["failure_count"] == 0 and agg.get("failures") == []
    assert len(agg["results"]) == agg["total"] == agg["success_count"] == 2
    assert sorted(x["item"] for x in agg["results"]) == ["doc_001", "doc_002"]
    assert all(x["output"].get("urgency") == 4 for x in agg["results"])
    # file and category are on the list; urgency is not.
    assert engine.asked_but_not_produced(result["run_id"]) == [
        {"step": "classify_emails", "skill": "classify_into_buckets", "field": f}
        for f in ("file", "category")]
    # Ruling 35d: compose is told the step's own fields beside the asked names.
    compose = stub.of_kind("compose")[0].user
    assert TRIAL_A_ASKED in compose, compose
