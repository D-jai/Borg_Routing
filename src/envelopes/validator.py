"""Envelope contract enforcement — validate_outbound + validate_inbound.

Error codes:
- BRT-ENV-001..005: outbound violations (see schema.py)
- BRT-ENV-010: answerer returned a status not matching the schema (shouldn't
               happen if schema is correct, but guards against runtime bypasses)
- BRT-ENV-011: inbound output does not match the task's declared output_schema
- BRT-ENV-012: failure_reason missing when status=failed
"""
from src.envelopes.schema import InboundEnvelope, WrappedTask


def validate_outbound(task: WrappedTask) -> None:
    """Raise ValueError with BRT-ENV-00x codes if the outbound task is invalid.

    WrappedTask construction already enforces most invariants via Pydantic.
    This function provides an explicit call-site check for callers who want
    to re-validate a task that may have been mutated after construction.

    Raises:
        ValueError: BRT-ENV-003 if current_depth > max_depth.
        ValueError: BRT-ENV-004 if instruction is empty.
    """
    if task.current_depth > task.max_depth:
        raise ValueError(
            f"BRT-ENV-003: current_depth ({task.current_depth}) exceeds "
            f"max_depth ({task.max_depth}) for envelope {task.envelope_id}"
        )
    if not task.instruction or not task.instruction.strip():
        raise ValueError(
            f"BRT-ENV-004: instruction is empty for envelope {task.envelope_id}"
        )


def validate_inbound(env: InboundEnvelope, task: WrappedTask) -> None:
    """Validate that an inbound envelope conforms to the outbound task contract.

    Raises:
        ValueError: BRT-ENV-012 if status=failed but failure_reason is missing.
        ValueError: BRT-ENV-011 if output is missing required top-level keys
                    declared in task.output_schema.
    """
    if env.status == "failed":
        if not env.failure_reason or not env.failure_reason.strip():
            raise ValueError(
                f"BRT-ENV-012: status=failed but failure_reason is missing "
                f"for envelope {env.envelope_id}"
            )

    if env.status == "resolved" and task.output_schema:
        missing = [k for k in task.output_schema if k not in env.output]
        if missing:
            raise ValueError(
                f"BRT-ENV-011: inbound output missing required keys "
                f"{missing} for envelope {env.envelope_id}. "
                f"Expected schema keys: {list(task.output_schema.keys())}"
            )
