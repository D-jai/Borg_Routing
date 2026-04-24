"""Envelope contract enforcement — validate_outbound + validate_inbound.

Error codes:
- BRT-ENV-001..005: outbound violations (see schema.py)
- BRT-ENV-006: ToolRequest.tool empty (raised inside schema on construction)
- BRT-ENV-010: answerer returned a status not matching the schema (shouldn't
               happen if schema is correct, but guards against runtime bypasses)
- BRT-ENV-011: inbound output does not match the task's declared output_schema
- BRT-ENV-012: failure_reason missing when status=failed
- BRT-TOOL-000: tool_request references a tool not in TOOL_REGISTRY (BRT-WEB-01)
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
        ValueError: BRT-TOOL-000 if any tool_request names an unknown tool.
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
    # BRT-WEB-01 — catch hallucinated tool names before dispatch.
    if task.tool_requests:
        # Late import so the tool package can import validator types cleanly.
        from src.tools.registry import TOOL_REGISTRY  # noqa: PLC0415

        for req in task.tool_requests:
            if req.tool not in TOOL_REGISTRY:
                raise ValueError(
                    f"BRT-TOOL-000: tool_request references unknown tool "
                    f"'{req.tool}' for envelope {task.envelope_id}. "
                    f"Registered tools: {sorted(TOOL_REGISTRY)}"
                )


def validate_inbound(env: InboundEnvelope, task: WrappedTask) -> None:
    """Validate that an inbound envelope conforms to the outbound task contract.

    Supports two output_schema shapes:
    - Flat ``{field_name: type_hint}`` (all top-level keys are required).
    - JSON-Schema ``{"type": "object", "properties": {...}, "required": [...]}``
      (the field names come from ``properties``; ``required`` restricts to a
      subset if present, otherwise all ``properties`` keys are required).

    Raises:
        ValueError: BRT-ENV-012 if status=failed but failure_reason is missing.
        ValueError: BRT-ENV-011 if output is missing required keys.
    """
    if env.status == "failed":
        if not env.failure_reason or not env.failure_reason.strip():
            raise ValueError(
                f"BRT-ENV-012: status=failed but failure_reason is missing "
                f"for envelope {env.envelope_id}"
            )

    if env.status == "resolved" and task.output_schema:
        # BRT-VAL-01 — detect JSON-Schema-shaped output_schema and walk into
        # `properties` / honour `required`, instead of treating the meta-keys
        # (type/properties/required) as required field names. Flat shapes
        # (e.g. {"bucket": "bug|feature|question"}) still work unchanged.
        schema = task.output_schema
        if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
            props = schema["properties"]
            required = schema.get("required")
            expected = (
                list(required)
                if isinstance(required, list)
                else list(props.keys())
            )
        else:
            expected = list(schema.keys())

        missing = [k for k in expected if k not in env.output]
        if missing:
            raise ValueError(
                f"BRT-ENV-011: inbound output missing required keys "
                f"{missing} for envelope {env.envelope_id}. "
                f"Expected schema keys: {expected}"
            )
