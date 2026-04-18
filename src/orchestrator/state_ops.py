"""Deterministic state operators — no model calls.

These operators work on scratch table data (and envelope outputs) to produce
derived datasets cheaply between model stages.

Operator dispatch: the WrappedTask.instruction contains the op name in
`dependency_input['op']`. Params are in `dependency_input['params']`.
Source data is the list under `dependency_input['items']`.

Error codes:
- BRT-SOP-001: unknown operator name
- BRT-SOP-002: missing required param for operator
- BRT-SOP-003: source items not found in dependency_input
- BRT-SOP-004: invalid filter expression
- BRT-SOP-005: group_by key not found in items
"""
import random
from typing import Optional
from uuid import UUID

from src.envelopes.schema import InboundEnvelope, WrappedTask


def _get_items(task: WrappedTask) -> list[dict]:
    """Extract items from dependency_input. Raises BRT-SOP-003 if missing."""
    items = task.dependency_input.get("items")
    if items is None:
        raise ValueError(
            f"BRT-SOP-003: dependency_input must contain 'items' list for "
            f"state_op envelope {task.envelope_id}"
        )
    if not isinstance(items, list):
        raise ValueError(
            f"BRT-SOP-003: 'items' must be a list, got {type(items).__name__}"
        )
    return items


async def op_filter(items: list[dict], params: dict) -> list[dict]:
    """Keep only items where params['key'] == params['value'] (or != if 'negate').

    Raises:
        ValueError: BRT-SOP-002 if 'key' or 'value' missing from params.
        ValueError: BRT-SOP-004 if filter expression is invalid.
    """
    key = params.get("key")
    if key is None:
        raise ValueError("BRT-SOP-002: op_filter requires 'key' in params")
    if "value" not in params:
        raise ValueError("BRT-SOP-002: op_filter requires 'value' in params")
    value = params["value"]
    negate = params.get("negate", False)

    try:
        if negate:
            return [item for item in items if item.get(key) != value]
        else:
            return [item for item in items if item.get(key) == value]
    except Exception as exc:
        raise ValueError(f"BRT-SOP-004: filter expression failed: {exc}") from exc


async def op_count(items: list[dict], params: dict) -> dict:
    """Count items, optionally grouped by a key.

    If params has 'group_by', returns a dict of {value: count}.
    Otherwise returns {'count': len(items)}.
    """
    group_key = params.get("group_by")
    if group_key:
        counts: dict = {}
        for item in items:
            val = str(item.get(group_key, "__missing__"))
            counts[val] = counts.get(val, 0) + 1
        return counts
    return {"count": len(items)}


async def op_group_by(items: list[dict], params: dict) -> dict[str, list[dict]]:
    """Group items by a key value.

    Raises:
        ValueError: BRT-SOP-002 if 'key' missing.
        ValueError: BRT-SOP-005 if key not found in any item.
    """
    key = params.get("key")
    if key is None:
        raise ValueError("BRT-SOP-002: op_group_by requires 'key' in params")

    groups: dict[str, list[dict]] = {}
    for item in items:
        if key not in item:
            raise ValueError(
                f"BRT-SOP-005: key '{key}' not found in item: {item}"
            )
        val = str(item[key])
        groups.setdefault(val, []).append(item)
    return groups


async def op_sample(items: list[dict], params: dict) -> list[dict]:
    """Return a random sample of n items.

    Raises:
        ValueError: BRT-SOP-002 if 'n' missing or invalid.
    """
    n = params.get("n")
    if n is None:
        raise ValueError("BRT-SOP-002: op_sample requires 'n' in params")
    try:
        n = int(n)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"BRT-SOP-002: op_sample 'n' must be an integer, got '{n}'") from exc

    seed = params.get("seed")
    rng = random.Random(seed)
    return rng.sample(items, min(n, len(items)))


_OP_HANDLERS = {
    "filter": op_filter,
    "count": op_count,
    "group_by": op_group_by,
    "sample": op_sample,
}


async def dispatch_state_op(
    task: WrappedTask,
    db_path: str = "data/borg_route.db",
) -> InboundEnvelope:
    """Route a state_op WrappedTask to the correct operator.

    The operator name is read from `task.dependency_input['op']`.
    Source items from `task.dependency_input['items']`.
    Operator params from `task.dependency_input['params']` (optional).

    Raises:
        ValueError: BRT-SOP-001..005 on operator errors.
    """
    op_name = task.dependency_input.get("op")
    if not op_name or op_name not in _OP_HANDLERS:
        raise ValueError(
            f"BRT-SOP-001: unknown operator '{op_name}'. "
            f"Must be one of {sorted(_OP_HANDLERS.keys())}"
        )

    items = _get_items(task)
    params = task.dependency_input.get("params", {})

    handler = _OP_HANDLERS[op_name]
    result = await handler(items, params)

    return InboundEnvelope(
        envelope_id=task.envelope_id,
        subtask_id=task.subtask_id,
        status="resolved",
        output={"result": result},
    )
