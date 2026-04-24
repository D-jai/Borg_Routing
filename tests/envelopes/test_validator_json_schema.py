"""Tests for BRT-VAL-01 — validator handles JSON-Schema-shaped output_schema.

Before BRT-VAL-01, validate_inbound iterated the top-level keys of
``task.output_schema`` and required each to be present in ``env.output``.
For JSON-Schema-shaped schemas (``{"type": "object", "properties": {...}}``)
that meant the validator demanded keys ``type``/``properties``/``required``
in every answerer output — rejecting correct ``{field_a, field_b}`` results.

TC-VAL-001 — JSON-Schema with matching output passes (no missing-keys error).
TC-VAL-002 — JSON-Schema with ``required`` missing a field raises BRT-ENV-011
             listing the REAL missing field name (not the meta-keys).
"""
import uuid

import pytest

from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.envelopes.validator import validate_inbound


def _task(output_schema: dict) -> WrappedTask:
    return WrappedTask(
        envelope_id=str(uuid.uuid4()),
        run_id=str(uuid.uuid4()),
        subtask_id=str(uuid.uuid4()),
        type="answerer",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        instruction="Synthesise a recommendation.",
        output_schema=output_schema,
    )


def _env(output: dict) -> InboundEnvelope:
    return InboundEnvelope(
        envelope_id=str(uuid.uuid4()),
        subtask_id=str(uuid.uuid4()),
        status="resolved",
        output=output,
    )


def test_json_schema_shape_passes_flat_output():
    """TC-VAL-001 — JSON-Schema output_schema with matching flat output passes.

    Reproduces the case from Bug Report §8: the orchestrator emits
    {type, properties, required} and the answerer returns
    {recommendation, reason}. Previously BRT-ENV-011 with missing keys
    ['type', 'properties', 'required']; now it must pass.
    """
    schema = {
        "type": "object",
        "properties": {
            "recommendation": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["recommendation", "reason"],
    }
    task = _task(output_schema=schema)
    env = _env(output={"recommendation": "use X", "reason": "cheaper"})

    validate_inbound(env, task)  # must not raise


def test_json_schema_missing_required_raises_real_key():
    """TC-VAL-002 — JSON-Schema with required field missing raises BRT-ENV-011
    listing the real field name (not a meta-key)."""
    schema = {
        "type": "object",
        "properties": {
            "recommendation": {"type": "string"},
            "reason": {"type": "string"},
        },
        "required": ["recommendation", "reason"],
    }
    task = _task(output_schema=schema)
    env = _env(output={"recommendation": "use X"})  # missing 'reason'

    with pytest.raises(ValueError, match="BRT-ENV-011") as exc:
        validate_inbound(env, task)

    msg = str(exc.value)
    assert "'reason'" in msg, f"expected real field 'reason' in error, got: {msg}"
    # meta-keys must NOT appear as missing fields
    assert "'type'" not in msg
    assert "'properties'" not in msg
    assert "'required'" not in msg
