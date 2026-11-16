"""Law 1 on resume: POST /runs/{id}/resume re-runs under the run row's own
operator instruction, never under a saved step's text.

Envelopes saved before ``governance_instruction`` existed carry only the
step's text, so the run row is the one place the operator's words survive.
"""
import uuid

import pytest
from fastapi import BackgroundTasks

from src.api import tasks as tasks_api
from src.db.state import create_run, save_envelope
from src.envelopes.schema import WrappedTask
from src.orchestrator import engine
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

OPERATOR = (
    "Review the two client notes below and tell me which client to call first.\n"
    "1. alpha: contract ends Friday\n"
    "2. beta: asked for a new quote"
)
SAVED_STEP_TEXT = "Summarise note alpha in one line."
RE_RUN_STEP = "Pick the client to call first."


@pytest.fixture
async def db(tmp_path, monkeypatch):
    from src.db.schema import create_all
    path = str(tmp_path / "resume_law1.db")
    await create_all(path)
    monkeypatch.setattr(tasks_api, "DB_PATH", path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-resume")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return path


async def _save_pre_fix_step(run_id: uuid.UUID, db: str) -> None:
    """An in-flight step saved without governance_instruction (pre-fix shape)."""
    await save_envelope(
        WrappedTask(
            envelope_id=uuid.uuid4(),
            run_id=run_id,
            subtask_id=uuid.uuid4(),
            type="answerer",
            current_depth=1,
            max_depth=5,
            level_path="1a",
            model_endpoint="https://openrouter.ai/api",
            model_name="openrouter/stub/cheap",
            instruction=SAVED_STEP_TEXT,
            output_schema={"result": "string"},
        ),
        db_path=db,
    )


async def _resume(run_id: uuid.UUID) -> dict:
    background = BackgroundTasks()
    resp = await tasks_api.resume_run_endpoint(str(run_id), background)
    await background()
    await drain_log_writes()
    import json
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_a_resumed_run_is_governed_by_the_operators_instruction(db, monkeypatch):
    run_id = await create_run(OPERATOR, db_path=db)
    await _save_pre_fix_step(run_id, db)
    plan = [{"type": "answerer", "id": "n1", "instruction": RE_RUN_STEP,
             "tier": "light", "output_schema": {"result": "string"}}]
    stub = StubModels(plan_nodes=plan).install(monkeypatch)

    body = await _resume(run_id)

    assert body["status"] == "resuming"
    assert stub.of_kind("plan"), "the resume never re-ran the planner"
    assert stub.of_kind("plan")[0].user == OPERATOR
    for req in stub.requests:
        assert req.governance_instruction == OPERATOR, (
            f"{req.kind} call was governed by {req.governance_instruction!r}"
        )
    assert stub.step_request(RE_RUN_STEP).governance_instruction == OPERATOR


@pytest.mark.asyncio
async def test_a_resumed_child_run_keeps_its_task_and_the_roots_governance(db, monkeypatch):
    sub_task = "Work out which contract is at risk."
    root_id = await create_run(OPERATOR, db_path=db)
    child_id = await create_run(sub_task, parent_run_id=root_id, db_path=db)
    await _save_pre_fix_step(child_id, db)
    plan = [{"type": "answerer", "id": "c1", "instruction": RE_RUN_STEP,
             "tier": "light", "output_schema": {"result": "string"}}]
    stub = StubModels(plan_nodes=plan).install(monkeypatch)

    await _resume(child_id)

    assert stub.of_kind("plan")[0].user == sub_task
    for req in stub.requests:
        assert req.governance_instruction == OPERATOR
