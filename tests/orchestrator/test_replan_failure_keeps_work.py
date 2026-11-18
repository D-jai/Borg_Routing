"""Planner reading (2) under ruling 35 (readings under ruling 35 (2)): a
re_plan that fails for good (BRT-ORC-002 after its JSON-only retry, the
non-object case included) goes to compose with the steps already finished,
the way MAX_LOOPS ends a run, when at least one step has resolved. The run
carries BRT-ORC-002 as a warning, and compose is told in one block line that
planning stopped early. With nothing resolved the run fails as before
(BRT-PIP-002); rulings 26a/26b/26e/26f apply unchanged; if compose fails too,
the existing salvage runs.
"""
import json
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine, row_markers
from src.pipeline.runner import run_pipeline
from tests.e2e.test_privacy_lane import _run as _lane_run, lane  # noqa: F401  (lane is a fixture)
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

ANSWERER = {"type": "answerer", "id": "n1", "instruction": "Say hi.", "tier": "light",
            "output_schema": {"result": "string"}}
NOT_JSON = "I think we should keep going, but here is no JSON."


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "replan_fail.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-replan-fail")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _errors(db_path):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute("SELECT decision_json FROM logs WHERE event_type='error'").fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


async def _answer_kind(db_path, run_id):
    from uuid import UUID

    from src.db.state import get_run
    return (await get_run(UUID(str(run_id)), db_path=db_path)).get("answer_kind")


def _stopped(db_path):
    return [d for d in _errors(db_path) if d.get("code") == "BRT-ORC-002"
            and d.get("severity") == "warning" and d.get("action") == "planning_stopped"]


@pytest.mark.parametrize("bad", [NOT_JSON, '"compose now"', '[{"action": "compose"}]'])
@pytest.mark.asyncio
async def test_a_re_plan_that_fails_for_good_composes_from_the_finished_steps(env, bad):
    db_path, mp = env
    stub = StubModels(plan_nodes=[ANSWERER], replan_for=lambda r: bad,
                      answer_for=lambda r: {"result": "hi"}).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("re_plan")) == 2                 # the reply, then the JSON-only retry
    composes = stub.of_kind("compose")
    assert composes, "compose was not called"
    assert row_markers.STOPPED_EARLY in composes[0].user
    assert len(_stopped(db_path)) == 1
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.asyncio
async def test_with_nothing_resolved_a_failed_re_plan_keeps_the_failure_path(env):
    db_path, mp = env
    widget = {"type": "widget", "id": "n1", "instruction": "x"}     # refused: nothing resolves
    stub = StubModels(plan_nodes=[widget], replan_for=lambda r: NOT_JSON).install(mp)
    result = await run_pipeline("Do x.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "failed"
    error = result.get("error") or ""
    assert "BRT-PIP-002" in error and "BRT-ORC-002" in error, error
    assert not stub.of_kind("compose")
    assert not _stopped(db_path)


@pytest.mark.asyncio
async def test_a_first_plan_that_cannot_be_parsed_keeps_the_failure_path(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[], plan_content_for=lambda r: NOT_JSON).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "failed" and "BRT-ORC-002" in (result.get("error") or "")
    assert not stub.of_kind("compose") and not _stopped(db_path)


async def test_with_documents_and_no_resolved_document_step_26b_still_fails(lane):
    """A plain step resolved, the document step failed every item (26f: not
    resolved): the early stop reaches the 26b check, which fails BRT-ORC-034."""
    plan = [dict(ANSWERER),
            {"type": "apply_to_each", "id": "n2", "inputs_ref": "documents",
             "instruction": "Extract the client.", "task_type": "extraction", "tier": "light",
             "output_schema": {"client": "string"}}]

    def answer(req):
        return {} if "Extract the client." in req.system else {"result": "hi"}

    stub, result = await _lane_run(lane, "by_reference", three_tier_matrix(), plan=plan,
                                   replan_for=lambda r: NOT_JSON, answer_for=answer)
    assert result["status"] == "failed"
    assert "BRT-ORC-034" in (result.get("error") or ""), result.get("error")
    assert not stub.of_kind("compose")


@pytest.mark.asyncio
async def test_compose_failing_after_the_early_stop_is_salvaged(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[ANSWERER], replan_for=lambda r: NOT_JSON,
                      compose_for=lambda r: NOT_JSON,
                      answer_for=lambda r: {"result": "hi"}).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    assert await _answer_kind(db_path, result["run_id"]) == "salvaged"
    codes = {d.get("code") for d in _errors(db_path)}
    assert {"BRT-ORC-002", "BRT-ORC-030"} <= codes
    assert len(_stopped(db_path)) == 1
