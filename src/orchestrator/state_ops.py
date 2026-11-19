"""Deterministic state operators — no model calls.

These operators work on the rows an earlier step produced to give derived
datasets cheaply between model stages.

Operator dispatch: the op name is in ``dependency_input['op']``, its params
in ``dependency_input['params']``. The rows are the list under
``dependency_input['items']``, or, when the source step was a ``group_by``
without ``count``, the groups of rows under ``dependency_input['groups']``.

Planner ruling 25 — the engine runs what the planner prompt teaches:

- One field lookup for all four ops (``field_value``): the row's ``output``
  first, then the row itself; ``"output.x"`` means the same as ``"x"``. An
  apply_to_each row is ``{index, item, output: {...}}``, so its fields are
  found. ``filter`` and ``sample`` return whole rows, in that shape.
- ``key`` and ``group_by`` name the grouping field in every op that takes
  one (no other aliases).
- ``group_by`` with ``count: true`` gives counts per group; without it,
  rows per group.
- ``sample`` over a grouped source, or with a grouping field, takes ``n``
  from each group and returns one flat list of rows.
- An unknown op, a missing param or a param the op does not take fails, and
  the message names the op's params in its first 200 characters (what
  re_plan is shown, ruling 3). An unknown param is never silently ignored.

Error codes:
- BRT-SOP-001: unknown operator name
- BRT-SOP-002: missing or invalid required param
- BRT-SOP-003: the source gave no rows
- BRT-SOP-004: invalid filter expression
- BRT-SOP-005: grouping field not found in a row
- BRT-SOP-006: a param the op does not take
"""
import random
from typing import Any, Optional

from src.envelopes.schema import InboundEnvelope, WrappedTask

# Each op's params, in the order the messages list them. ``key|group_by``
# is one param written either way.
OP_PARAMS: dict[str, tuple[str, ...]] = {
    "filter": ("key", "value", "negate"),
    "count": ("key|group_by",),
    "group_by": ("key|group_by", "count"),
    "sample": ("n", "key|group_by", "seed"),
}
_ACCEPTED = {
    op: {name for spec in specs for name in spec.split("|")}
    for op, specs in OP_PARAMS.items()
}
_MISSING = object()


def _params_text(op: str) -> str:
    return ", ".join(OP_PARAMS[op])


def _all_ops_text() -> str:
    return " ".join(f"{op}({_params_text(op)})" for op in OP_PARAMS)


def field_value(row: Any, name: object) -> Any:
    """A row's field: its ``output`` first, then the row itself.

    ``"output.x"`` means the same as ``"x"``. Returns ``_MISSING`` when
    neither holds the field.
    """
    text = str(name)
    if text.startswith("output."):
        text = text[len("output."):]
    if not isinstance(row, dict):
        return _MISSING
    out = row.get("output")
    if isinstance(out, dict) and text in out:
        return out[text]
    if text in row:
        return row[text]
    return _MISSING


def _group_field(op: str, params: dict) -> Optional[str]:
    """The grouping field, written as ``key`` or ``group_by``; None if absent."""
    # An empty field name is a missing param (review of ac1c932).
    key, group_by = (None if params.get(n) == "" else params.get(n) for n in ("key", "group_by"))
    if key is not None and group_by is not None and str(key) != str(group_by):
        raise ValueError(
            f"BRT-SOP-002: {op} got key '{str(key)[:30]}' and group_by "
            f"'{str(group_by)[:30]}'; give one field. Its params: {_params_text(op)}"
        )
    field = key if key is not None else group_by
    return None if field is None else str(field)


def _row_label(row: Any, position: int) -> str:
    index = row.get("index") if isinstance(row, dict) else None
    return f"index {index}" if index is not None else f"position {position}"


def _groups_by_field(items: list, field: str) -> dict[str, list]:
    """Rows grouped on ``field`` in first-seen order; BRT-SOP-005 on a row without it."""
    groups: dict[str, list] = {}
    for position, item in enumerate(items):
        value = field_value(item, field)
        if value is _MISSING:
            raise ValueError(
                f"BRT-SOP-005: field '{field[:40]}' not found in the row at "
                f"{_row_label(item, position)} (looked in its output, then the row)"
            )
        groups.setdefault(str(value), []).append(item)
    return groups


def _flatten(groups: dict) -> list:
    return [row for rows in groups.values() for row in rows]


async def op_filter(items: list[dict], params: dict) -> list[dict]:
    """Keep the whole rows whose field ``key`` equals ``value`` (or not, with ``negate``).

    Raises:
        ValueError: BRT-SOP-002 if 'key' or 'value' missing from params.
        ValueError: BRT-SOP-004 if filter expression is invalid.
    """
    key = params.get("key")
    if key in (None, "") or "value" not in params:
        missing = "key" if key in (None, "") else "value"
        raise ValueError(
            f"BRT-SOP-002: filter requires '{missing}'. Its params: {_params_text('filter')}"
        )
    value = params["value"]
    negate = bool(params.get("negate", False))

    try:
        return [
            item for item in items
            if (field_value(item, key) == value) != negate
        ]
    except Exception as exc:
        raise ValueError(f"BRT-SOP-004: filter expression failed: {exc}") from exc


async def op_count(items: list[dict], params: dict) -> dict:
    """Count rows, or rows per value of a grouping field (``key`` or ``group_by``).

    A row without the field is counted under ``__missing__``.
    """
    field = _group_field("count", params)
    if field:
        counts: dict = {}
        for item in items:
            value = field_value(item, field)
            name = "__missing__" if value is _MISSING else str(value)
            counts[name] = counts.get(name, 0) + 1
        return counts
    return {"count": len(items)}


async def op_group_by(items: list[dict], params: dict) -> dict:
    """Group rows on a field: rows per group, or counts per group with ``count: true``.

    Raises:
        ValueError: BRT-SOP-002 if the field is missing.
        ValueError: BRT-SOP-005 if a row does not hold the field.
    """
    field = _group_field("group_by", params)
    if not field:
        raise ValueError(
            "BRT-SOP-002: group_by requires 'key' (or 'group_by'). "
            f"Its params: {_params_text('group_by')}"
        )
    groups = _groups_by_field(items, field)
    if params.get("count") is True:
        return {name: len(rows) for name, rows in groups.items()}
    return groups


async def op_sample(items: list[dict], params: dict,
                    groups: Optional[dict] = None) -> list[dict]:
    """``n`` rows at random; from each group when the source is grouped or a
    grouping field is given, as one flat list (group order, input order
    within a group).

    Raises:
        ValueError: BRT-SOP-002 if 'n' missing or invalid.
    """
    n = params.get("n")
    try:
        n = int(n) if n is not None and not isinstance(n, bool) else None
    except (ValueError, TypeError):
        n = None
    if n is None or n < 0:
        raise ValueError(
            f"BRT-SOP-002: sample requires 'n', a whole number of 0 or more. "
            f"Its params: {_params_text('sample')}"
        )
    rng = random.Random(params.get("seed"))
    field = _group_field("sample", params)
    if field:
        groups = _groups_by_field(items if groups is None else _flatten(groups), field)
    if groups is None:
        return rng.sample(items, min(n, len(items)))
    picked: list = []
    for rows in groups.values():
        rows = rows if isinstance(rows, list) else []
        chosen = sorted(rng.sample(range(len(rows)), min(n, len(rows))))
        picked.extend(rows[i] for i in chosen)
    return picked


_OP_HANDLERS = {
    "filter": op_filter,
    "count": op_count,
    "group_by": op_group_by,
    "sample": op_sample,
}


def _check_params(op: str, params: object) -> dict:
    """The params as a dict; BRT-SOP-006 for any param the op does not take."""
    if params is None:
        return {}
    if not isinstance(params, dict):
        raise ValueError(
            f"BRT-SOP-002: {op} params must be an object. Its params: {_params_text(op)}"
        )
    unknown = sorted(str(k) for k in params if str(k) not in _ACCEPTED[op])
    if unknown:
        names = ", ".join(unknown)
        if len(names) > 60:
            names = names[:57] + "..."
        raise ValueError(
            f"BRT-SOP-006: {op} does not take {names}. Its params: {_params_text(op)}"
        )
    for flag in ("count", "negate"):
        # A real boolean only: "true", "false" or 1 is never taken loosely.
        if flag in params and not isinstance(params[flag], bool):
            raise ValueError(
                f"BRT-SOP-002: {op} '{flag}' must be true or false. Its params: {_params_text(op)}"
            )
    return params


def _source_rows(task: WrappedTask) -> tuple[list, Optional[dict]]:
    """``(rows, groups)``: the rows to work on, and the groups when the source
    was grouped (rows is then every group's rows, in group order).

    Raises BRT-SOP-003 when the source gave neither.
    """
    dep = task.dependency_input
    groups = dep.get("groups")
    if isinstance(groups, dict) and all(isinstance(v, list) for v in groups.values()):
        return _flatten(groups), groups
    items = dep.get("items")
    if isinstance(items, list):
        return items, None
    source = dep.get("source_ref")
    where = f"source '{str(source)[:40]}'" if source else "this step"
    raise ValueError(
        f"BRT-SOP-003: {where} gave no rows to work on. A state_op reads a list of "
        "rows, or the groups of a group_by without count."
    )


async def dispatch_state_op(
    task: WrappedTask,
    db_path: str = "data/borg_route.db",
) -> InboundEnvelope:
    """Route a state_op WrappedTask to the correct operator.

    Raises:
        ValueError: BRT-SOP-001..006 on operator errors.
    """
    op_name = task.dependency_input.get("op")
    if not isinstance(op_name, str) or op_name not in _OP_HANDLERS:
        raise ValueError(
            f"BRT-SOP-001: unknown op '{str(op_name)[:20]}'. Ops and params: {_all_ops_text()}"
        )

    params = _check_params(op_name, task.dependency_input.get("params"))
    items, groups = _source_rows(task)

    if op_name == "sample":
        result = await op_sample(items, params, groups=groups)
    else:
        result = await _OP_HANDLERS[op_name](items, params)

    return InboundEnvelope(
        envelope_id=task.envelope_id,
        subtask_id=task.subtask_id,
        status="resolved",
        output={"result": result},
    )
