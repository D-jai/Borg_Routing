"""Planner reading (2) under ruling 35: a missing EXTRA field is retried, then kept.

Extra fields are those a skill step adds through ``skill_args.extra_fields``.
Where a failing item is retried today (a packed item, ruling 23: once alone) a
reply without an extra field takes that same retry; a single unpacked call has
no schema retry, and none is added. Still without it, the item is kept with the
fields it has. A deferred item needs no extra field. The skill's own fields
stay as strict as before.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.envelopes.validator import missing_extra_fields, validate_inbound
from src.orchestrator import compose_checks as cc
from src.orchestrator import engine
from src.orchestrator import row_markers as rm
from tests.e2e.test_item_packing import OPERATOR, _run, pk  # noqa: F401

URGENT = {"bucket": "a", "confidence": 0.9, "urgency": 3}
NO_URGENCY = {"bucket": "a", "confidence": 0.9}
DEFER = {"bucket": "defer", "confidence": 0.2, "defer_reason": "no bucket fits"}
MISSING = "note 003"


def _items(n):
    return [f"note {i:03d}" for i in range(n)]


def _node(items):
    return {"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets",
            "skill_args": {"buckets": ["a", "b"], "items_ref": items,
                           "extra_fields": {"urgency": "integer 1-5"}}}


def _from_node(node) -> WrappedTask:
    return engine._build_task_from_node(
        node, run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1",
        model_endpoint="https://openrouter.ai/api", model_name="openrouter/stub/cheap",
        user_instruction=OPERATOR, results={}, data_scope="derived", lane=None)


def _text_of(req) -> str:
    found = [t for t in _items(40) if t in req.system or t in (req.user or "")]
    return found[-1]


def _by_text(table: dict, default: dict):
    return (lambda r, t: dict(table.get(t, default)),
            lambda r: dict(table.get(_text_of(r), default)))


async def _step(pk, table: dict, n: int = 10, default: dict = URGENT):  # noqa: F811
    pack_for, answer_for = _by_text(table, default)
    return await _run(pk, _from_node(_node(_items(n))),
                      pack_item_for=pack_for, answer_for=answer_for)


def _notes(db, run_id) -> list:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE run_id=? AND event_type='orchestrator_decision'",
            (str(run_id),),
        ).fetchall()
    return [d for d in (json.loads(r[0]) for r in rows if r[0])
            if d.get("mode") == "extra_fields_missing"]


def _row(env, item):
    return next(r for r in env.output["results"] if r["item"] == item)


def test_the_built_step_knows_which_fields_are_extra():
    task = _from_node(_node(_items(2)))
    assert task.extra_fields == ["urgency"]
    assert "urgency" in task.output_schema


def test_a_step_without_extra_fields_has_none():
    node = _node(_items(2))
    del node["skill_args"]["extra_fields"]
    assert _from_node(node).extra_fields == []


# (a) packed --------------------------------------------------------------------

async def test_a_packed_item_without_urgency_is_retried_alone_once_then_kept(pk):  # noqa: F811
    stub, env = await _step(pk, {MISSING: NO_URGENCY})
    assert len(stub.of_kind("packed")) == 1
    singles = stub.of_kind("answerer")
    assert len(singles) == 1 and MISSING in singles[0].system
    out = env.output
    assert out["success_count"] == 10 and out["failures"] == []
    kept = _row(env, MISSING)["output"]
    assert kept == {"bucket": "a", "confidence": 0.9} and "urgency" not in kept
    assert _row(env, "note 004")["output"]["urgency"] == 3
    notes = _notes(pk["db"], env_run_id(stub, env))
    assert notes == [{"mode": "extra_fields_missing", "step": str(env.envelope_id),
                      "fields": {"urgency": 1}}]


async def test_a_packed_item_whose_retry_brings_urgency_keeps_it(pk):  # noqa: F811
    stub, env = await _run(
        pk, _from_node(_node(_items(10))),
        pack_item_for=lambda r, t: dict(NO_URGENCY if t == MISSING else URGENT),
        answer_for=lambda r: dict(URGENT))
    assert len(stub.of_kind("answerer")) == 1
    assert _row(env, MISSING)["output"]["urgency"] == 3
    assert _notes(pk["db"], env_run_id(stub, env)) == []


async def test_forty_packed_items_without_urgency_are_all_retried_and_all_kept(pk):  # noqa: F811
    # The shape ruling 35 replaces (tests/skills/test_catalogue_extra_fields.py
    # pinned it failing every item).
    stub, env = await _step(pk, {}, n=40, default=NO_URGENCY)
    assert [len(r.pack_ids) for r in stub.of_kind("packed")] == [20, 20]
    assert len(stub.of_kind("answerer")) == 40
    assert env.output["success_count"] == 40 and env.output["failure_count"] == 0
    assert all("urgency" not in r["output"] for r in env.output["results"])
    notes = _notes(pk["db"], env_run_id(stub, env))
    assert [n["fields"] for n in notes] == [{"urgency": 40}]


async def test_a_null_urgency_is_retried_and_kept_as_null(pk):  # noqa: F811
    stub, env = await _step(pk, {MISSING: {**NO_URGENCY, "urgency": None}})
    assert len(stub.of_kind("answerer")) == 1
    assert _row(env, MISSING)["output"]["urgency"] is None
    assert [n["fields"] for n in _notes(pk["db"], env_run_id(stub, env))] == [{"urgency": 1}]


# (b) unpacked ------------------------------------------------------------------

async def test_an_unpacked_item_without_urgency_gets_no_retry_and_is_kept(pk):  # noqa: F811
    pk["mp"].setenv("BORG_PACK_MAX_ITEMS", "0")
    stub, env = await _step(pk, {MISSING: NO_URGENCY})
    assert stub.of_kind("packed") == []
    singles = stub.of_kind("answerer")
    assert len(singles) == 10                       # one call per item, no retry added
    assert sum(MISSING in r.system for r in singles) == 1
    assert env.output["success_count"] == 10 and env.output["failures"] == []
    assert _row(env, MISSING)["output"] == NO_URGENCY
    assert [n["fields"] for n in _notes(pk["db"], env_run_id(stub, env))] == [{"urgency": 1}]


# (c) deferred ------------------------------------------------------------------

async def test_a_deferred_packed_item_without_urgency_is_kept_with_no_retry(pk):  # noqa: F811
    stub, env = await _step(pk, {MISSING: DEFER})
    assert stub.of_kind("answerer") == []
    assert _row(env, MISSING)["output"] == DEFER
    assert env.output["failures"] == []
    assert _notes(pk["db"], env_run_id(stub, env)) == []


async def test_forty_deferred_items_without_urgency_are_all_kept_with_no_retry(pk):  # noqa: F811
    stub, env = await _step(pk, {}, n=40, default=DEFER)
    assert stub.of_kind("answerer") == []
    assert env.output["success_count"] == 40 and env.output["failure_count"] == 0
    assert _notes(pk["db"], env_run_id(stub, env)) == []


async def test_a_deferred_unpacked_item_without_urgency_is_kept(pk):  # noqa: F811
    pk["mp"].setenv("BORG_PACK_MAX_ITEMS", "0")
    stub, env = await _step(pk, {MISSING: DEFER})
    assert len(stub.of_kind("answerer")) == 10
    assert _row(env, MISSING)["output"] == DEFER


# (d) a present value -------------------------------------------------------------

async def test_a_present_urgency_of_another_type_is_kept_while_a_missing_one_is_retried(pk):  # noqa: F811
    # The inbound check tests presence only (validate_inbound checks no types),
    # so a present value of any type is kept as given, with no retry; in the
    # same pack, the item without urgency is retried alone and kept.
    stub, env = await _step(pk, {MISSING: {**NO_URGENCY, "urgency": "very high"},
                                 "note 005": NO_URGENCY})
    singles = stub.of_kind("answerer")
    assert len(singles) == 1 and "note 005" in singles[0].system
    assert _row(env, MISSING)["output"]["urgency"] == "very high"
    assert _row(env, "note 005")["output"] == NO_URGENCY
    assert env.output["failures"] == []
    assert [n["fields"] for n in _notes(pk["db"], env_run_id(stub, env))] == [{"urgency": 1}]


# (e) the skill's own field -------------------------------------------------------

async def test_a_missing_own_field_fails_while_a_missing_extra_field_is_kept(pk):  # noqa: F811
    stub, env = await _step(pk, {MISSING: {"bucket": "a", "urgency": 2},
                                 "note 005": NO_URGENCY})
    assert len(stub.of_kind("answerer")) == 2      # ruling 23's retry, for each
    fails = env.output["failures"]
    assert [f["item"] for f in fails] == [MISSING]
    assert "BRT-ANS-005" in str(fails[0]["reason"]) and "confidence" in str(fails[0]["reason"])
    assert _row(env, "note 005")["output"] == NO_URGENCY
    assert env.output["success_count"] == 9


def test_the_validator_still_requires_own_fields_and_only_if_stays_optional():
    task = _from_node(_node(_items(2)))
    child = task.model_copy(update={"type": "answerer"})

    def env(output):
        return InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                               status="resolved", confidence=0.9, output=output)

    validate_inbound(env(NO_URGENCY), child)                 # extra field absent: passes
    assert missing_extra_fields(env(NO_URGENCY), child) == ["urgency"]
    assert missing_extra_fields(env(URGENT), child) == []
    assert missing_extra_fields(env(DEFER), child) == []     # deferred: none needed
    validate_inbound(env(DEFER), child)
    validate_inbound(env(NO_URGENCY), child)                 # defer_reason "(only if": optional
    try:
        validate_inbound(env({"bucket": "a", "urgency": 1}), child)
    except ValueError as exc:
        assert "BRT-ENV-011" in str(exc) and "confidence" in str(exc)
    else:
        raise AssertionError("a missing own field must fail")


def test_an_only_if_extra_field_is_never_retried():
    node = _node(_items(2))
    node["skill_args"]["extra_fields"] = {"note": "string (only if bucket='a')"}
    child = _from_node(node).model_copy(update={"type": "answerer"})
    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                          status="resolved", confidence=0.9, output=NO_URGENCY)
    validate_inbound(env, child)
    assert missing_extra_fields(env, child) == []


# (f) the compose block -----------------------------------------------------------

async def test_the_compose_block_counts_the_kept_item_under_no_value(pk):  # noqa: F811
    stub, env = await _step(pk, {MISSING: NO_URGENCY})
    env._node_type = "apply_to_each"
    results = {"n1": env}
    steps = rm.list_steps(results, None)
    model = cc.ItemModel.build(steps, results)
    assert ("urgency", 1) in model.no_value_counts()
    block = rm.block(steps, {}, len(model.no_result()), model.no_value_counts(), [])
    assert "Rows with no value (a step returned the item but left the field empty): urgency 1." \
        in block


async def test_the_compose_block_counts_urgency_40_when_no_row_holds_it(pk):  # noqa: F811
    stub, env = await _step(pk, {}, n=40, default=NO_URGENCY)
    assert env._extra_fields == ["urgency"]
    env._node_type = "apply_to_each"               # as the engine stamps it
    results = {"n1": env}
    steps = rm.list_steps(results, None)
    assert steps["n1"].extra_fields == ["urgency"]
    assert "urgency" not in steps["n1"].fields   # not offered for placing
    model = cc.ItemModel.build(steps, results)
    assert ("urgency", 40) in model.no_value_counts()
    block = rm.block(steps, {}, len(model.no_result()), model.no_value_counts(), [])
    assert "Rows with no value (a step returned the item but left the field empty): urgency 40." \
        in block
    # C4 (BRT-ORC-041) catches an urgency invented for those rows.
    reply = '[{"item": "note 000", "bucket": "a", "urgency": 4}]'
    case1, case2 = cc.invented_values(reply, model, [])
    assert case1 == [model.match("note 000")] and case2 == {}
    problems = cc.check(reply, steps, {}, model, [], set(), ["n1"])
    assert [code for code, _ in problems] == ["BRT-ORC-041"]
    # The stored bucket is not invented.
    assert cc.invented_values('[{"item": "note 000", "bucket": "a"}]', model, []) == ([], {})


# review round 1, item 1: what a deferral is ------------------------------------

async def test_an_extra_field_holding_defer_is_not_a_deferral(pk):  # noqa: F811
    node = _node(_items(10))
    node["skill_args"]["extra_fields"] = {"urgency": "integer 1-5", "next_action": "string"}
    reply = {**NO_URGENCY, "next_action": "defer"}
    full = {**URGENT, "next_action": "call"}
    stub, env = await _run(pk, _from_node(node),
                           pack_item_for=lambda r, t: dict(reply if t == MISSING else full),
                           answer_for=lambda r: dict(reply))
    singles = stub.of_kind("answerer")
    assert len(singles) == 1 and MISSING in singles[0].system   # still retried
    assert _row(env, MISSING)["output"] == reply
    assert [n["fields"] for n in _notes(pk["db"], env_run_id(stub, env))] == \
        [{"urgency": 1, "next_action": 0}]


def test_a_transform_style_text_of_defer_is_not_a_deferral():
    from src.envelopes.validator import is_deferred
    node = {"type": "apply_to_each", "id": "n1", "skill_id": "transform_style",
            "skill_args": {"items_ref": ["x"], "target_style": "plain",
                           "extra_fields": {"urgency": "integer 1-5"}}}
    task = _from_node(node)
    assert task.extra_fields == ["urgency"]
    out = {"rewritten": "Defer", "meaning_preserved": True}
    assert not is_deferred(out, task)
    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                          status="resolved", confidence=0.9, output=out)
    assert missing_extra_fields(env, task) == ["urgency"]


def test_defer_and_collect_result_or_defer_is_a_deferral():
    from src.envelopes.validator import is_deferred
    node = {"type": "apply_to_each", "id": "n1", "skill_id": "defer_and_collect",
            "skill_args": {"items_ref": ["x"], "instruction": "Label it.",
                           "output_schema": {"label": "string"}, "confidence_threshold": 0.5,
                           "extra_fields": {"urgency": "integer 1-5"}}}
    task = _from_node(node)
    assert task.extra_fields == ["urgency"]
    assert is_deferred({"result_or_defer": "defer", "confidence": 0.2}, task)
    assert not is_deferred({"result_or_defer": {"label": "defer"}, "confidence": 0.9}, task)


# review round 1, item 2: a plain answerer step stays strict ---------------------

def test_a_plain_answerer_skill_step_keeps_extra_fields_strict():
    node = {"type": "answerer", "id": "v1", "skill_id": "verify_claim",
            "skill_args": {"claim": "The sky is green.", "evidence_ref": ["It is blue."],
                           "extra_fields": {"urgency": "integer 1-5"}}}
    task = _from_node(node)
    assert task.type == "answerer" and "urgency" in task.output_schema
    assert task.extra_fields == []
    out = {"verdict": "contradicted", "confidence": 0.9, "cited_evidence_ids": [],
           "explanation": "blue"}
    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                          status="resolved", confidence=0.9, output=out)
    try:
        validate_inbound(env, task)
    except ValueError as exc:
        assert "BRT-ENV-011" in str(exc) and "urgency" in str(exc)
    else:
        raise AssertionError("a plain answerer step must still require its extra field")
    validate_inbound(env.model_copy(update={"output": {**out, "urgency": 2}}), task)


# review round 1, item 5: redaction ---------------------------------------------

def test_a_redacted_viewer_keeps_the_note_and_the_pack_count():
    from src.api.tasks import _redact_events
    long = "u" * 60
    fields = {long: 2, **{f"f{i}": i for i in range(12)}, "bad": "x"}
    events = [
        {"event_type": "orchestrator_decision", "decision_json": json.dumps(
            {"mode": "extra_fields_missing", "step": "s" * 50, "fields": fields,
             "secret": "document text"})},
        {"event_type": "orchestrator_decision", "decision_json": json.dumps(
            {"mode": "pack_outcome", "step": "s", "items": 3, "invalid": 1,
             "extra_missing": 1, "text": "document text"})},
    ]
    note, outcome = (json.loads(e["decision_json"]) for e in _redact_events(events))
    assert note["mode"] == "extra_fields_missing" and note["step"] == "s" * 40
    assert "secret" not in note and len(note["fields"]) <= 10
    assert note["fields"]["u" * 40] == 2 and "bad" not in note["fields"]
    assert outcome["extra_missing"] == 1 and "text" not in outcome


def env_run_id(stub, env):
    return stub.task.run_id
