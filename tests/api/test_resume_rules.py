"""Resume rules (4.3 review): a running run is refused, and a failed run with
no saved steps (it failed at planning) is re-planned from its own instruction.
"""
import json
import uuid

import pytest
from fastapi import BackgroundTasks, HTTPException

from src.api import tasks as tasks_api
from src.db.state import create_run, update_run_status
from src.orchestrator import engine
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

OPERATOR = "Tell me which client to call first."


@pytest.fixture
async def db(tmp_path, monkeypatch):
    from src.db.schema import create_all
    path = str(tmp_path / "resume_rules.db")
    await create_all(path)
    monkeypatch.setattr(tasks_api, "DB_PATH", path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-resume")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return path


@pytest.mark.asyncio
async def test_resume_refuses_a_running_run(db):
    run_id = await create_run(OPERATOR, db_path=db)
    await update_run_status(run_id, "running", db_path=db)
    with pytest.raises(HTTPException) as exc:
        await tasks_api.resume_run_endpoint(str(run_id), BackgroundTasks())
    assert exc.value.status_code == 409
    assert exc.value.detail["error"] == "BRT-API-024"
    assert "running" in exc.value.detail["message"]


@pytest.mark.asyncio
async def test_failed_run_with_no_steps_is_replanned_from_its_instruction(db, monkeypatch):
    run_id = await create_run(OPERATOR, db_path=db)
    await update_run_status(run_id, "failed",
                            failure_reason="BRT-ORC-007: planner gave no plan", db_path=db)
    plan = [{"type": "answerer", "id": "n1", "instruction": "Pick the client.",
             "tier": "light", "output_schema": {"result": "string"}}]
    stub = StubModels(plan_nodes=plan).install(monkeypatch)

    background = BackgroundTasks()
    resp = await tasks_api.resume_run_endpoint(str(run_id), background)
    await background()
    await drain_log_writes()

    body = json.loads(resp.body)
    assert body["status"] == "resuming"
    assert body["retrying"] == 0
    assert stub.of_kind("plan")[0].user == OPERATOR
    for req in stub.requests:
        assert req.governance_instruction == OPERATOR


@pytest.mark.asyncio
async def test_pending_run_with_nothing_saved_has_no_pending_work(db):
    run_id = await create_run(OPERATOR, db_path=db)
    resp = await tasks_api.resume_run_endpoint(str(run_id), BackgroundTasks())
    assert json.loads(resp.body)["status"] == "no_pending_work"


@pytest.mark.asyncio
async def test_completed_run_stays_refused(db):
    run_id = await create_run(OPERATOR, db_path=db)
    await update_run_status(run_id, "completed", db_path=db)
    with pytest.raises(HTTPException) as exc:
        await tasks_api.resume_run_endpoint(str(run_id), BackgroundTasks())
    assert exc.value.status_code == 400
