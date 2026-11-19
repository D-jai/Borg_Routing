"""Rows and outputs placed by code: compose markers (planner rulings 21 and 32e).

Designs: ``docs/design/compose_row_markers.md`` (ruling 21) and
``docs/design/compose_placement.md`` (ruling 32e). Compose names a finished
list-holding step with ``@@ROWS:<target>@@`` (a JSON array) or
``@@ROWS_TABLE:<target>@@`` (a Markdown table), and a step's long output with
``@@OUTPUT:<step id>@@``; the engine writes the stored rows or text in, so
compose never retypes them.

- ``list_steps`` finds the list-holding steps of a run and their rows; an
  item that is a document part is labelled by the document's name as the
  manifest showed it (``_item_label``).
- ``long_outputs`` finds the resolved steps whose output holds a string of
  ``LONG_CHARS`` or more.
- ``parse_target`` reads a marker's target: one step, steps joined with
  ``+``, and an optional spec after ``|`` choosing fields and names.
- ``select`` / ``join`` build the rows a spec asks for.
- ``block`` / ``pointer_lines`` build the code-side lines added to compose's
  user message.
- ``marker_problems`` finds markers that cannot be placed (BRT-ORC-032,
  BRT-ORC-037), and near-misses.
- ``place`` substitutes every valid marker, once (placed text is never
  scanned again), calling the guards on each rendered block.

No model call and no prompt file: ``src/prompts/`` is not touched.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Callable, Optional

STRICT = re.compile(r"@@\s*(ROWS_TABLE|ROWS|OUTPUT)\s*:\s*([^@\n]{1,600}?)\s*@@", re.IGNORECASE)
LOOSE = re.compile(r"@@\s*(row|output)", re.IGNORECASE)
# A diff hunk header ("@@ -1,2 +1,2 @@") is masked before LOOSE runs, so a
# context line such as "@@ -1,2 +1,2 @@ output = x" is not a hit.
_HUNK = re.compile(r"@@ -\d+(,\d+)? \+\d+(,\d+)? @@")
_HIT_MAX = 200
_ID_MAX = 200
_CAPTURE_MAX = 600
_NAME_MAX = 64
_ECHO_MAX = 40
_FORBIDDEN = set(",=|@[]{}\n")
LONG_CHARS = 2000

INTRO = (
    "The reader sees only your answer. They cannot see any step, step id or step result, "
    "so never refer them to one: everything they need must be in your answer, written by "
    "you or placed by the code.\n"
    "The code can place stored results for you, exactly as stored. Do not retype rows or "
    "long outputs yourself; naming a few items in your own text is fine."
)
ROWS_BULLET = (
    "- Rows: write @@ROWS:<step id>@@ where a step's rows belong (a JSON array, one row per "
    "line), or @@ROWS_TABLE:<step id>@@ for a Markdown table. Put the marker on its own line, "
    "outside any brackets you write: the code writes the brackets. To choose fields, their "
    "order and their names, add them after a bar: "
    "@@ROWS:<step id> | <new name>=<existing field>, <existing field>@@ "
    "(for example @@ROWS:s1 | file=item, label=bucket@@). To join steps on the item, list them "
    "with +: @@ROWS:<step id> + <step id> | ...@@; when both give a field, the later step's "
    "value is used. Use only the fields listed for those steps. An item with no result keeps "
    "its error and gets no other value."
)
OUTPUT_BULLET = (
    "- Whole output: write @@OUTPUT:<step id>@@ where a step's whole output belongs; the code "
    "writes it in word for word."
)
NO_RESULT_LINE = (
    "Items with no result (a step failed them and no other step gave them a value): {m}. "
    "Never give them a value: say they have no result."
)
NO_VALUE_LINE = (
    "Rows with no value (a step returned the item but left the field empty): {counts}. "
    "Leave those fields empty; do not supply a value yourself."
)
# Planner ruling 35d: if one of the step's own fields fits the asked name,
# compose places it under that name with a rows marker; otherwise it says the
# field was not produced. Compose decides the fit (it may look at the rows it
# is shown); the code lists each asked name beside the step's own field names
# (as the step line shows them, without ``error``) and pairs nothing itself.
# A step with no rows to place gets "not produced".
ASKED_HEAD = (
    "Asked for but not produced (the plan asked a step for these fields and the step does "
    "not return them under these names):"
)
ASKED_LINE = "- {step} (skill {skill}): {names}. The step's own fields: {own}"
ASKED_LINE_NO_ROWS = ("- {step} (skill {skill}): {names}. The step has no rows to place, "
                      "so these were not produced.")
ASKED_TAIL = (
    "For each of these names, choose from the step's own fields: where one of "
    "them holds what the name asks for, place it under that name with a rows marker, for "
    "example @@ROWS:{step} | {name}=<field>, ...@@. Where none does, leave the name out and "
    "tell the reader it was not produced. Do not supply values for these fields yourself."
)
ASKED_TAIL_NO_ROWS = ("Do not supply values for these fields yourself: tell the reader they "
                      "were not produced.")


def asked_tail(step: str, name: str) -> str:
    """The asked section's last line; the example names the first asked step
    that has rows and its first asked name."""
    return ASKED_TAIL.format(step=step, name=name)
# Planner reading (2) under ruling 35: the re_plan failed for good and the
# run composes from the steps finished so far.
STOPPED_EARLY = (
    "Planning stopped early: the planner's last reply could not be used, so only the steps "
    "finished so far are below. Tell the reader the answer may be incomplete; do not present "
    "it as complete."
)
ROWS_POINTER = (
    "Mode: compose. Your last reply was cut off at the length limit. Do not type out a "
    "step's rows or long output: put @@ROWS:<step id>@@ or @@OUTPUT:<step id>@@ where they "
    "belong and the code writes them in. "
    'Reply with {"response": "<the final answer>"} only.'
)
REPAIR_HEAD = "Mode: compose. Your last reply cannot be used as it is:"
REPAIR_TAIL = 'Fix these and reply with {"response": "<the final answer>"} only.'

# Marker problems (docs/design/compose_placement.md 2.4.6): exact sentences.
S_NO_STEP = "A marker names no step the code can place. Use only the step ids listed below."
S_OUTPUT_ON_LIST = ("@@OUTPUT:{id}@@ names a step that has rows, not a long output: "
                    "use @@ROWS:{id}@@ for it.")
S_ROWS_ON_OUTPUT = ("@@ROWS:{id}@@ names a step that has a long output, not rows: "
                    "use @@OUTPUT:{id}@@ for it.")
S_MISSPELLED = ("A marker is misspelled. Write it exactly as shown below, with two @ signs "
                "at each end.")
S_TOO_LONG = "A marker is longer than 600 characters; name fewer fields."
# Planner ruling 39b: the sentence names the pair's order, with the step's own
# fields (as the block lists them) as the choice.
S_UNKNOWN = ("The marker for {target} names fields that step does not have: {names}. "
             "Write each pair as <new name>=<field>, where the field is one of: {fields}. "
             "No step produced the others; do not give values for them.")
# Planner ruling 39 (narrower rule): a reversed pair whose written name is a
# value the field holds is a filter attempt, not a column name.
S_FILTER = ("The marker for {target} writes {name}={value}, and {value} is a value of {name}: "
            "a rows marker cannot filter. Place all rows, or name the few items in your own text.")
S_DUPLICATE = "The marker for {target} uses the name {name} twice; each name may appear once."
S_EMPTY = "The marker for {target} has a bar but no fields after it."
S_BRACKETS = ("The marker for {target} puts its fields in brackets; write them plainly after "
              "the bar: @@ROWS:{target} | name=field, field@@.")
S_UNREADABLE = ("The marker for {target} has a field entry the code cannot read ({entry}): "
                "use plain names with no , = | @ brackets or line break.")
# A join needs an item on every row: a stored-order list (a state_op list of
# plain values) has none (review round 1).
S_JOIN_STORED = ("The marker for {target} joins {id}, whose rows are in stored order with no "
                 "item to join on: use @@ROWS:{id}@@ for it on its own.")
# Not in the note's table (the builder's wording): an OUTPUT marker with no
# field on a step whose output holds two or more long fields.
S_OUTPUT_FIELD = "The marker for {target} must name one field after a bar: {fields}."

CODE_MARKER = "BRT-ORC-032"
CODE_SPEC = "BRT-ORC-037"

INPUT_ORDER = "input order"
STORED_ORDER = "stored order"


@dataclass
class ListStep:
    id: str
    rows: list
    order: str
    fields: list = field(default_factory=list)
    # The canonical identity of each row's item (the part id for documents),
    # in row order; None for a stored-order list of plain values.
    identities: list = field(default_factory=list)
    # "file name", "document id" or None: what the item label shows.
    item_kind: Optional[str] = None
    raw: bool = False
    # The step's declared extra fields (ruling 35, reading 2): counted under
    # "Rows with no value" even when no row holds one. Not added to ``fields``
    # (a field no row holds is not offered for placing).
    extra_fields: list = field(default_factory=list)


@dataclass
class LongOutput:
    id: str
    fields: dict                 # {field name or None (a bare string): text}
    raw: bool = False


@dataclass
class Target:
    kind: str                    # "rows" | "table" | "output"
    ids: list
    spec: Optional[list]         # [(name, field)] or None
    # Planner ruling 39a: the pairs read the other way round, as written
    # ([(written name, written field)]); empty when none was.
    reversed_pairs: list = field(default_factory=list)
    # Planner ruling 39c: True for a rows marker whose spec could not be used
    # and whose step's rows are placed whole (Resolver(plain_fallback=True)).
    plain: bool = False

    @property
    def key(self) -> str:
        return " + ".join(self.ids)


# --- rows -------------------------------------------------------------------

def _is_aggregate(out: object) -> bool:
    return isinstance(out, dict) and isinstance(out.get("results"), list) and (
        "success_count" in out or "failures" in out
    )


def _is_entry(value: object) -> bool:
    return isinstance(value, dict) and "index" in value and "output" in value


def _is_entry_item(value: object) -> bool:
    """An item that is itself an aggregate entry (a retry over a step's
    stored results, as in round-2 job D)."""
    return isinstance(value, dict) and "index" in value and "item" in value


def _is_part(lane, value: object) -> bool:
    return lane is not None and hasattr(lane, "is_part_id") and lane.is_part_id(value)


def _doc_label(lane, part_id: str) -> str:
    """The name the manifest showed for a document part: the document's name
    (``name#k`` for part k), or the part id under ``lane_manifest: ids``."""
    docs = getattr(lane, "docs", None)
    settings = getattr(lane, "settings", None)
    if docs is None or getattr(settings, "manifest", "names") == "ids":
        return part_id
    part = docs.part_index.get(part_id)
    if part is None:
        return part_id
    doc = docs.doc_index.get(part.doc_id)
    if doc is None:
        return part_id
    if part_id == doc.doc_id:
        return doc.name
    return f"{doc.name}#{part_id.split('#', 1)[1]}" if "#" in part_id else doc.name


def _identity(entry: dict, raw: bool, lane) -> object:
    """The item's canonical identity: the inner item value (the part id for
    documents), else ruling 21's id rule."""
    item = entry.get("item")
    if _is_entry_item(item):
        item = item.get("item")
    # A retry of failed items names its entries by the original step's index.
    index = entry.get("original_index", entry.get("index"))
    if _is_part(lane, item):
        return item
    if raw:
        return index
    if isinstance(item, (str, int, float)) and not isinstance(item, bool):
        return item
    if isinstance(item, dict):
        ident = item.get("id")
        if isinstance(ident, (str, int, float)) and not isinstance(ident, bool):
            return ident
    return index


def _item_label(entry: dict, raw: bool, lane) -> object:
    """What a row's ``item`` shows: a document part as the manifest named it,
    else the identity."""
    ident = _identity(entry, raw, lane)
    if _is_part(lane, ident):
        return _doc_label(lane, ident)
    return ident


# Kept for callers of ruling 21's name.
_item_id = _item_label


def _failure_code(entry: dict, from_model: bool) -> str:
    reason = entry.get("reason")
    if isinstance(reason, dict) and isinstance(reason.get("code"), str):
        return reason["code"]
    if from_model:
        return "BRT-ANS-010"
    from src.lane.context import raw_failure_code  # noqa: PLC0415

    return raw_failure_code(str(reason or ""), "BRT-ORC-004")


def _resolved_row(entry: dict, raw: bool, lane) -> dict:
    row: dict = {"item": _item_label(entry, raw, lane)}
    out = entry.get("output")
    if isinstance(out, dict):
        for key, value in out.items():
            row[f"output_{key}" if key in ("item", "error") else key] = value
    else:
        row["output"] = out
    return row


def _is_raw(env, lane) -> bool:
    return (
        lane is not None and getattr(lane, "keeps_raw_local", False)
        and getattr(env, "_data_scope", None) == "raw"
    )


def _entries(env, lane) -> Optional[list]:
    """``[(kind, entry)]`` in row order for a list-holding step, else None.
    kind is "ok", "fail" or "plain" (an element of a stored-order list)."""
    if env is None or getattr(env, "status", None) != "resolved":
        return None
    node_type = getattr(env, "_node_type", None)
    out = getattr(env, "output", None)
    raw = _is_raw(env, lane)
    if node_type == "apply_to_each" and _is_aggregate(out):
        entries = [("ok", e) for e in out.get("results") or [] if isinstance(e, dict)]
        entries += [("fail", e) for e in out.get("failures") or [] if isinstance(e, dict)]
        entries.sort(key=lambda pair: (pair[1].get("index") is None, pair[1].get("index") or 0))
        return entries
    if node_type == "state_op" and isinstance(out, dict) and isinstance(out.get("result"), list):
        values = out["result"]
        if values and all(_is_entry(v) for v in values):
            ordered = sorted(values, key=lambda e: (e.get("index") is None, e.get("index") or 0))
            return [("ok", e) for e in ordered]
        if raw:
            return None
        return [("plain", v) for v in values]
    return None


def rows_for(env, lane) -> Optional[list]:
    """The rows of one finished step, or None when it holds no list.

    apply_to_each: results and failures merged on ``index`` (input order). A
    state_op returning a list: aggregate entries sorted on ``index``; any
    other list as stored, except on a raw step in ``raw_local``/``all_local``
    (not offered). An item that is a document part shows the document's
    name (``_item_label``); any other raw item shows its index.
    """
    entries = _entries(env, lane)
    if entries is None:
        return None
    raw = _is_raw(env, lane)
    model_reasons = getattr(env, "_model_reason_indexes", None) or set()
    rows = []
    for kind, entry in entries:
        if kind == "ok":
            rows.append(_resolved_row(entry, raw, lane))
        elif kind == "fail":
            rows.append({"item": _item_label(entry, raw, lane),
                         "error": _failure_code(entry, entry.get("index") in model_reasons)})
        else:
            rows.append(entry)
    return rows


def _identities_for(env, lane) -> list:
    entries = _entries(env, lane) or []
    raw = _is_raw(env, lane)
    return [None if kind == "plain" else _identity(entry, raw, lane) for kind, entry in entries]


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
    return names or ["(values)"]


def markable(step_id: object) -> bool:
    text = str(step_id)
    return (
        bool(text) and text == text.strip() and len(text) <= _ID_MAX
        and "@" not in text and "\n" not in text
    )


def is_failed_row(row: object) -> bool:
    return isinstance(row, dict) and "error" in row and set(row) <= {"item", "error"}


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
        identities = _identities_for(env, lane)
        if len(identities) != len(rows):
            identities = [r.get("item") if isinstance(r, dict) else None for r in rows]
        kind = None
        if any(_is_part(lane, i) for i in identities):
            settings = getattr(lane, "settings", None)
            kind = "document id" if getattr(settings, "manifest", "names") == "ids" else "file name"
        steps[str(step_id)] = ListStep(
            id=str(step_id), rows=rows, order=order,
            fields=_fields(rows, keyed=order == INPUT_ORDER),
            identities=identities, item_kind=kind, raw=_is_raw(env, lane),
            extra_fields=[str(f) for f in (getattr(env, "_extra_fields", None) or [])
                          if order == INPUT_ORDER],
        )
    return steps


def long_outputs(results: dict, steps: Optional[dict] = None, lane=None) -> dict:
    """``{step id: LongOutput}`` for every markable resolved step (not a list
    step) whose output is a string of ``LONG_CHARS`` or more, or holds such a
    top-level string field. The text is the output as compose was shown it."""
    found: dict = {}
    for step_id, env in (results or {}).items():
        sid = str(step_id)
        if not markable(step_id) or (steps and sid in steps):
            continue
        if env is None or getattr(env, "status", None) != "resolved":
            continue
        if _is_raw(env, lane) and getattr(env, "lane_guarded", None) is not True:
            # Salvage's withhold rule: a raw output the guard did not pass is
            # never offered for placement.
            continue
        out = getattr(env, "output", None)
        if isinstance(out, str):
            fields = {None: out} if len(out) >= LONG_CHARS else {}
        elif isinstance(out, dict) and not _is_aggregate(out):
            fields = {k: v for k, v in out.items()
                      if isinstance(k, str) and isinstance(v, str) and len(v) >= LONG_CHARS}
        else:
            fields = {}
        if fields:
            found[sid] = LongOutput(id=sid, fields=fields, raw=_is_raw(env, lane))
    return found


# --- specs and joins --------------------------------------------------------------

def _cut(text: object) -> str:
    text = str(text)
    return text if len(text) <= _ECHO_MAX else text[:_ECHO_MAX]


def _resolve_ids(text: str, listed) -> Optional[list]:
    text = text.strip()
    if text in listed:
        return [text]
    parts = [p.strip() for p in text.split("+")]
    if len(parts) > 1 and all(p in listed for p in parts):
        return parts
    return None


def _step_fields(ids: list, steps: dict) -> list:
    names: list = []
    for sid in ids:
        for f in steps[sid].fields:
            if f not in names and f != "(values)":
                names.append(f)
    if "item" in names:
        names.remove("item")
        names.insert(0, "item")
    if "error" in names:
        names.remove("error")
        names.append("error")
    return names


def _parse_spec(text: str, target: str) -> tuple:
    """(entries, problem): entries ``[(name, field)]`` or a 037 sentence."""
    body = text.strip()
    if not body:
        return None, S_EMPTY.format(target=target)
    if body[0] in "[{":
        return None, S_BRACKETS.format(target=target)
    entries: list = []
    for raw in body.split(","):
        entry = raw.strip()
        if not entry:
            continue
        renamed = "=" in entry
        name, fld = entry.split("=", 1) if renamed else (entry, entry)
        name, fld = name.strip(), fld.strip()
        if fld.startswith("output."):
            # output.x means x (ruling 25a); only top-level keys are selectable.
            fld = fld[len("output."):]
            if not renamed:
                name = fld
        for part in (name, fld):
            if not part or len(part) > _NAME_MAX or any(c in _FORBIDDEN for c in part):
                return None, S_UNREADABLE.format(target=target, entry=_cut(entry))
        entries.append((name, fld))
    if not entries:
        return None, S_EMPTY.format(target=target)
    seen: set = set()
    for name, _ in entries:
        if name in seen:
            return None, S_DUPLICATE.format(target=target, name=_cut(name))
        seen.add(name)
    return entries, None


def _known_name(name: str, known: list) -> Optional[str]:
    """The field ``name`` names: itself when it is a field, else the one field
    equal to it with case ignored (``Topic`` for ``topic``), else None."""
    if name in known:
        return name
    hits = [k for k in known if k.lower() == name.lower()]
    return hits[0] if len(hits) == 1 else None


def _read_reversed_pairs(entries: list, known: list) -> tuple:
    """``(entries, reversed)`` (planner ruling 39a). A pair is read as written
    first (``name=field``). Only when its right side names no field of the
    target, and its left side does (case ignored, or ``item``), is it read the
    other way round: the column is the right side as written, the field the
    real field name. The write-up model was seen writing ``item=feedback`` and
    ``topic=Topic`` for "the item, headed feedback" (demo re-record,
    18-Nov-2026). A pair that fits both ways keeps the written reading; one
    that fits neither is left for BRT-ORC-037. ``reversed`` lists the pairs
    read the other way, as written."""
    out: list = []
    flipped: list = []
    for name, fld in entries:
        # A right side that names a field, case ignored, is never flipped
        # (review of f0aa4db): ``Item=Sentiment`` keeps its written order with
        # the real field name, and a bare ``Topic`` is not a reversed pair.
        straight = _known_name(fld, known)
        if straight is not None:
            out.append((name, straight))
            continue
        real = _known_name(name, known)
        if real is not None:
            flipped.append((name, fld))
            name, fld = fld, real
        out.append((name, fld))
    return out, flipped


def _norm_value(value: object) -> str:
    """A stored value as the filter rule compares it: ``str``, trimmed, case
    ignored (a boolean reads "true" / "false"; the job-3 demo rows hold the
    strings "yes" / "no", so no yes/no mapping is made)."""
    return str(value).strip().lower()


def _filter_pair(flipped: list, entries: list, ids: list, steps: dict) -> Optional[tuple]:
    """Planner ruling 39 (narrower rule): the first reversed pair whose right
    side as written equals, trimmed and case ignored, a value its field holds
    in the target's rows (for ``item``, the item labels), as
    ``(written name, written value)``; else None. Only the reversed reading is
    checked. Accepted oddity: a field whose values happen to include its own
    display name (a ``follow_up`` field holding the value "Follow-up") is
    refused when written ``follow_up=Follow-up``; the write-up must then name
    the column another way. (A name equal to the field with case ignored,
    ``status=Status``, reads straight and never reaches this check.)"""
    if not flipped:
        return None
    by_written = dict(zip([f for _, f in flipped], [n for n, _ in flipped]))
    for col, real in entries:
        if col not in by_written:
            continue
        held = set()
        for sid in ids:
            for row in steps[sid].rows:
                if isinstance(row, dict) and real in row and row[real] is not None:
                    held.add(_norm_value(row[real]))
        if _norm_value(col) in held:
            return by_written[col], col
    return None


def parse_target(keyword: str, capture: str, steps: dict, outputs: Optional[dict] = None) -> tuple:
    """``(Target, None)`` or ``(None, (code, sentence))`` for one strict marker.

    Resolution order (compose_placement.md 2.1): the whole trimmed capture is
    a listed id; else the first ``|`` whose left part resolves as a target
    (one id, or ids joined by ``+``), the rest being the spec; else the whole
    capture split on ``+``; else the marker is invalid (BRT-ORC-032)."""
    outputs = outputs or {}
    kind = {"ROWS": "rows", "ROWS_TABLE": "table", "OUTPUT": "output"}[keyword.upper()]
    listed = set(steps) | set(outputs)
    cap = capture.strip()
    ids: Optional[list] = None
    spec_text: Optional[str] = None
    if cap in listed:
        ids = [cap]
    else:
        for i, ch in enumerate(cap):
            if ch == "|":
                res = _resolve_ids(cap[:i], listed)
                if res:
                    ids, spec_text = res, cap[i + 1:]
                    break
        if ids is None and "+" in cap:
            ids = _resolve_ids(cap, listed)
    if ids is None:
        return None, (CODE_MARKER, S_NO_STEP)
    target = " + ".join(ids)
    if kind == "output":
        if len(ids) > 1:
            return None, (CODE_MARKER, S_NO_STEP)
        if ids[0] in steps:
            return None, (CODE_MARKER, S_OUTPUT_ON_LIST.format(id=ids[0]))
        long = outputs[ids[0]]
        names = [f for f in long.fields if f is not None]
        if spec_text is None:
            if len(long.fields) > 1:
                return None, (CODE_SPEC, S_OUTPUT_FIELD.format(target=target, fields=", ".join(names)))
            return Target(kind, ids, None), None
        entries, problem = _parse_spec(spec_text, target)
        if problem:
            return None, (CODE_SPEC, problem)
        unknown = [f for _, f in entries if f not in long.fields]
        if unknown or len(entries) != 1:
            return None, (CODE_SPEC, S_UNKNOWN.format(
                target=target, names=", ".join(_cut(f) for f in (unknown or [e[1] for e in entries[1:]])),
                fields=", ".join(names) or "(the whole output)"))
        return Target(kind, ids, entries), None
    for sid in ids:
        if sid in outputs:
            return None, (CODE_MARKER, S_ROWS_ON_OUTPUT.format(id=sid))
    if len(ids) > 1:
        for sid in ids:
            if steps[sid].order != INPUT_ORDER:
                return None, (CODE_SPEC, S_JOIN_STORED.format(target=target, id=sid))
    if spec_text is None:
        return Target(kind, ids, None), None
    entries, problem = _parse_spec(spec_text, target)
    if problem:
        return None, (CODE_SPEC, problem)
    known = _step_fields(ids, steps)
    known_set = set(known) | {"item", "error"}
    entries, flipped = _read_reversed_pairs(
        entries, known + [f for f in ("item", "error") if f not in known])
    filtered = _filter_pair(flipped, entries, ids, steps)
    if filtered:
        return None, (CODE_SPEC, S_FILTER.format(
            target=target, name=_cut(filtered[0]), value=_cut(filtered[1])))
    if len({n for n, _ in entries}) != len(entries):
        dup = next(n for i, (n, _) in enumerate(entries) if n in {m for m, _ in entries[:i]})
        return None, (CODE_SPEC, S_DUPLICATE.format(target=target, name=_cut(dup)))
    unknown = [f for _, f in entries if f not in known_set]
    if unknown:
        return None, (CODE_SPEC, S_UNKNOWN.format(
            target=target, names=", ".join(_cut(f) for f in dict.fromkeys(unknown)),
            fields=", ".join(known)))
    return Target(kind, ids, entries, flipped), None


def plain_target(keyword: str, capture: str, steps: dict, outputs: Optional[dict] = None
                 ) -> Optional[Target]:
    """Planner ruling 39c: the step's plain rows target (all its fields,
    file-name labels: ``@@ROWS:<target>@@``) for a rows or table marker whose
    target resolves and whose only problem is its spec (BRT-ORC-037), else
    None. A join that cannot be placed without a spec either, an OUTPUT
    marker and a marker with no usable target get None (salvage)."""
    if keyword.upper() == "OUTPUT":
        return None
    _, problem = parse_target(keyword, capture, steps, outputs)
    if problem is None or problem[0] != CODE_SPEC:
        return None
    listed = set(steps) | set(outputs or {})
    cap = capture.strip()
    ids = None
    for i, ch in enumerate(cap):
        if ch == "|":
            ids = _resolve_ids(cap[:i], listed)
            if ids:
                break
    if not ids:
        return None
    target, problem = parse_target(keyword, " + ".join(ids), steps, outputs)
    if problem is not None or target is None or target.spec is not None:
        return None
    target.plain = True
    return target


def select(step: ListStep, spec: list) -> tuple:
    """(rows, columns) of one step with chosen fields (compose_placement 2.1)."""
    err_name = next((n for n, f in spec if f == "error"), None)
    rows: list = []
    any_failed = False
    for row in step.rows:
        if not isinstance(row, dict):
            rows.append({n: None for n, _ in spec})
            continue
        if is_failed_row(row):
            any_failed = True
            out = {}
            for n, f in spec:
                if f == "item":
                    out[n] = row.get("item")
                elif f == "error":
                    out[n] = row.get("error")
            if err_name is None:
                out["error"] = row.get("error")
            rows.append(out)
        else:
            rows.append({n: (row.get("item") if f == "item" else row.get(f)) for n, f in spec})
    columns = [n for n, _ in spec] + (["error"] if err_name is None and any_failed else [])
    return rows, columns


def join(steps: list, spec: Optional[list]) -> tuple:
    """(rows, columns) of steps joined on the item's canonical identity
    (compose_placement 2.1, lead ruling 1a)."""
    if spec is None:
        names = _step_fields([s.id for s in steps], {s.id: s for s in steps})
        spec = [(f, f) for f in names if f != "error"]
    err_name = next((n for n, f in spec if f == "error"), None)
    order: list = []
    state: dict = {}                    # identity -> {step id: ("ok", row) | ("fail", code)}
    labels: dict = {}
    for step in steps:
        for ident, row in zip(step.identities, step.rows):
            key = ident if not isinstance(ident, (dict, list)) else json.dumps(ident, sort_keys=True)
            if key not in state:
                state[key] = {}
                order.append(key)
                labels[key] = row.get("item") if isinstance(row, dict) else ident
            if is_failed_row(row):
                state[key][step.id] = ("fail", row.get("error"))
            elif isinstance(row, dict):
                state[key][step.id] = ("ok", row)
    rows: list = []
    any_failed = False
    for key in order:
        st = state[key]
        resolved = [s for s in steps if st.get(s.id, ("", None))[0] == "ok"]
        if not resolved:
            any_failed = True
            code = next(st[s.id][1] for s in reversed(steps) if st.get(s.id, ("", None))[0] == "fail")
            out = {}
            for n, f in spec:
                if f == "item":
                    out[n] = labels[key]
                elif f == "error":
                    out[n] = code
            if err_name is None:
                out["error"] = code
            rows.append(out)
            continue
        out = {}
        for n, f in spec:
            if f == "item":
                out[n] = labels[key]
                continue
            if f == "error":
                out[n] = None
                continue
            having = [s for s in steps if f in s.fields]
            resolvers = [s for s in having if st.get(s.id, ("", None))[0] == "ok"]
            value = None
            found = False
            for s in reversed(resolvers):
                v = st[s.id][1].get(f)
                if v is not None:
                    value, found = v, True
                    break
            if not found and not resolvers:
                failers = [s for s in having if st.get(s.id, ("", None))[0] == "fail"]
                value = st[failers[-1].id][1] if failers else None
            out[n] = value
        rows.append(out)
    columns = [n for n, _ in spec] + (["error"] if err_name is None and any_failed else [])
    return rows, columns


def target_rows(target: Target, steps: dict) -> tuple:
    """(rows, columns) a rows or table marker places."""
    if len(target.ids) == 1:
        step = steps[target.ids[0]]
        if target.spec is None:
            return step.rows, list(step.fields) or ["value"]
        return select(step, target.spec)
    return join([steps[i] for i in target.ids], target.spec)


def target_text(target: Target, outputs: dict) -> str:
    long = outputs[target.ids[0]]
    if target.spec:
        return long.fields[target.spec[0][1]]
    return next(iter(long.fields.values()))


# --- the block ---------------------------------------------------------------------

def _failed_count(step: ListStep) -> int:
    return sum(1 for r in step.rows if is_failed_row(r))


def step_lines(steps: dict) -> str:
    lines = []
    for s in steps.values():
        fields = [f"item ({s.item_kind})" if f == "item" and s.item_kind else str(f)
                  for f in s.fields]
        lines.append(f"- {s.id}: {len(s.rows)} rows, {s.order}, {_failed_count(s)} failed; "
                     f"fields: {', '.join(fields)}")
    return "\n".join(lines)


def output_lines(outputs: dict) -> str:
    lines = []
    for o in outputs.values():
        several = len(o.fields) > 1
        for name, text in o.fields.items():
            n = f"{len(text):,} characters"
            if name is None:
                lines.append(f"- {o.id}: {n}")
            elif several:
                lines.append(f"- {o.id}: {n} (field {name}; write @@OUTPUT:{o.id} | {name}@@)")
            else:
                lines.append(f"- {o.id}: {n} (field {name})")
    return "\n".join(lines)


def pointer_lines(steps: dict, outputs: Optional[dict] = None) -> str:
    parts = []
    if steps:
        parts.append("Steps with rows:\n" + step_lines(steps))
    if outputs:
        parts.append("Steps with a long output:\n" + output_lines(outputs))
    return "\n".join(parts)


def block(steps: dict, outputs: Optional[dict] = None, no_result: int = 0,
          no_value: Optional[list] = None, asked: Optional[list] = None,
          stopped_early: bool = False) -> Optional[str]:
    """The lines added after compose's first user-message line, or None with
    no list step, no long output, no asked-but-not-produced entry and no
    early stop (every request then stays byte-identical). ``stopped_early``
    (reading (2) under ruling 35) adds ``STOPPED_EARLY`` as the first line;
    alone, it is the whole block."""
    outputs = outputs or {}
    asked = asked or []
    if not steps and not outputs and not asked:
        return STOPPED_EARLY if stopped_early else None
    lines = ([STOPPED_EARLY] if stopped_early else []) + [INTRO]
    if steps:
        lines.append(ROWS_BULLET)
    if outputs:
        lines.append(OUTPUT_BULLET)
    if steps:
        lines.append("Steps with rows:\n" + step_lines(steps))
    if outputs:
        lines.append("Steps with a long output:\n" + output_lines(outputs))
    if steps:
        lines.append(NO_RESULT_LINE.format(m=no_result))
        counts = [(f, m) for f, m in (no_value or []) if m > 0]
        if counts:
            lines.append(NO_VALUE_LINE.format(counts=", ".join(f"{f} {m}" for f, m in counts)))
    if asked:
        grouped: dict = {}
        keys: list = []
        for entry in asked:
            if not isinstance(entry, dict):
                continue
            if not is_output_field(entry):
                # BRT-ORC-036: a node or skill_args key nothing takes, not an
                # output field (review round 1): its own line, key wording.
                line = (f"The plan gave {entry.get('field')} to {entry.get('step')}, which "
                        "does not take it; it was not used.")
                if line not in keys:
                    keys.append(line)
                continue
            key = (str(entry.get("step")), str(entry.get("skill")))
            grouped.setdefault(key, [])
            fld = str(entry.get("field"))
            if fld not in grouped[key]:
                grouped[key].append(fld)
        if grouped:
            lines.append(ASKED_HEAD)
            example = None
            for (step, skill), flds in grouped.items():
                names = ", ".join(flds)
                listed = steps.get(step) if steps else None
                if listed is None:
                    lines.append(ASKED_LINE_NO_ROWS.format(step=step, skill=skill, names=names))
                    continue
                own = [f"item ({listed.item_kind})" if f == "item" and listed.item_kind else str(f)
                       for f in listed.fields if f != "error"]
                lines.append(ASKED_LINE.format(step=step, skill=skill, names=names,
                                               own=", ".join(own)))
                if example is None:
                    example = (step, flds[0])
            lines.append(asked_tail(*example) if example else ASKED_TAIL_NO_ROWS)
        lines.extend(keys)
    return "\n".join(lines)


CODE_NOT_RETURNED = "BRT-ORC-035"
CODE_NOT_TAKEN = "BRT-ORC-036"


def is_output_field(entry: dict) -> bool:
    """True for a BRT-ORC-035 entry (an output field the skill does not
    return); False for a BRT-ORC-036 entry (a key nothing takes). An entry
    with no code is read as 035 (the round-2 fixtures predate the code)."""
    return str(entry.get("code") or CODE_NOT_RETURNED) != CODE_NOT_TAKEN


def repair_message(problems: list) -> str:
    """The repair re-ask's own lines; the engine adds the block after them."""
    return "\n".join([REPAIR_HEAD] + [f"- {s}" for _, s in problems] + [REPAIR_TAIL])


# --- markers ----------------------------------------------------------------------

def _masked(text: str) -> str:
    return _HUNK.sub(lambda m: " " * len(m.group(0)), text)


def loose_positions(text: str) -> list:
    """LOOSE hits (hunk headers masked), skipping any hit that starts inside
    a STRICT match found at an earlier hit: the closing ``@@`` of
    ``@@ROWS:s@@ output above`` is not a hit of its own."""
    out: list = []
    end = -1
    for m in LOOSE.finditer(_masked(text)):
        if m.start() < end:
            continue
        out.append(m.start())
        strict = STRICT.match(text, m.start())
        if strict:
            end = strict.end()
    return out


def strict_matches(text: str) -> list:
    """STRICT matches that start at a LOOSE hit (hunk headers masked)."""
    out = []
    for pos in loose_positions(text):
        m = STRICT.match(text, pos)
        if m:
            out.append(m)
    return out


def _hit_text(text: str, pos: int) -> str:
    m = STRICT.match(text, pos)
    if m:
        return m.group(0)
    end = text.find("@@", pos + 2)
    line_end = text.find("\n", pos)
    stops = [x for x in (end + 2 if end >= 0 else -1, line_end) if x >= 0]
    stop = min(stops) if stops else len(text)
    return text[pos:min(stop, pos + _HIT_MAX)]


def quoted_hits(quoted: list) -> set:
    """The hit texts found in quoted strings (decoded string leaves of the
    operator instruction and the step outputs)."""
    found: set = set()
    for q in quoted or []:
        if not isinstance(q, str):
            continue
        for pos in loose_positions(q):
            found.add(_hit_text(q, pos))
    return found


def _over_long(text: str, pos: int, listed) -> Optional[tuple]:
    """A LOOSE hit whose text up to the next ``@@`` on its line is longer than
    600 characters: (code, sentence), else None."""
    line_end = text.find("\n", pos)
    line_end = len(text) if line_end < 0 else line_end
    end = text.find("@@", pos + 2, line_end)
    if end < 0:
        return None
    inner = text[pos + 2:end]
    if len(inner) <= _CAPTURE_MAX:
        return None
    after = inner.split(":", 1)[1] if ":" in inner else ""
    head = re.split(r"[|+]", after, maxsplit=1)[0].strip()
    return (CODE_SPEC if head in listed else CODE_MARKER, S_TOO_LONG)


def _text_problems(text: str, steps: dict, outputs: dict, quoted: set, placeable: bool) -> list:
    found = []
    listed = set(steps) | set(outputs)
    for pos in loose_positions(text):
        m = STRICT.match(text, pos)
        if m:
            # A whole marker is quoted data only when that exact marker was
            # quoted; a quoted fragment ("@@ROWS") must not exempt it.
            if m.group(0) in quoted:
                continue
            target, problem = parse_target(m.group(1), m.group(2), steps, outputs)
            if problem is None and placeable:
                continue
            found.append((m.group(0), problem or (CODE_MARKER, S_NO_STEP)))
            continue
        if any(qh and text.startswith(qh, pos) for qh in quoted):
            continue
        found.append((_hit_text(text, pos), _over_long(text, pos, listed) or (CODE_MARKER, S_MISSPELLED)))
    return found


def _walk_problems(value, steps: dict, outputs: dict, qhits: set) -> list:
    found: list = []

    def walk(v) -> None:
        if isinstance(v, str):
            found.extend(_text_problems(v, steps, outputs, qhits, placeable=True))
        elif isinstance(v, dict):
            for k, item in v.items():
                if isinstance(k, str):
                    found.extend(_text_problems(k, steps, outputs, qhits, placeable=False))
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(value)
    return found


def marker_problems(value, steps: dict, outputs: Optional[dict], quoted) -> list:
    """``[(code, sentence)]`` for every marker-like hit in an answer that
    ``place`` would not replace (BRT-ORC-032, BRT-ORC-037), each sentence
    once, in first-seen order. A hit in an object key is never placed."""
    qhits = quoted if isinstance(quoted, set) else quoted_hits(quoted)
    out: list = []
    for _, problem in _walk_problems(value, steps, outputs or {}, qhits):
        if problem not in out:
            out.append(problem)
    return out


def plain_fixable(value, steps: dict, outputs: Optional[dict], quoted) -> bool:
    """Planner ruling 39c: True when the reply has at least one marker problem
    and every one is a rows marker in the answer's text (never an object key)
    whose target resolves and whose only problem is its spec, so placing the
    step's plain rows (``plain_target``) leaves no marker behind."""
    qhits = quoted if isinstance(quoted, set) else quoted_hits(quoted)
    outputs = outputs or {}
    found = False
    ok = True

    def check_text(text: str, is_key: bool) -> None:
        nonlocal found, ok
        for hit, _ in _text_problems(text, steps, outputs, qhits, placeable=not is_key):
            found = True
            m = STRICT.fullmatch(hit)
            if is_key or m is None or plain_target(m.group(1), m.group(2), steps, outputs) is None:
                ok = False

    def walk(v) -> None:
        if isinstance(v, str):
            check_text(v, False)
        elif isinstance(v, dict):
            for k, item in v.items():
                if isinstance(k, str):
                    check_text(k, True)
                walk(item)
        elif isinstance(v, list):
            for item in v:
                walk(item)

    walk(value)
    return found and ok


def invalid_hits(value, steps: dict, quoted, outputs: Optional[dict] = None) -> list:
    """Every marker-like hit text in an answer ``value`` that ``place`` would
    not replace (ruling 21's reader; see ``marker_problems``)."""
    qhits = quoted if isinstance(quoted, set) else quoted_hits(quoted)
    return [hit for hit, _ in _walk_problems(value, steps, outputs or {}, qhits)]


# --- rendering ----------------------------------------------------------------------

def _json_lines(rows: list) -> str:
    if not rows:
        return "[]"
    return "[\n" + ",\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows) + "\n]"


def _compact(rows: list) -> str:
    return json.dumps(rows, ensure_ascii=False, default=str)


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


def table_of(rows: list, columns: list) -> str:
    cols = list(columns) or ["value"]
    lines = ["| " + " | ".join(_cell(c) for c in cols) + " |",
             "| " + " | ".join("---" for _ in cols) + " |"]
    for row in rows:
        if isinstance(row, dict):
            cells = [_cell(row.get(c)) for c in cols]
        else:
            cells = [_cell(row)] + [""] * (len(cols) - 1)
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines)


def _table(step: ListStep) -> str:
    return table_of(step.rows, step.fields)


_FENCE_OPEN = re.compile(r"(`{3,}|~{3,})(.*)$")
_FENCE_CLOSE = re.compile(r"(`{3,}|~{3,})[ \t]*$")


def _closes(line: str, fence: tuple) -> bool:
    """True when ``line`` closes ``fence``: the same character, a run at
    least as long, and nothing else on the line."""
    m = _FENCE_CLOSE.match(line.strip())
    return bool(m) and m.group(1)[0] == fence[0] and len(m.group(1)) >= fence[1]


def _fence_state(before: str) -> Optional[tuple]:
    """``(char, run length, opening line)`` when ``before`` ends inside a
    fence (backticks or tildes, three or more), else None."""
    opening: Optional[tuple] = None
    for line in before.split("\n")[:-1]:
        if opening is None:
            m = _FENCE_OPEN.match(line.lstrip())
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                opening = (m.group(1)[0], len(m.group(1)), line.strip())
        elif _closes(line, opening):
            opening = None
    return opening


def fence_region(before: str) -> tuple:
    """(start offset of the scan region, fence or None): the innermost open
    fence's first line, else the end of the last closed fence, else 0."""
    opening: Optional[tuple] = None
    start = 0
    offset = 0
    for line in before.split("\n")[:-1]:
        nxt = offset + len(line) + 1
        if opening is None:
            m = _FENCE_OPEN.match(line.lstrip())
            if m and not (m.group(1)[0] == "`" and "`" in m.group(2)):
                opening = (m.group(1)[0], len(m.group(1)), line.strip())
                start = nxt
        elif _closes(line, opening):
            opening = None
            start = nxt
        offset = nxt
    return start, opening


def _table_in_fence(b: str, a: str, at_start: bool, fence: tuple) -> tuple:
    """(text before, text after) a table placed outside the fence it sat in:
    the fence is closed before it and reopened after it, except that an
    opener right before the marker or a closer right after it is dropped, so
    no empty fence is left."""
    char, length, opener = fence
    if at_start:
        lines = b.split("\n")[:-1]
        # Blank lines (CRLF included) between the opener and the marker do
        # not stop the opener from being dropped.
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[-1].strip() == opener:
            head = "\n".join(lines[:-1])
        else:
            head = b.rstrip() + "\n" + char * length
    else:
        head = b + "\n" + char * length
    rest = a[1:] if a.startswith("\n") else a
    after = rest.split("\n")
    i = 0
    while i < len(after) and not after[i].strip():
        i += 1
    if i == len(after):
        tail = ""                                   # end of text closes the fence
    elif _closes(after[i], fence):
        tail = "\n".join(after[i + 1:])
    else:
        tail = opener + "\n" + rest
    head = head.rstrip("\n")
    tail = tail.lstrip("\n")
    return (head + "\n\n" if head else ""), ("\n\n" + tail if tail else "")


class Resolver:
    """Resolves strict markers to what they place; each target is built once."""

    def __init__(self, steps: dict, outputs: Optional[dict] = None,
                 plain_fallback: bool = False) -> None:
        self.steps = steps or {}
        self.outputs = outputs or {}
        self._cache: dict = {}
        self.sizes: dict = {}
        # Ruling 39c: a rows marker with an unusable spec on a good target
        # places the step's plain rows; ``plain`` holds each such marker.
        self.plain_fallback = plain_fallback
        self.plain: dict = {}
        # Ruling 39a: {marker text: pairs read the other way round, as written}.
        self.reversed: dict = {}

    def target(self, m) -> Optional[Target]:
        target, problem = parse_target(m.group(1), m.group(2), self.steps, self.outputs)
        if problem and self.plain_fallback:
            target = plain_target(m.group(1), m.group(2), self.steps, self.outputs)
            if target is not None:
                self.plain[m.group(0)] = target.key
            return target
        if target is not None and target.reversed_pairs:
            self.reversed[m.group(0)] = [f"{n}={f}" for n, f in target.reversed_pairs]
        return None if problem else target

    def rows(self, target: Target) -> tuple:
        key = ("rows", target.key, repr(target.spec))
        if key not in self._cache:
            self._cache[key] = target_rows(target, self.steps)
        rows, cols = self._cache[key]
        self.sizes[target.key] = len(rows)
        return rows, cols

    def text(self, target: Target) -> str:
        text = target_text(target, self.outputs)
        self.sizes[target.key] = len(text)
        return text


def _scan(text: str, pos: int):
    from src.orchestrator.compose_checks import scan  # noqa: PLC0415

    return scan(text, pos)


def _place_in_text(text: str, resolver: Resolver, placed: dict, guard, text_guard) -> str:
    """Every valid marker of ``text`` placed, left to right (one pass).

    Markers, bracket context and fences are read from the ORIGINAL reply,
    never from text already placed (review round 1), so a placed output
    holding a stray ``[`` or fence cannot change how a later marker is
    placed. After each placement ``cur`` ends with the original's tail, so an
    original position maps to ``position + len(cur) - len(text)``."""
    original = text
    cur = text
    pos = 0
    for m in strict_matches(original):
        target = resolver.target(m)
        if target is None:
            continue
        shift = len(cur) - len(original)
        s, e = m.start() + shift, m.end() + shift
        if s < pos or cur[s:e] != m.group(0):
            continue
        ctx = _scan(original, m.start())
        if ctx.depth > 0:
            cur, pos = _place_nested(cur, s, e, ctx, target, resolver, guard, text_guard,
                                     original, shift, pos)
            placed[target.key] = placed.get(target.key, 0) + 1
            continue
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
        fence = _fence_state(original[:s - shift])
        if target.kind == "output":
            body = resolver.text(target)
            if text_guard is not None:
                text_guard(body)
            if fence is not None:
                prefix, a = _table_in_fence(b, a, at_start, fence)
                new = prefix + body
            else:
                head = b.rstrip("\n")
                new = (head + "\n\n" if head else "") + body + ("\n\n" if a.strip("\n") else "")
                a = a.lstrip("\n")
        elif target.kind == "table":
            rows, cols = resolver.rows(target)
            body = table_of(rows, cols)
            if guard is not None:
                guard(rows, body)
            if fence is not None:
                prefix, a = _table_in_fence(b, a, at_start, fence)
                new = prefix + body
            else:
                head = b.rstrip("\n")
                new = (head + "\n\n" if head else "") + body + ("\n\n" if a.strip("\n") else "")
                a = a.lstrip("\n")
        else:
            rows, _ = resolver.rows(target)
            body = _json_lines(rows)
            if fence is not None:
                if at_start and at_end:
                    new = b + body
                else:
                    body = _compact(rows)
                    b, a, new = before, after, before + body
            else:
                body = "```json\n" + body + "\n```"
                new = b + ("" if at_start else "\n") + body + ("" if at_end else "\n")
            if guard is not None:
                guard(rows, body)
        cur = new + a
        placed[target.key] = placed.get(target.key, 0) + 1
        pos = len(new)
    return cur


def _place_nested(cur: str, s: int, e: int, ctx, target: Target, resolver: Resolver,
                  guard, text_guard, original: Optional[str] = None, shift: int = 0,
                  floor: int = 0) -> tuple:
    """A marker inside brackets compose wrote (compose_placement 2.2, 2.4.1):
    placed as JSON so the surrounding JSON stays valid. Returns (text, next
    scan position)."""
    if target.kind == "output":
        text = resolver.text(target)
        if text_guard is not None:
            text_guard(text)
        if ctx.in_string:
            body = json.dumps(text, ensure_ascii=False)[1:-1]
        else:
            body = json.dumps(text, ensure_ascii=False)
        return cur[:s] + body + cur[e:], s + len(body)
    rows, _ = resolver.rows(target)
    compact = _compact(rows)
    if ctx.in_string:
        if s > 0 and cur[s - 1] == '"' and e < len(cur) and cur[e] == '"':
            # The marker is the whole quoted value: the quotes are dropped.
            body = compact
            s, e = s - 1, e + 1
        else:
            body = json.dumps(compact, ensure_ascii=False)[1:-1]
    elif ctx.innermost == "[":
        from src.orchestrator.compose_checks import close_of  # noqa: PLC0415

        # ctx and the bracket positions are the original reply's (``src``).
        src = cur if original is None else original
        opener = ctx.stack[-1][1]
        ms, me = s - shift, e - shift
        close = close_of(src, opener)
        inner = (src[opener + 1:ms] + src[me:close if close is not None else len(src)])
        if close is not None and opener + shift >= floor and not inner.strip(" \t\r\n,"):
            # A list holding only the marker: compose's brackets are absorbed.
            body = compact if ctx.depth > 1 else _json_lines(rows)
            s, e = opener + shift, close + shift + 1
        else:
            body = compact
    else:
        body = compact
    if guard is not None:
        guard(rows, body)
    return cur[:s] + body + cur[e:], s + len(body)


def _whole_marker(value: str):
    whole = STRICT.fullmatch(value.strip())
    return whole


def _place_value(value, resolver: Resolver, placed: dict, guard, text_guard):
    if isinstance(value, str):
        return _place_in_text(value, resolver, placed, guard, text_guard)
    if isinstance(value, list):
        if len(value) == 1 and isinstance(value[0], str):
            whole = _whole_marker(value[0])
            target = resolver.target(whole) if whole else None
            if target is not None and target.kind == "rows":
                # In an object answer, a list whose only element is the
                # marker becomes the rows list.
                return _place_leaf(value[0], resolver, placed, guard, text_guard)
        return [_place_leaf(v, resolver, placed, guard, text_guard) for v in value]
    if isinstance(value, dict):
        return {k: _place_leaf(v, resolver, placed, guard, text_guard) for k, v in value.items()}
    return value


def _place_leaf(value, resolver: Resolver, placed: dict, guard, text_guard):
    """Inside an object or list answer: a leaf that is exactly a marker
    becomes the rows list (or the table text, or the output text); a marker
    inside a longer leaf is placed in compact form (an output as its text)."""
    if isinstance(value, (list, dict)):
        return _place_value(value, resolver, placed, guard, text_guard)
    if not isinstance(value, str):
        return value
    whole = _whole_marker(value)
    target = resolver.target(whole) if whole else None
    if target is not None:
        placed[target.key] = placed.get(target.key, 0) + 1
        if target.kind == "output":
            text = resolver.text(target)
            if text_guard is not None:
                text_guard(text)
            return text
        rows, cols = resolver.rows(target)
        if target.kind == "table":
            body = table_of(rows, cols)
            if guard is not None:
                guard(rows, body)
            return body
        if guard is not None:
            guard(rows, _compact(rows))
        return list(rows)
    out = []
    pos = 0
    for m in strict_matches(value):
        target = resolver.target(m)
        if target is None:
            continue
        if target.kind == "output":
            body = resolver.text(target)
            if text_guard is not None:
                text_guard(body)
        else:
            rows, _ = resolver.rows(target)
            # Inside a longer leaf both row forms are placed compact, on one line.
            body = _compact(rows)
            if guard is not None:
                guard(rows, body)
        placed[target.key] = placed.get(target.key, 0) + 1
        out.append(value[pos:m.start()])
        out.append(body)
        pos = m.end()
    out.append(value[pos:])
    return "".join(out)


def place(value, steps: dict, guard: Optional[Callable[[list, str], None]] = None,
          outputs: Optional[dict] = None, text_guard: Optional[Callable[[str], None]] = None,
          resolver: Optional[Resolver] = None):
    """``(value with every valid marker replaced, {target: times placed})``.

    One pass: placed text is never scanned again. ``guard(rows, block)`` runs
    on each rendered rows block and ``text_guard(text)`` on each placed
    output before it is placed; either may raise.
    """
    placed: dict = {}
    resolver = resolver or Resolver(steps, outputs)
    return _place_value(value, resolver, placed, guard, text_guard), placed
