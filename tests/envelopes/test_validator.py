"""Tests for src/envelopes/validator.py.

TC-ENV-020 — validate_outbound passes for valid WrappedTask
TC-ENV-021 — validate_outbound raises BRT-ENV-003 on depth violation
TC-ENV-022 — validate_outbound raises BRT-ENV-004 on empty instruction
TC-ENV-023 — validate_inbound passes for resolved envelope matching schema
TC-ENV-024 — validate_inbound passes for failed envelope with failure_reason
TC-ENV-025 — BRT-ENV-012: failed envelope missing failure_reason raises
TC-ENV-026 — BRT-ENV-011: resolved envelope missing required output key raises
TC-ENV-027 — validate_inbound passes when output has extra keys (not an error)
TC-ENV-028 — validate_inbound on failed envelope with empty output passes
"""
import uuid

import pytest
from pydantic import ValidationError

from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.envelopes.validator import validate_inbound, validate_outbound


def _task(**kwargs) -> WrappedTask:
    base = {
        "envelope_id": str(uuid.uuid4()),
        "run_id": str(uuid.uuid4()),
        "subtask_id": str(uuid.uuid4()),
        "type": "answerer",
        "current_depth": 1,
        "max_depth": 5,
        "level_path": "1a",
        "instruction": "Classify this email.",
        "output_schema": {"bucket": "bug|feature|question"},
    }
    base.update(kwargs)
    return WrappedTask(**base)


def _env(**kwargs) -> InboundEnvelope:
    base = {
        "envelope_id": str(uuid.uuid4()),
        "subtask_id": str(uuid.uuid4()),
        "status": "resolved",
        "output": {"bucket": "bug"},
    }
    base.update(kwargs)
    return InboundEnvelope(**base)


def test_validate_outbound_passes_for_valid_task():
    """TC-ENV-020 — No error raised for a well-formed WrappedTask."""
    task = _task()
    validate_outbound(task)  # must not raise


def test_validate_outbound_depth_violation():
    """TC-ENV-021 — BRT-ENV-003 raised when current_depth > max_depth after construction bypass."""
    task = _task()
    # Bypass Pydantic by directly mutating the model (simulate runtime mutation)
    object.__setattr__(task, "current_depth", 10)
    object.__setattr__(task, "max_depth", 5)
    with pytest.raises(ValueError, match="BRT-ENV-003"):
        validate_outbound(task)


def test_validate_outbound_empty_instruction():
    """TC-ENV-022 — BRT-ENV-004 raised when instruction is whitespace after mutation."""
    task = _task()
    object.__setattr__(task, "instruction", "   ")
    with pytest.raises(ValueError, match="BRT-ENV-004"):
        validate_outbound(task)


def test_validate_inbound_resolved_passes():
    """TC-ENV-023 — No error for resolved envelope matching task.output_schema."""
    task = _task(output_schema={"bucket": "bug|feature|question"})
    env = _env(output={"bucket": "bug"})
    validate_inbound(env, task)  # must not raise


def test_validate_inbound_failed_with_reason_passes():
    """TC-ENV-024 — No error for failed envelope that has failure_reason."""
    task = _task()
    env = _env(status="failed", output={}, failure_reason="Model timed out.")
    validate_inbound(env, task)  # must not raise


def test_validate_inbound_failed_missing_reason_raises():
    """TC-ENV-025 — BRT-ENV-012: failed envelope without failure_reason raises."""
    task = _task()
    env = _env(status="failed", output={}, failure_reason=None)
    with pytest.raises(ValueError, match="BRT-ENV-012"):
        validate_inbound(env, task)


def test_validate_inbound_missing_output_key_raises():
    """TC-ENV-026 — BRT-ENV-011: output missing a required key raises."""
    task = _task(output_schema={"bucket": "bug|feature|question", "confidence": "float"})
    env = _env(output={"bucket": "bug"})  # missing 'confidence'
    with pytest.raises(ValueError, match="BRT-ENV-011"):
        validate_inbound(env, task)


def test_validate_inbound_extra_output_keys_ok():
    """TC-ENV-027 — Extra keys in output beyond schema are acceptable."""
    task = _task(output_schema={"bucket": "bug|feature|question"})
    env = _env(output={"bucket": "bug", "extra_key": "extra_value"})
    validate_inbound(env, task)  # must not raise


def test_validate_inbound_failed_with_empty_output_passes():
    """TC-ENV-028 — Failed envelope with empty output is valid (no schema check on failure)."""
    task = _task(output_schema={"bucket": "bug|feature|question"})
    env = _env(status="failed", output={}, failure_reason="Timeout.")
    validate_inbound(env, task)  # must not raise


def test_validate_inbound_empty_schema_passes():
    """TC-ENV-029 — Empty output_schema means no key check needed."""
    task = _task(output_schema={})
    env = _env(output={})
    validate_inbound(env, task)  # must not raise
