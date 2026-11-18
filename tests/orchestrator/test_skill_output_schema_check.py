"""Planner ruling 32c: a skill step whose output_schema names a field the
skill does not return is a plan error (BRT-ORC-035), sent to re_plan.

Bench round 2 job A wrote ``classify_into_buckets`` with an output_schema of
{file, category, urgency}; the engine ignored it and no urgency ever existed.
The engine does not rewrite the step: the planner chooses a different one.
"""
import json
import re

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

HINT = "for other fields use apply_to_each with your own output_schema"
ARGS = {"buckets": ["billing", "bug"], "items_ref": None}


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


def test_job_a_shape_is_a_plan_error_naming_the_skills_fields():
    msg = engine.skill_schema_message(_node(JOB_A))
    assert msg is not None
    assert msg.startswith("BRT-ORC-035")
    assert len(msg) <= 200
    for word in ("bucket", "confidence", "defer_reason", HINT):
        assert word in msg, (word, msg)


def test_message_names_extra_fields_and_stays_within_200():
    msg = engine.skill_schema_message(_node(JOB_A, extra_fields=["topic", "reason"]))
    assert len(msg) <= 200 and HINT in msg
    assert "topic" in msg and "reason" in msg


def test_long_names_still_fit_200_with_the_hint_kept():
    schema = {f"field_number_{i}_with_a_long_name": "string" for i in range(12)}
    msg = engine.skill_schema_message(_node(schema, extra_fields=[f"extra_{i}" for i in range(12)]))
    assert len(msg) <= 200 and msg.endswith(HINT)


def test_no_output_schema_passes_unchanged():
    assert engine.skill_schema_message(_node()) is None


def test_output_schema_naming_only_the_skills_fields_passes():
    assert engine.skill_schema_message(
        _node({"bucket": "string", "confidence": "float"})) is None


def test_json_schema_shaped_output_schema_is_read_by_its_properties():
    ok = {"type": "object", "properties": {"bucket": {"type": "string"}}}
    bad = {"type": "object", "properties": {"urgency": {"type": "string"}}}
    assert engine.skill_schema_message(_node(ok)) is None
    assert engine.skill_schema_message(_node(bad)) is not None


def test_extra_fields_cover_the_named_field():
    assert engine.skill_schema_message(
        _node({"bucket": "string", "urgency": "string"}, extra_fields=["urgency"])) is None


def test_plain_apply_to_each_with_its_own_schema_is_unchanged():
    node = {"type": "apply_to_each", "id": "n1", "instruction": "Rate each.",
            "items": ["a"], "output_schema": JOB_A}
    assert engine.skill_schema_message(node) is None


def test_unreadable_skill_is_left_to_the_builder():
    assert engine.skill_schema_message(_node(JOB_A, skill_id="no_such_skill")) is None


# --- end to end: the step fails before any call and re_plan sees why --------

@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skill_schema.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-skill-schema")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _failure(stub):
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    body = replans[0].system + "\n" + replans[0].user
    code = re.search(r'"failure": \{[^{}]*?"code": "(BRT-[A-Z]+-\d+)"', body)
    assert code, body[-600:]
    msg = re.search(r'"message": ("(?:[^"\\]|\\.)*")', body[code.start():])
    assert msg, body[-600:]
    return code.group(1), json.loads(msg.group(1))


@pytest.mark.asyncio
async def test_job_a_step_fails_before_any_call_and_re_plan_is_shown_it(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_node(JOB_A)]).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    code, message = _failure(stub)
    assert code == "BRT-ORC-035"
    assert HINT in message and len(message) <= 200
    assert not stub.of_kind("answerer"), "the step made a model call"


@pytest.mark.asyncio
async def test_the_same_plan_again_does_not_loop_forever(env):
    db_path, mp = env
    again = {"action": "continue", "rationale": "same again", "next_nodes": [_node(JOB_A)]}
    stub = StubModels(plan_nodes=[_node(JOB_A)], replan_for=lambda req: again).install(mp)
    await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert 1 <= len(stub.of_kind("re_plan")) <= engine.MAX_LOOPS
    assert not stub.of_kind("answerer")


@pytest.mark.asyncio
async def test_a_valid_skill_step_still_runs(env):
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_node({"bucket": "string"})],
        answer_for=lambda req: {"bucket": "billing", "confidence": 0.9, "defer_reason": ""},
    ).install(mp)
    result = await run_pipeline("Sort these.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert stub.of_kind("answerer")
