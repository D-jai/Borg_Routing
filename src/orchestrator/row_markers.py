"""Rows placed by code: compose row markers (planner ruling 21).

Design: ``docs/design/compose_row_markers.md``. Compose names a finished
list-holding step with ``@@ROWS:<step id>@@`` (a JSON array) or
``@@ROWS_TABLE:<step id>@@`` (a Markdown table); the engine writes that
step's stored rows in, so compose never retypes a long list.

- ``list_steps`` finds the list-holding steps of a run and their rows.
- ``block`` / ``step_lines`` build the code-side lines added to compose's
  user message (id, row count, order and field names only).
- ``invalid_hits`` finds markers that name no list step, and near-misses.
- ``place`` substitutes every valid marker, once (placed rows are never
  scanned again), calling ``guard`` on each rendered block.

No model call and no prompt file: ``src/prompts/`` is not touched.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

STRICT = re.compile(r"@@\s*(ROWS_TABLE|ROWS)\s*:\s*([^@\n]{1,200}?)\s*@@", re.IGNORECASE)
LOOSE = re.compile(r"@@\s*row", re.IGNORECASE)
_HIT_MAX = 200
_ID_MAX = 200

HEADER = (
    "List steps the code can write out for you: put @@ROWS:<step id>@@ in your answer "
    "where a step's rows belong (a JSON array, one row per line), or "
    "@@ROWS_TABLE:<step id>@@ for a Markdown table. Do not type those rows yourself."
)
INVALID_REASK = (
    "Mode: compose. Your last reply used a list marker that names no list step. "
    'Use only the step ids listed below. Reply with {"response": "<the final answer>"} only.'
)
ROWS_POINTER = (
    "Mode: compose. Your last reply was cut off at the length limit. Do not type out a "
    "step's rows: put @@ROWS:<step id>@@ where they belong and the code writes them in. "
    'Reply with {"response": "<the final answer>"} only.'
)

INPUT_ORDER = "input order"
STORED_ORDER = "stored order"


@dataclass
class ListStep:
    id: str
    rows: list
    order: str
    fields: list = field(default_factory=list)


# --- rows -------------------------------------------------------------------

def _is_aggregate(out: object) -> bool:
    return isinstance(out, dict) and isinstance(out.get("results"), list) and (
        "success_count" in out or "failures" in out
    )


def _is_entry(value: object) -> bool:
    return isinstance(value, dict) and "index" in value and "output" in value


def _item_id(entry: dict, raw: bool, lane) -> object:
    item = entry.get("item")
    index = entry.get("index")
    if raw:
        return item if lane.is_part_id(item) else index
    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
        return item
    if isinstance(item, dict):
        ident = item.get("id")
        if isinstance(ident, (str, int, float)) and not isinstance(ident, bool):
            return ident
    return index


def _failure_code(entry: dict) -> str:
    reason = entry.get("reason")
    if isinstance(reason, dict) and isinstance(reason.get("code"), str):
        return reason["code"]
    if entry.get("_from_model"):
        return "BRT-ANS-010"
    from src.lane.context import raw_failure_code  # noqa: PLC0415

    return raw_failure_code(str(reason or ""), "BRT-ORC-004")


def _resolved_row(entry: dict, raw: bool, lane) -> dict:
    row: dict = {"item": _item_id(entry, raw, lane)}
    out = entry.get("output")
    if isinstance(out, dict):
        for key, value in out.items():
            row[f"output_{key}" if key in ("item", "error") else key] = value
    else:
        row["output"] = out
    return row


def rows_for(env, lane) -> Optional[list]:
    """The rows of one finished step, or None when it holds no list.

    apply_to_each: results and failures merged on ``index`` (input order). A
    state_op returning a list: aggregate entries sorted on ``index``; any
    other list as stored, except on a raw step in ``raw_local``/``all_local``
    (not offered). A raw item shows its document part id, else its index.
    """
    if env is None or getattr(env, "status", None) != "resolved":
        return None
    node_type = getattr(env, "_node_type", None)
    out = getattr(env, "output", None)
    raw = (
        lane is not None and getattr(lane, "keeps_raw_local", False)
        and getattr(env, "_data_scope", None) == "raw"
    )
    if node_type == "apply_to_each" and _is_aggregate(out):
        entries = [("ok", e) for e in out.get("results") or [] if isinstance(e, dict)]
        entries += [("fail", e) for e in out.get("failures") or [] if isinstance(e, dict)]
        entries.sort(key=lambda pair: (pair[1].get("index") is None, pair[1].get("index") or 0))
        rows = []
        for kind, entry in entries:
            if kind == "ok":
                rows.append(_resolved_row(entry, raw, lane))
            else:
                rows.append({"item": _item_id(entry, raw, lane), "error": _failure_code(entry)})
        return rows
    if node_type == "state_op" and isinstance(out, dict) and isinstance(out.get("result"), list):
        values = out["result"]
        if values and all(_is_entry(v) for v in values):
            ordered = sorted(values, key=lambda e: (e.get("index") is None, e.get("index") or 0))
            return [_resolved_row(e, raw, lane) for e in ordered]
        if raw:
            return None
        return list(values)
    return None


def _order_of(env) -> str:
    out = getattr(env, "output", None) or {}
    if getattr(env, "_node_type", None) == "apply_to_each":
        return INPUT_ORDER
    values = out.get("result") if isinstance(out, dict) else None
    if isinstance(values, list) and values and all(_is_entry(v) for v in values):
        return INPUT_ORDER
    return STORED_ORDER


def _fields(rows: list, keyed: bool) -> list:
    names: list = []
    has_error = False
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in row:
            if keyed and key == "error":
                has_error = True
            elif key not in names and not (keyed and key == "item"):
                names.append(key)
    if keyed:
        return ["item"] + names + (["error"] if has_error else [])
    return names


def markable(step_id: object) -> bool:
    text = str(step_id)
    return (
        bool(text) and text == text.strip() and len(text) <= _ID_MAX
        and "@" not in text and "\n" not in text
    )


def list_steps(results: dict, lane) -> dict:
    """``{step id: ListStep}`` for every markable list-holding step, in run order."""
    steps: dict = {}
    for step_id, env in (results or {}).items():
        if not markable(step_id):
            continue
        rows = rows_for(env, lane)
        if rows is None:
            continue
        order = _order_of(env)
        steps[str(step_id)] = ListStep(
            id=str(step_id), rows=rows, order=order,
            fields=_fields(rows, keyed=order == INPUT_ORDER),
        )
    return steps


def step_lines(steps: dict) -> str:
    return "\n".join(
        f"- {s.id}: {len(s.rows)} rows, {s.order}; fields: {', '.join(map(str, s.fields))}"
        for s in steps.values()
    )


def block(steps: dict) -> Optional[str]:
    """The lines added to compose's user message, or None with no list step."""
    if not steps:
        return None
    return HEADER + "\n" + step_lines(steps)


# --- markers --------------------------------------------------------------------

def _hit_text(text: str, pos: int) -> str:
    m = STRICT.match(text, pos)
    if m:
        return m.group(0)
    end = text.find("@@", pos + 2)
    line_end = text.find("\n", pos)
    stops = [x for x in (end + 2 if end >= 0 else -1, line_end) if x >= 0]
    stop = min(stops) if stops else len(text)
    return text[pos:min(stop, pos + _HIT_MAX)]


def invalid_hits(text: str, steps: dict, quoted: list) -> list:
    """Every loose hit in ``text`` that is not a strict marker naming a list
    step, except a hit whose exact text appears in ``quoted`` (the operator
    instruction, the stage results): that is data, left as text."""
    bad = []
    for loose in LOOSE.finditer(text or ""):
        m = STRICT.match(text, loose.start())
        if m and m.group(2).strip() in steps:
            continue
        hit = _hit_text(text, loose.start())
        if any(hit and hit in (q or "") for q in quoted):
            continue
        bad.append(hit)
    return bad


def _json_lines(rows: list) -> str:
    if not rows:
        return "[]"
    return "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows) + "\n]"


def _cell(value: object) -> str:
    if isinstance(value, str):
        text = value
    elif value is None:
        text = ""
    elif isinstance(value, (int, float, bool)):
        text = json.dumps(value)
    else:
        text = json.dumps(value, ensure_ascii=False, default=str)
    return text.replace("\r\n", " ").replace("\n", " ").replace("\r", " ").replace("|", "\\|")


def _table(step: ListStep) -> str:
    cols = list(step.fields) or ["value"]
    lines = ["| " + " | ".join(_cell(c) for c in cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for row in step.rows:
        if isinstance(row, dict):
            cells = [_cell(row.get(c)) for c in cols]
        else:
            cells = [_cell(row)] + [""] * (len(cols) - 1)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _fence_state(before: str) -> Optional[str]:
    """The opening fence line when ``before`` ends inside a fence, else None."""
    opening = None
    for line in before.split("\n")[:-1]:
        if line.lstrip().startswith("```"):
            opening = None if opening is not None else line.strip()
    return opening


def _place_in_text(text: str, steps: dict, placed: dict, guard) -> str:
    cur = text
    pos = 0
    while True:
        m = STRICT.search(cur, pos)
        if not m:
            return cur
        step_id = m.group(2).strip()
        if step_id not in steps:
            pos = m.end()
            continue
        step = steps[step_id]
        table = m.group(1).upper() == "ROWS_TABLE"
        s, e = m.start(), m.end()
        k = 0
        while s - k - 1 >= 0 and cur[s - k - 1] == "`":
            k += 1
        j = 0
        while e + j < len(cur) and cur[e + j] == "`":
            j += 1
        if 0 < k == j < 3:
            s, e = s - k, e + k
        before, after = cur[:s], cur[e:]
        b, a = before.rstrip(" \t"), after.lstrip(" \t")
        at_start = b == "" or b.endswith("\n")
        at_end = a == "" or a.startswith("\n")
        fence = _fence_state(before)
        if table:
            body = _table(step)
            if fence is not None:
                new = (b + ("" if at_start else "\n") + "```\n\n" + body + "\n\n" + fence
                       + ("" if at_end else "\n"))
            else:
                head = b.rstrip("\n")
                new = (head + "\n\n" if head else "") + body + ("\n\n" if a.strip("\n") else "")
                a = a.lstrip("\n")
        else:
            body = _json_lines(step.rows)
            if fence is not None:
                if at_start and at_end:
                    new = b + body
                else:
                    body = json.dumps(step.rows, ensure_ascii=False, default=str)
                    b, a, new = before, after, before + body
            else:
                body = "```json\n" + body + "\n```"
                new = b + ("" if at_start else "\n") + body + ("" if at_end else "\n")
        if guard is not None:
            guard(step.rows, body)
        cur = new + a
        placed[step_id] = placed.get(step_id, 0) + 1
        pos = len(new)


def _place_value(value, steps: dict, placed: dict, guard):
    if isinstance(value, str):
        return _place_in_text(value, steps, placed, guard)
    if isinstance(value, list):
        return [_place_leaf(v, steps, placed, guard) for v in value]
    if isinstance(value, dict):
        return {k: _place_leaf(v, steps, placed, guard) for k, v in value.items()}
    return value


def _place_leaf(value, steps: dict, placed: dict, guard):
    """Inside an object or list answer: a leaf that is exactly a marker
    becomes the rows list (or the table text); a marker inside a longer leaf
    is placed in compact form."""
    if isinstance(value, (list, dict)):
        return _place_value(value, steps, placed, guard)
    if not isinstance(value, str):
        return value
    whole = STRICT.fullmatch(value.strip())
    if whole and whole.group(2).strip() in steps:
        step = steps[whole.group(2).strip()]
        placed[step.id] = placed.get(step.id, 0) + 1
        if whole.group(1).upper() == "ROWS_TABLE":
            body = _table(step)
            if guard is not None:
                guard(step.rows, body)
            return body
        if guard is not None:
            guard(step.rows, json.dumps(step.rows, ensure_ascii=False, default=str))
        return list(step.rows)
    out = []
    pos = 0
    for m in STRICT.finditer(value):
        step_id = m.group(2).strip()
        if step_id not in steps:
            continue
        step = steps[step_id]
        if m.group(1).upper() == "ROWS_TABLE":
            body = _table(step)
        else:
            body = json.dumps(step.rows, ensure_ascii=False, default=str)
        if guard is not None:
            guard(step.rows, body)
        placed[step_id] = placed.get(step_id, 0) + 1
        out.append(value[pos:m.start()])
        out.append(body)
        pos = m.end()
    out.append(value[pos:])
    return "".join(out)


def place(value, steps: dict, guard: Optional[Callable[[list, str], None]] = None):
    """``(value with every valid marker replaced, {step id: times placed})``.

    One pass: placed rows are never scanned again. ``guard(rows, block)``
    runs on each rendered block before it is placed and may raise.
    """
    placed: dict = {}
    return _place_value(value, steps, placed, guard), placed
