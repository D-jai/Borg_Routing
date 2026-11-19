"""Demo run 19f56430: a skill step with ``items_ref: null`` and its items inline.

The planner wrote an extract_fields apply_to_each step with the items in
``skill_args.items`` and ``items_ref: null`` (the prompt says: give the
items and leave the ref out), plus two group_by state_ops reading it. The
step's build failed with no event and no envelope; its dependents then ran
with no items (BRT-SOP-003), and re_plan re-issued the same step until
BRT-ORC-005.

- A null ``*_ref`` beside inline items is the same as leaving the ref out:
  the step runs, one call per item, before its dependents.
- A step whose build really fails is logged (BRT-ORC-008 event), and a
  state_op whose ``source`` failed is not run: it fails with BRT-APE-005.

The real engine, executor and router run against the scripted stand-in
model server. Nothing here needs a key or the network.
"""
import json
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

LINES = [
    "1. The Tidewell app reminds me to drink water and I actually do it now, love it.",
    "2. Syncing with my bottle takes forever, sometimes it never connects.",
    "3. Battery on the bottle lasts about two weeks, which is fine.",
]
EXTRACT = "Extract the following fields"


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "null_items_ref.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-null-ref")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _demo_plan(items):
    """The exact node shape of the demo run's plan, with ``items`` inline."""
    n1 = {
        "type": "apply_to_each", "id": "n1",
        "skill_id": "extract_fields", "skill_version": 1,
        "skill_args": {
            "items_ref": None,
            "items": items,
            "fields": {
                "line_number": "integer",
                "sentiment": "string (one of: positive, negative, neutral, mixed)",
                "feature": "string (the single main product feature mentioned, or 'none')",
            },
        },
        "concurrency_limit": 20, "tier": "light", "task_type": "extraction",
        "output_schema": {"line_number": "integer", "sentiment": "string",
                          "feature": "string"},
    }
    n2 = {"type": "state_op", "id": "n2", "op": "group_by", "source": "n1",
          "params": {"group_by": "sentiment", "count": True}, "depends_on": ["n1"]}
    n3 = {"type": "state_op", "id": "n3", "op": "group_by", "source": "n1",
          "params": {"group_by": "feature", "count": True, "order": "count desc",
                     "limit": 5},
          "depends_on": ["n1"]}
    return [n1, n2, n3]


def _answer(req):
    return {"line_number": 1, "sentiment": "positive", "feature": "reminders"}


def _compose_once(_req):
    return {"action": "compose", "rationale": "done"}


def _envelopes(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT type, status, inbound_payload FROM envelopes ORDER BY rowid"
        ).fetchall()


def _error_events(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error' ORDER BY rowid"
        ).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


@pytest.mark.asyncio
async def test_null_items_ref_with_inline_items_runs_before_its_dependents(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=_demo_plan(LINES), answer_for=_answer,
                      replan_for=_compose_once).install(mp)
    result = await run_pipeline("Classify each feedback line.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    calls = [r for r in stub.of_kind("answerer") if EXTRACT in r.system]
    assert len(calls) == len(LINES), f"{len(calls)} per-item calls"
    for line in LINES:
        assert sum(line in r.system for r in calls) == 1, line

    rows = _envelopes(db_path)
    types = [r[0] for r in rows]
    assert "apply_to_each" in types, rows
    ape = types.index("apply_to_each")
    assert rows[ape][1] == "resolved", rows[ape]
    # The dependents come after the step they read, and never with no items.
    sops = [i for i, t in enumerate(types) if t == "state_op"]
    assert len(sops) == 2 and all(i > ape for i in sops), types
    for i in sops:
        assert "BRT-SOP-003" not in (rows[i][2] or ""), rows[i]
    assert not any(e.get("code") == "BRT-ORC-008" for e in _error_events(db_path))


@pytest.mark.asyncio
async def test_invalid_skill_step_fails_visibly_and_its_state_ops_do_not_run(env):
    db_path, mp = env
    # items_ref null and no items anywhere: really invalid.
    plan = _demo_plan(LINES)
    del plan[0]["skill_args"]["items"]
    stub = StubModels(plan_nodes=plan, answer_for=_answer,
                      replan_for=_compose_once).install(mp)
    await run_pipeline("Classify each feedback line.", db_path=db_path)
    await drain_log_writes()

    assert not [r for r in stub.of_kind("answerer") if EXTRACT in r.system]
    events = _error_events(db_path)
    build = [e for e in events if e.get("code") == "BRT-ORC-008"]
    assert build and build[0].get("node_id") == "n1", events
    assert "items_ref" in build[0].get("msg", "")

    # The dependents were not run: no state_op envelope, no BRT-SOP-003.
    rows = _envelopes(db_path)
    assert not [r for r in rows if r[0] == "state_op"], rows
    dead = {e.get("node_id") for e in events if e.get("code") == "BRT-APE-005"}
    assert dead == {"n2", "n3"}, events

    # re_plan sees a real reason for every step.
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    text = replans[0].system + replans[0].user
    assert "BRT-ORC-008" in text and "items_ref" in text
    assert "BRT-APE-005" in text
    assert "BRT-SOP-003" not in text
