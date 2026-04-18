"""Tests for src/envelopes/schema.py.

TC-ENV-001 — WrappedTask roundtrip serialisation
TC-ENV-002 — InboundEnvelope roundtrip serialisation
TC-ENV-003 — BRT-ENV-001: status='unresolved' rejected by InboundEnvelope
TC-ENV-004 — BRT-ENV-002: invalid envelope type rejected
TC-ENV-005 — BRT-ENV-003: current_depth > max_depth rejected
TC-ENV-006 — BRT-ENV-004: empty instruction rejected
TC-ENV-007 — BRT-ENV-005: invalid SideEffectAction action rejected
TC-ENV-008 — WrappedTask defaults are correct
TC-ENV-009 — InboundEnvelope with status=failed roundtrips cleanly
TC-ENV-010 — SideEffectAction write and delete both accepted
"""
import uuid

import pytest
from pydantic import ValidationError

from src.envelopes.schema import InboundEnvelope, SideEffectAction, WrappedTask


def _make_wrapped_task(**kwargs) -> dict:
    return {
        "envelope_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "subtask_id": str(uuid.uuid4()),
        "type": "answerer",
        "current_depth": 1,
        "max_depth": 5,
        "level_path": "1a",
        "instruction": "Classify this email into one of 3 buckets.",
        "output_schema": {"bucket": "bug|feature|question"},
        **kwargs,
    }


def _make_inbound_envelope(**kwargs) -> dict:
    return {
        "envelope_id": str(uuid.uuid4()),
        "subtask_id": str(uuid.uuid4()),
        "status": "resolved",
        "output": {"bucket": "bug"},
        **kwargs,
    }


def test_wrapped_task_roundtrip():
    """TC-ENV-001 — WrappedTask serialises and deserialises cleanly."""
    data = _make_wrapped_task(
        parent_run_id=str(uuid.uuid4()),
        model_endpoint="http://localhost:1234",
        model_name="qwen3-coder",
        context="Previous context here.",
        skill_id="classify_into_buckets",
        skill_version=1,
    )
    task = WrappedTask(**data)
    assert task.type == "answerer"
    assert task.current_depth == 1
    task2 = WrappedTask(**task.model_dump())
    assert task2 == task


def test_inbound_envelope_resolved_roundtrip():
    """TC-ENV-002 — InboundEnvelope (resolved) serialises cleanly."""
    data = _make_inbound_envelope(
        confidence=0.95,
        tokens_used={"input": 200, "output": 50},
    )
    env = InboundEnvelope(**data)
    assert env.status == "resolved"
    env2 = InboundEnvelope(**env.model_dump())
    assert env2 == env


def test_inbound_envelope_status_unresolved_rejected():
    """TC-ENV-003 — BRT-ENV-001: status='unresolved' must be rejected."""
    with pytest.raises(ValidationError) as exc_info:
        InboundEnvelope(**_make_inbound_envelope(status="unresolved"))
    assert "unresolved" in str(exc_info.value).lower() or "resolved" in str(exc_info.value).lower()


def test_invalid_envelope_type_rejected():
    """TC-ENV-004 — BRT-ENV-002: unknown type raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        WrappedTask(**_make_wrapped_task(type="invalid_type"))
    assert "BRT-ENV-002" in str(exc_info.value)


def test_depth_exceeds_max_rejected():
    """TC-ENV-005 — BRT-ENV-003: current_depth > max_depth raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        WrappedTask(**_make_wrapped_task(current_depth=6, max_depth=5))
    assert "BRT-ENV-003" in str(exc_info.value)


def test_empty_instruction_rejected():
    """TC-ENV-006 — BRT-ENV-004: blank instruction raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        WrappedTask(**_make_wrapped_task(instruction="   "))
    assert "BRT-ENV-004" in str(exc_info.value)


def test_invalid_side_effect_action_rejected():
    """TC-ENV-007 — BRT-ENV-005: unknown action value raises ValidationError."""
    with pytest.raises(ValidationError) as exc_info:
        SideEffectAction(table="deferrals", action="upsert")
    assert "BRT-ENV-005" in str(exc_info.value)


def test_wrapped_task_defaults():
    """TC-ENV-008 — WrappedTask default fields are correct."""
    task = WrappedTask(**_make_wrapped_task())
    assert task.context == ""
    assert task.side_effects == []
    assert task.dependency_input == {}
    assert task.max_output_tokens == 1000
    assert task.trigger_reason == "initial_plan"
    assert task.skill_id is None
    assert task.parent_run_id is None


def test_inbound_envelope_failed_roundtrip():
    """TC-ENV-009 — InboundEnvelope with status=failed roundtrips cleanly."""
    data = _make_inbound_envelope(
        status="failed",
        output={},
        failure_reason="Model returned unparseable JSON.",
    )
    env = InboundEnvelope(**data)
    assert env.status == "failed"
    assert env.failure_reason == "Model returned unparseable JSON."
    env2 = InboundEnvelope(**env.model_dump())
    assert env2 == env


def test_side_effect_action_write_and_delete():
    """TC-ENV-010 — SideEffectAction accepts both 'write' and 'delete'."""
    w = SideEffectAction(table="deferrals", action="write", row_id="abc-123")
    assert w.action == "write"
    d = SideEffectAction(table="deferrals", action="delete", ids=["abc", "def"])
    assert d.action == "delete"
    assert d.ids == ["abc", "def"]


def test_all_envelope_types_accepted():
    """TC-ENV-011 — All four valid envelope types are accepted."""
    for t in ("orchestrate", "answerer", "apply_to_each", "state_op"):
        task = WrappedTask(**_make_wrapped_task(type=t))
        assert task.type == t


def test_inbound_with_scratch_actions():
    """TC-ENV-012 — InboundEnvelope with scratch_writes and scratch_cleanup."""
    data = _make_inbound_envelope(
        scratch_writes=[{"table": "deferrals", "action": "write", "row_id": "r1"}],
        scratch_cleanup=[{"table": "deferrals", "action": "delete", "ids": ["r1"]}],
    )
    env = InboundEnvelope(**data)
    assert len(env.scratch_writes) == 1
    assert env.scratch_writes[0].table == "deferrals"
    assert len(env.scratch_cleanup) == 1
    assert env.scratch_cleanup[0].action == "delete"
