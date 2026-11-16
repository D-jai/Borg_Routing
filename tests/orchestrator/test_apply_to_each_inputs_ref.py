"""Tests for BRT-APE-01 — apply_to_each inputs_ref resolution + empty-items.

TC-APE-010 — _build_task_from_node resolves inputs_ref="<node_id>" against
             a prior envelope's output.items → dependency_input['items'].
TC-APE-011 — empty resolved items raises BRT-APE-005 at expand time
             (visible failed envelope, not a silent 0-result success).
TC-APE-012 — string inputs_ref with no matching upstream node falls through
             to BRT-APE-001 (no items + no inline list).
"""
import uuid

import pytest

from src.db.schema import create_all
from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.orchestrator.apply_to_each import expand_and_run
from src.orchestrator.engine import _build_task_from_node


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "ape_ir.db")
    await create_all(path)
    return path


def _prior_env(items: list) -> InboundEnvelope:
    return InboundEnvelope(
        envelope_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        status="resolved",
        output={"items": items},
    )


def test_inputs_ref_string_resolved_from_results():
    """TC-APE-010 — inputs_ref='n1' + results={n1: env with items=[...]}
    → built task has dependency_input['items'] populated."""
    node = {
        "type": "apply_to_each",
        "id": "n2",
        "instruction": "process one item",
        "inputs_ref": "n1",
        "output_schema": {"ok": "bool"},
    }
    results = {"n1": _prior_env([{"x": 1}, {"x": 2}, {"x": 3}])}

    task = _build_task_from_node(
        node=node,
        run_id=uuid.uuid4(),
        current_depth=1,
        max_depth=5,
        level_path="1a",
        model_endpoint="http://localhost:1234",
        model_name="test",
        user_instruction="root prompt",
        results=results,
    )

    assert task.type == "apply_to_each"
    assert task.dependency_input["items"] == [{"x": 1}, {"x": 2}, {"x": 3}]
    assert task.dependency_input["inputs_ref"] == "n1"


@pytest.mark.asyncio
async def test_empty_items_raises_005(db):
    """TC-APE-011 — empty items list surfaces BRT-APE-005 as a failed
    envelope (not a silent resolved-with-zero-results)."""
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="apply_to_each",
        current_depth=1,
        max_depth=5,
        level_path="1",
        instruction="process item",
        output_schema={"ok": "bool"},
        dependency_input={"items": [], "concurrency_limit": 5},
        model_endpoint="http://localhost:1234",
        model_name="test",
    )

    env = await expand_and_run(task, db_path=db)

    assert env.status == "failed"
    assert env.failure_reason and "BRT-APE-005" in env.failure_reason
    assert env.output["total"] == 0
    assert env.output["success_count"] == 0


def test_inputs_ref_unknown_runs_once_over_the_operators_input():
    """TC-APE-012 — inputs_ref references a node not in results; node has
    no inline items. Since the Check 1 ruling (16-Nov-2026) the step is
    built as one answerer task over the operator's input, never a silent
    empty fan-out (BRT-ORC-024; the dispatch loop logs it)."""
    node = {
        "type": "apply_to_each",
        "id": "n2",
        "instruction": "x",
        "inputs_ref": "does_not_exist",
        "output_schema": {"ok": "bool"},
    }
    results = {}  # empty — no upstream to resolve from

    task = _build_task_from_node(
        node=node,
        run_id=uuid.uuid4(),
        current_depth=1,
        max_depth=5,
        level_path="1a",
        model_endpoint="http://localhost:1234",
        model_name="test",
        user_instruction="root prompt",
        results=results,
    )

    assert task.type == "answerer"
    assert task.instruction.endswith(": x")
    assert task.context.startswith("root prompt")
    assert task.governance_instruction == "root prompt"
    assert task.output_schema == {"results": [{"ok": "bool"}]}
    assert task.dependency_input == {"_unresolved_inputs_ref": "does_not_exist"}
