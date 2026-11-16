"""Law 1 — Uncompacted Governance, end to end.

The operator's instruction reaches every model call verbatim: every step,
every item of an apply_to_each, and every call inside a child run. A step's
own text is its task, never a stand-in for what the operator asked.
"""
import uuid

import pytest

from src.agents.executor import execute
from src.envelopes.schema import WrappedTask
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

ROOT = (
    "Review the two client notes below and tell me which client to call first.\n"
    "1. alpha: contract ends Friday\n"
    "2. beta: asked for a new quote"
)


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all
    path = str(tmp_path / "law1.db")
    await create_all(path)
    return path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-law1")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return monkeypatch


async def _run(stub: StubModels, env, db: str) -> StubModels:
    stub.install(env)
    result = await run_pipeline(ROOT, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    return stub


@pytest.mark.asyncio
async def test_every_item_of_an_apply_to_each_carries_the_operators_instruction(db, env):
    plan = [
        {"type": "apply_to_each", "id": "n1", "instruction": "Label this note.",
         "items": ["alpha: contract ends Friday", "beta: asked for a new quote"],
         "tier": "light", "output_schema": {"result": "string"}},
    ]
    stub = await _run(StubModels(plan_nodes=plan), env, db)

    items = stub.of_kind("answerer")
    assert len(items) == 2
    for req in items:
        assert req.governance_instruction == ROOT
        assert "Label this note." in req.system      # the step text is still the task


@pytest.mark.asyncio
async def test_a_child_run_is_governed_by_the_operators_instruction_not_its_step_text(db, env):
    sub_task = "Work out which contract is at risk."
    leaf = "Read the note and name the risk."

    def plan_for(req):
        if req.user == ROOT:
            return [{"type": "orchestrate", "id": "n1", "instruction": sub_task,
                     "output_schema": {}}]
        return [{"type": "answerer", "id": "c1", "instruction": leaf,
                 "tier": "light", "output_schema": {"result": "string"}}]

    stub = await _run(StubModels(plan_nodes=[], plan_for=plan_for), env, db)

    # Every call in the parent and in the child shows the operator's words.
    assert len(stub.of_kind("plan")) == 2
    for req in stub.requests:
        assert req.governance_instruction == ROOT, (
            f"{req.kind} call on {req.model} was governed by {req.governance_instruction!r}"
        )

    # The child still knows its own assignment: as the plan-mode message, and
    # in the prompt of its later calls (which only say "continue").
    child_plan = next(r for r in stub.of_kind("plan") if r.user != ROOT)
    assert child_plan.user == sub_task
    child_later = [
        r for r in stub.requests
        if r.kind in ("re_plan", "compose") and sub_task in r.system
    ]
    assert {r.kind for r in child_later} == {"re_plan", "compose"}

    # The grandchild step: governed by the operator, tasked with the leaf.
    leaf_req = stub.step_request(leaf)
    assert leaf_req.governance_instruction == ROOT


@pytest.mark.asyncio
async def test_a_grandchild_runs_step_is_governed_by_the_operators_instruction(db, env):
    """Depth 2: root run -> orchestrate step -> child run -> orchestrate step
    -> grandchild run -> answerer step."""
    sub_task = "Work out which contract is at risk."
    sub_sub_task = "Check the alpha contract's end date."
    leaf = "Read the alpha note and give the end date."

    def plan_for(req):
        if req.user == ROOT:
            return [{"type": "orchestrate", "id": "n1", "instruction": sub_task,
                     "output_schema": {}}]
        if req.user == sub_task:
            return [{"type": "orchestrate", "id": "c1", "instruction": sub_sub_task,
                     "output_schema": {}}]
        return [{"type": "answerer", "id": "g1", "instruction": leaf,
                 "tier": "light", "output_schema": {"result": "string"}}]

    stub = await _run(StubModels(plan_nodes=[], plan_for=plan_for), env, db)

    # Three runs planned: root, child, grandchild.
    assert [r.user for r in stub.of_kind("plan")] == [ROOT, sub_task, sub_sub_task]
    grandchild_step = stub.step_request(leaf)
    assert grandchild_step.governance_instruction == ROOT
    assert ROOT in grandchild_step.system
    for req in stub.requests:
        assert req.governance_instruction == ROOT, (
            f"{req.kind} call was governed by {req.governance_instruction!r}"
        )


@pytest.mark.asyncio
async def test_a_skill_step_is_governed_by_the_operators_instruction(db, env):
    plan = [{"type": "answerer", "id": "n1", "skill_id": "verify_claim", "skill_version": 1,
             "skill_args": {"claim": "Alpha's contract ends Friday.", "evidence_ref": "notes"}}]
    stub = await _run(StubModels(plan_nodes=plan), env, db)

    steps = stub.of_kind("answerer")
    assert len(steps) == 1
    assert steps[0].governance_instruction == ROOT
    assert "Alpha's contract ends Friday." in steps[0].system   # the skill's task is still there


@pytest.mark.asyncio
async def test_a_parent_runs_own_calls_carry_no_assignment_block(db, env):
    plan = [{"type": "answerer", "id": "n1", "instruction": "Pick the client.",
             "output_schema": {"result": "string"}}]
    stub = await _run(StubModels(plan_nodes=plan), env, db)
    for req in stub.requests:
        assert "Your Assignment In This Run" not in req.system


@pytest.mark.asyncio
async def test_a_task_built_without_the_operators_instruction_falls_back_to_its_own(db, env):
    """Tasks made outside the engine (older callers, resumed envelopes saved
    before this field existed) still get a governance block."""
    stub = StubModels(plan_nodes=[]).install(env)
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        model_endpoint="https://openrouter.ai/api",
        model_name="openrouter/stub/cheap",
        instruction="Standalone step.",
        output_schema={"result": "string"},
    )
    assert task.governance_instruction is None

    env_out = await execute(task, db_path=db)
    await drain_log_writes()

    assert env_out.status == "resolved"
    assert stub.requests[0].governance_instruction == "Standalone step."
