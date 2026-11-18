"""Planner ruling 32c: a skill step whose output_schema names a field the
skill does not return is a plan error (BRT-ORC-035), sent to re_plan.

Bench round 2 job A wrote ``classify_into_buckets`` with an output_schema of
{file, category, urgency}; the engine ignored it and no urgency ever existed.
The engine does not rewrite the step: the planner chooses a different one.
"""
import ast
import json
import re
import sqlite3
from pathlib import Path

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.orchestrator.plan_validation import validate_plan
from src.pipeline.runner import run_pipeline
from src.skills.library import SkillLibrary
from tests.e2e.test_privacy_lane import (  # noqa: F401  (lane is a fixture)
    CANARIES, PLAN_RAW, _run as _lane_run, lane,
)
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

HINT = "for other fields use apply_to_each with your own output_schema"
HINT2 = "to rename fields, do it when composing"
ENDING = f"; {HINT}; {HINT2}"
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


def _msg(node):
    issues = _issues(node)
    return issues[0].message if issues else None


def test_job_a_shape_is_a_plan_error_with_the_three_part_message():
    msg = _msg(_node(JOB_A))
    assert msg is not None and msg.startswith("BRT-ORC-035")
    assert len(msg) <= 200
    assert msg.endswith(ENDING), msg
    head = msg[: -len(ENDING)]
    for word in ("bucket", "confidence", "defer_reason", "item", "error"):
        assert word in head, (word, msg)
    assert _issues(_node(JOB_A))[0].fields == ("file", "category", "urgency")


def test_message_names_extra_fields_and_stays_within_200():
    msg = _msg(_node(JOB_A, extra_fields=["topic", "reason"]))
    assert len(msg) <= 200 and msg.endswith(ENDING)
    assert "bucket" in msg and "item, error" in msg
    # extra fields are returned fields: they are not reported as missing
    assert _issues(_node(JOB_A, extra_fields=["urgency"]))[0].fields == ("file", "category")


def test_long_names_trim_the_lists_never_the_hints_and_item_error_stay():
    schema = {f"field_number_{i}_with_a_very_long_name_to_overflow_the_cut": "s" for i in range(12)}
    msg = _msg(_node(schema, extra_fields=[f"extra_{i}_and_more_text" for i in range(12)]))
    assert len(msg) <= 200 and msg.endswith(ENDING)
    assert "item, error" in msg
    assert "field_number_0_with_a_very_long_name_to_overflow_the_cut" not in msg  # cut at 40 chars


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


def test_extra_fields_cover_the_named_field():
    assert _issues(_node({"bucket": "string", "urgency": "string"}, extra_fields=["urgency"])) == []


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


@pytest.mark.asyncio
async def test_first_hit_fails_the_step_before_any_call_and_re_plan_gets_the_message(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node(JOB_A)]).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    code, message = _failure(stub)
    assert code == "BRT-ORC-035"
    assert message.endswith(ENDING) and len(message) <= 200
    assert not stub.of_kind("answerer"), "the step made a model call"


@pytest.mark.asyncio
async def test_second_hit_runs_the_step_and_records_it_three_ways(env):
    db_path, mp = env
    again = {"action": "continue", "rationale": "same again", "next_nodes": [_node(JOB_A, id="n2")]}
    stub = StubModels(plan_nodes=[_node(JOB_A)], replan_for=lambda req: again,
                      answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 1
    assert stub.of_kind("answerer"), "the second-hit step did not run"
    # 1. the plan note
    notes = [d for d in _events(db_path, "orchestrator_decision") if d.get("code") == "BRT-ORC-035"]
    assert len(notes) == 1
    note = notes[0]
    assert note["node_id"] == "n2" and note["skill_id"] == "classify_into_buckets"
    assert note["fields"] == ["file", "category", "urgency"] and note["action"] == "ran_without_fields"
    # 3. the run-level list
    assert engine.asked_but_not_produced(result["run_id"]) == [
        {"step": "n2", "skill": "classify_into_buckets", "field": f} for f in ("file", "category", "urgency")]


@pytest.mark.asyncio
async def test_two_bad_steps_in_the_first_plan_still_get_one_re_plan(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node(JOB_A), _node(JOB_A, id="n2")]).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 1
    assert _failure(stub, "n1")[0] == "BRT-ORC-035" and _failure(stub, "n2")[0] == "BRT-ORC-035"
    assert not stub.of_kind("answerer")


@pytest.mark.asyncio
async def test_a_planner_that_fixes_the_plan_runs_normally(env):
    db_path, mp = env
    calls: list = []
    fixed = {"action": "continue", "rationale": "fixed",
             "next_nodes": [_node({"bucket": "string"}, id="n2")]}
    stub = StubModels(
        plan_nodes=[_node(JOB_A)],
        replan_for=lambda req: fixed if not calls.append(1) and len(calls) == 1 else
        {"action": "compose", "rationale": "done"},
        answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert stub.of_kind("answerer"), "the fixed step did not run"
    assert len(stub.of_kind("re_plan")) == 2
    assert engine.asked_but_not_produced(result["run_id"]) == []


@pytest.mark.asyncio
async def test_a_valid_skill_step_still_runs(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node({"bucket": "string"})], answer_for=_answer).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert stub.of_kind("answerer")


# --- lane: plan validation reaches re_plan with its message, a running step does not ---

def _raw_skill_plan():
    return [{"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets",
             "skill_version": 1, "task_type": "extraction", "tier": "light",
             "skill_args": {"buckets": ["high", "low"], "items_ref": "documents"},
             "output_schema": JOB_A}]


@pytest.mark.parametrize("mode", ["raw_local", "all_local"])
async def test_lane_plan_refusal_gives_re_plan_the_message_and_no_document_text(lane, mode):
    from tests.e2e.stub_model_server import lane_matrix
    stub, result = await _lane_run(lane, mode, lane_matrix(), plan=_raw_skill_plan())
    replan = stub.of_kind("re_plan")[0]
    body = replan.system + "\n" + replan.user
    assert "BRT-ORC-035" in body and ENDING in body
    assert "item, error" in body
    text = json.dumps(replan.body)
    for canary in CANARIES:
        assert canary not in text
    assert "relationship desk" not in text and "warehouse contract" not in text
    assert stub.of_kind("answerer") == []


async def test_lane_a_step_that_fails_while_running_still_gives_code_and_ids_only(lane):
    from tests.e2e.stub_model_server import three_tier_matrix as no_local
    stub, result = await _lane_run(lane, "raw_local", no_local())
    body = stub.of_kind("re_plan")[0].system
    assert "BRT-LANE-002" in body and "BRT-ORC-035" not in body
    for canary in CANARIES:
        assert canary not in body
