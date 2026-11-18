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


ONLY_IF_MARKER = "(only if"


def is_conditional_field(hint: object) -> bool:
    """True when a schema entry marks its field as conditional ("(only if ...)").

    ``hint`` is a string hint, or a JSON-Schema property dict whose
    ``description`` holds the marker. Such a field may be left out of an item's
    reply (or be null) when its requirement is only implied; an explicit
    ``required`` list wins (see ``validate_inbound``). The one shared rule for
    packed and unpacked items: both are checked by ``validate_inbound``.
    """
    if isinstance(hint, dict):
        hint = hint.get("description")
    return isinstance(hint, str) and ONLY_IF_MARKER in hint


DEFER_VALUE = "defer"


def is_deferred(output: object) -> bool:
    """True when an item's output is the worker's deferral: a top-level value
    of "defer" (``bucket: defer``, ``result_or_defer: defer``)."""
    return isinstance(output, dict) and any(
        isinstance(v, str) and v.strip().casefold() == DEFER_VALUE for v in output.values()
    )


def missing_extra_fields(env: InboundEnvelope, task: WrappedTask) -> list[str]:
    """The task's extra fields a resolved reply leaves absent or null
    (planner reading (2) under ruling 35). A deferred item needs none. An
    extra field marked "(only if" whose requirement is implied (ruling 32b)
    is never counted. Empty for a failed reply or a task with no extra fields.
    """
    if env.status != "resolved":
        return []
    return missing_extra_fields_in(env.output, task)


def missing_extra_fields_in(output: object, task: WrappedTask) -> list[str]:
    """``missing_extra_fields`` for a stored item output."""
    extra = list(task.extra_fields or [])
    if not extra or not isinstance(output, dict) or is_deferred(output):
        return []
    schema = task.output_schema if isinstance(task.output_schema, dict) else {}
    if isinstance(schema.get("properties"), dict):
        props = schema["properties"]
        explicit = isinstance(schema.get("required"), list)
    else:
        props, explicit = schema, False
    return [
        f for f in extra
        if output.get(f) is None and (explicit or not is_conditional_field(props.get(f)))
    ]


def validate_inbound(env: InboundEnvelope, task: WrappedTask) -> None:
    """Validate that an inbound envelope conforms to the outbound task contract.

    Supports two output_schema shapes:
    - Flat ``{field_name: type_hint}`` (all top-level keys are required).
    - JSON-Schema ``{"type": "object", "properties": {...}, "required": [...]}``
      (the field names come from ``properties``; ``required`` restricts to a
      subset if present, otherwise all ``properties`` keys are required).
    A field whose string hint (or property ``description``) holds "(only if"
    may be absent or null, but only where the requirement is implied (flat
    schema, or ``properties`` without a ``required`` list). A field named in an
    explicit ``required`` list stays required.

    Only presence is checked: a present value of any type passes.

    An extra field (``task.extra_fields``, planner reading (2) under ruling 35)
    is never required here: a reply without one passes, and the packed path
    retries it once alone (``missing_extra_fields``). The skill's own fields
    stay required as above.

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
            explicit = isinstance(required, list)
            expected = list(required) if explicit else list(props.keys())
        else:
            props = schema
            explicit = False
            expected = list(schema.keys())

        # Ruling 32b: a field whose hint holds "(only if" may be absent or null.
        # Ruling 35, reading (2): an extra field is retried, then kept.
        extra = set(task.extra_fields or [])
        missing = [
            k for k in expected
            if k not in env.output and k not in extra
            and (explicit or not is_conditional_field(props.get(k)))
        ]
        if missing:
            raise ValueError(
                f"BRT-ENV-011: inbound output missing required keys "
                f"{missing} for envelope {env.envelope_id}. "
                f"Expected schema keys: {expected}"
            )
