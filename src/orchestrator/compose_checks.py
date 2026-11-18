"""Checks after compose (planner ruling 32e; docs/design/compose_placement.md 2.3, 2.4).

Deterministic code, no model call. Run only when the code-side block was
sent, on the reply's chosen answer:

- ``nested_markers`` (C1, BRT-ORC-038): a rows marker that is an element of a
  list compose wrote holding other elements, or sits below table rows.
- ``unplaced_long_outputs`` (C3, BRT-ORC-040): a listed long output that is
  neither placed nor present while compose's own text points at a step.
- ``invented_values`` (C4, BRT-ORC-041): a value no step produced, for an item
  with no value for that field, or under an asked-but-not-produced field.

C2 (BRT-ORC-039) was dropped by the planner and is not built.

``ItemModel`` holds each list step item's identity, labels, stored values and
failures once per compose; it also gives the block's "no result" and "no
value" counts. ``scan`` gives a position's bracket depth and string state.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

from src.orchestrator import row_markers as rm

CODE_NESTED = "BRT-ORC-038"
CODE_UNPLACED = "BRT-ORC-040"
CODE_INVENTED = "BRT-ORC-041"
ORDER = ("BRT-ORC-032", "BRT-ORC-037", CODE_NESTED, CODE_UNPLACED, CODE_INVENTED)

S_NESTED = ("@@{kw}:{target}@@ sits inside a list or table you wrote that holds other rows. "
            "Put the marker on its own line, outside any brackets, and do not type its rows "
            "yourself.")
S_UNPLACED = ("Your answer points the reader to a step result they cannot see. Put "
              "@@OUTPUT:{id}@@ where that step's output belongs instead of pointing to it.")
S_CASE1 = ("You gave values to {k} items that no step gave a value for that field (for example "
           "{ids}). Do not give them values: in placed rows they show their error or stay empty, "
           "and in your own text say they have no result or no value. To show the stored values, "
           "place the rows instead of typing them: @@ROWS:{steps} | <name>=<field>, <field>@@.")
S_CASE2 = ("You gave values for {field} to {k} items (for example {ids}), but no step produced "
           "{field}. Leave the field out, or tell the reader it was not produced.")

_LINE_MAX = 160
_VOCAB_MAX = 40
_COVERAGE = 0.5
_GRAM = 8
_NUMBER = re.compile(r"^-?\d+(?:\.\d+)?$")
_CODE = re.compile(r"^BRT-[A-Z]+-\d{3}$")
_EMPTY_LIKE = {"", "-", "n/a", "none", "unknown", "no result", "no label", "no value",
               "missing", "error", "failed", "not produced"}
_BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)])\s+")
_SPLIT = re.compile(r"\||,|:|;|\t| - ")
_SEPARATOR_CELL = re.compile(r"^:?-{2,}:?$")


# --- scanning ------------------------------------------------------------------------

@dataclass
class ScanCtx:
    depth: int
    stack: list                 # [(bracket char, position)]
    in_string: bool
    region_start: int
    fence: Optional[tuple]

    @property
    def innermost(self) -> Optional[str]:
        return self.stack[-1][0] if self.stack else None


def scan(text: str, pos: int) -> ScanCtx:
    """Bracket depth and string state at ``pos``. The scan starts at the
    innermost open fence, the end of the last closed fence, or the start of
    the text; quotes count only while depth is above 0 (and a string never
    runs past a line end, so prose quotes cannot run away)."""
    start, fence = rm.fence_region(text[:pos])
    stack: list = []
    in_str = False
    esc = False
    for i in range(start, pos):
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"' or ch == "\n":
                in_str = False
        elif ch in "[{":
            stack.append((ch, i))
        elif ch in "]}":
            if stack:
                stack.pop()
        elif ch == '"' and stack:
            in_str = True
    return ScanCtx(len(stack), stack, in_str, start, fence)


def close_of(text: str, opener: int) -> Optional[int]:
    """The position of the bracket closing the one at ``opener``, or None
    when the text or the fence ends first."""
    depth = 0
    in_str = False
    esc = False
    i = opener
    while i < len(text):
        ch = text[i]
        if ch == "\n" and text.startswith(("```", "~~~"), i + 1):
            return None
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"' or ch == "\n":
                in_str = False
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0:
                return i
        elif ch == '"':
            in_str = True
        i += 1
    return None


# --- values -------------------------------------------------------------------------

def has_value(v: object) -> bool:
    """A value is anything other than null or an all-blank string."""
    return v is not None and not (isinstance(v, str) and not v.strip())


def norm(v: object):
    """A comparable form: numbers as numbers, strings trimmed and case-folded;
    None for null, blank, booleans, lists and objects."""
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        s = v.strip()
        if not s:
            return None
        if _NUMBER.match(s):
            return float(s)
        return s.casefold()
    return None


def empty_like(v: object) -> bool:
    if v is None:
        return True
    if isinstance(v, str):
        s = v.strip()
        return s.casefold() in _EMPTY_LIKE or bool(_CODE.match(s))
    return False


def _scalar(v: object) -> bool:
    return isinstance(v, (str, int, float)) and not isinstance(v, bool)


# --- the item model -----------------------------------------------------------------

@dataclass
class Item:
    key: object
    label: object
    tokens: set
    states: dict = field(default_factory=dict)     # step id -> ("ok", row) | ("fail", code)


def _hashable(ident: object) -> object:
    return json.dumps(ident, sort_keys=True) if isinstance(ident, (dict, list)) else ident


def _tokens(ident: object, label: object) -> set:
    found = set()
    for t in (ident, label):
        if isinstance(t, str):
            found.add(t)
            if "/" in t:
                found.add(t.rsplit("/", 1)[1])
    return {t.casefold() for t in found if len(t) >= 3 and not _NUMBER.match(t)}


class ItemModel:
    """Each list step item's identity, labels, stored values and failures
    (compose_placement 2.3). Built once per compose."""

    def __init__(self) -> None:
        self.items: dict = {}
        self.step_ids: list = []
        self.fields: dict = {}             # step id -> [field]
        self.values: dict = {}             # key -> {field: {normalised values}}
        self.own: dict = {}                # key -> {normalised values, any field}
        self.vocab: dict = {}              # field -> {normalised strings}
        self.paired: dict = {}             # key -> {normalised values an output pairs with it}
        self._token_map: dict = {}
        self._line_re: Optional[re.Pattern] = None

    @classmethod
    def build(cls, steps: dict, results: dict) -> "ItemModel":
        model = cls()
        for step in (steps or {}).values():
            if step.order != rm.INPUT_ORDER:
                continue
            model.step_ids.append(step.id)
            model.fields[step.id] = [f for f in step.fields if f not in ("item", "error")]
            for ident, row in zip(step.identities, step.rows):
                if not isinstance(row, dict):
                    continue
                key = _hashable(ident)
                item = model.items.get(key)
                if item is None:
                    item = Item(key, row.get("item"), _tokens(ident, row.get("item")))
                    model.items[key] = item
                    model.values[key] = {}
                    model.own[key] = set()
                    model.paired[key] = set()
                if rm.is_failed_row(row):
                    item.states[step.id] = ("fail", row.get("error"))
                    continue
                item.states[step.id] = ("ok", row)
                for f, v in row.items():
                    if f == "item" or not has_value(v):
                        continue
                    n = norm(v)
                    if n is None:
                        continue
                    model.values[key].setdefault(f, set()).add(n)
                    model.own[key].add(n)
                    if isinstance(v, str) and len(v.strip()) <= _VOCAB_MAX and isinstance(n, str):
                        model.vocab.setdefault(f, set()).add(n)
        counts: dict = {}
        for item in model.items.values():
            for t in item.tokens:
                counts[t] = counts.get(t, 0) + 1
        model._token_map = {t: item.key for item in model.items.values()
                            for t in item.tokens if counts[t] == 1}
        if model._token_map:
            alts = "|".join(re.escape(t) for t in sorted(model._token_map, key=len, reverse=True))
            model._line_re = re.compile(rf"(?<![\w#./-])(?:{alts})(?![\w#-])", re.IGNORECASE)
        list_ids = set(steps or {})
        for sid, env in (results or {}).items():
            if str(sid) in list_ids or getattr(env, "status", None) != "resolved":
                continue
            out = getattr(env, "output", None)
            if rm._is_aggregate(out):
                continue
            model._pair_outputs(out)
        return model

    def _pair_outputs(self, value: object) -> None:
        """The value-level source: a JSON object in a resolved (non-list)
        output holding one of an item's identity tokens and other values."""
        if isinstance(value, dict):
            scalars = [(k, v) for k, v in value.items() if _scalar(v)]
            for k, v in scalars:
                key = self.match(v) if isinstance(v, str) else None
                if key is None:
                    continue
                for k2, v2 in scalars:
                    if k2 != k and has_value(v2) and norm(v2) is not None:
                        self.paired[key].add(norm(v2))
            for v in value.values():
                if isinstance(v, (dict, list)):
                    self._pair_outputs(v)
        elif isinstance(value, list):
            for v in value:
                self._pair_outputs(v)

    def match(self, value: object) -> Optional[object]:
        if not isinstance(value, str):
            return None
        return self._token_map.get(value.strip().casefold())

    def line_tokens(self, line: str) -> list:
        if self._line_re is None:
            return []
        return [(m, self._token_map[m.group(0).casefold()]) for m in self._line_re.finditer(line)]

    def no_value(self, key: object, f: str) -> bool:
        return f not in self.values.get(key, {})

    def no_result(self) -> list:
        """Items a list step failed that no list step resolved and no
        resolved output pairs with a value (the block's count)."""
        out = []
        for key, item in self.items.items():
            states = item.states.values()
            if any(s[0] == "fail" for s in states) and not any(s[0] == "ok" for s in states) \
                    and not self.paired.get(key):
                out.append(key)
        return out

    def no_value_counts(self) -> list:
        """``[(field, count)]`` in step order: for each field some resolved
        row holds a value for, the items resolved by a step that has the
        field and given no value for it by any list step."""
        order: list = []
        for sid in self.step_ids:
            for f in self.fields[sid]:
                if f not in order:
                    order.append(f)
        counts = []
        for f in order:
            if not any(f in vals for vals in self.values.values()):
                continue
            having = {sid for sid in self.step_ids if f in self.fields[sid]}
            m = 0
            for key, item in self.items.items():
                resolved = any(sid in having and st[0] == "ok" for sid, st in item.states.items())
                if resolved and self.no_value(key, f):
                    m += 1
            counts.append((f, m))
        return counts

    def label(self, key: object) -> str:
        return str(self.items[key].label)

    def order_of(self, key: object) -> int:
        return list(self.items).index(key)


# --- compose's own text --------------------------------------------------------------

def _strip_markers(text: str, resolver: rm.Resolver) -> str:
    out = []
    pos = 0
    for m in rm.strict_matches(text):
        if resolver.target(m) is None:
            continue
        out.append(text[pos:m.start()])
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def own_text(value: object, resolver: rm.Resolver) -> object:
    """The reply value with every valid marker removed, kept in its
    structure (a string stays a string; an object's leaves are never joined)."""
    if isinstance(value, str):
        return _strip_markers(value, resolver)
    if isinstance(value, dict):
        return {k: own_text(v, resolver) for k, v in value.items()}
    if isinstance(value, list):
        return [own_text(v, resolver) for v in value]
    return value


def _strings(value: object) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for v in value.values() for s in _strings(v)]
    if isinstance(value, list):
        return [s for v in value for s in _strings(v)]
    return []


# --- C1 -------------------------------------------------------------------------------

def _kw(kind: str) -> str:
    return "ROWS_TABLE" if kind == "table" else "ROWS"


def nested_markers(value: object, resolver: rm.Resolver) -> list:
    """C1 (BRT-ORC-038): ``[(kind, target key)]`` for every valid rows marker
    that is an element of a list compose wrote holding other elements, or
    (table form) sits right below table rows."""
    found: list = []

    def text_hits(text: str) -> None:
        for m in rm.strict_matches(text):
            target = resolver.target(m)
            if target is None or target.kind == "output":
                continue
            if target.kind == "table":
                above = [ln for ln in text[:m.start()].split("\n")[:-1] if ln.strip()]
                if above and above[-1].lstrip().startswith("|"):
                    found.append((target.kind, target.key))
                    continue
            ctx = scan(text, m.start())
            if ctx.depth == 0 or ctx.in_string or ctx.innermost != "[":
                continue
            opener = ctx.stack[-1][1]
            close = close_of(text, opener)
            end = close if close is not None else len(text)
            inner = text[opener + 1:m.start()] + text[m.end():end]
            if inner.strip(" \t\r\n,"):
                found.append((target.kind, target.key))

    def walk(v) -> None:
        if isinstance(v, str):
            text_hits(v)
        elif isinstance(v, dict):
            for item in v.values():
                walk(item)
        elif isinstance(v, list):
            for item in v:
                if isinstance(item, str):
                    whole = STRICT_FULL(item)
                    target = resolver.target(whole) if whole else None
                    if target is not None:
                        if len(v) > 1 and target.kind != "output":
                            found.append((target.kind, target.key))
                        continue
                walk(item)

    walk(value)
    return list(dict.fromkeys(found))


def STRICT_FULL(text: str):  # noqa: N802
    return rm.STRICT.fullmatch(text.strip())


def placed_targets(value: object, resolver: rm.Resolver) -> list:
    """Every valid target in the reply, in order (each once)."""
    keys: list = []
    for text in _strings(value):
        for m in rm.strict_matches(text):
            target = resolver.target(m)
            if target is not None and target.key not in [t.key for t in keys]:
                keys.append(target)
    return keys


# --- C3 -------------------------------------------------------------------------------

def _grams(text: str) -> set:
    words = re.findall(r"\w+", (text or "").casefold())
    return {tuple(words[i:i + _GRAM]) for i in range(len(words) - _GRAM + 1)}


def _points_at_step(text: str, step_ids) -> bool:
    if re.search(r"(?i)\b(?:step|stage)[ _]results?\b", text):
        return True
    for sid in step_ids:
        sid = str(sid)
        if not sid:
            continue
        e = re.escape(sid)
        if re.search(rf"\(\s*{e}\s*\)", text) or f"`{sid}`" in text:
            return True
        if re.search(rf"(?i)\b(?:step|stage|node)\s+{e}(?![\w-])", text):
            return True
    return False


def unplaced_long_outputs(own: object, outputs: dict, placed_keys, step_ids) -> list:
    """C3 (BRT-ORC-040, kept narrow by the planner): the listed long outputs
    that no OUTPUT marker placed, that are not present (under half of their
    word 8-grams in compose's own text), while that text points at a step
    result or names a step id."""
    texts = "\n".join(_strings(own))
    if not outputs or not _points_at_step(texts, step_ids):
        return []
    have = _grams(texts)
    out = []
    for oid, long in outputs.items():
        if oid in placed_keys:
            continue
        grams = _grams("\n".join(long.fields.values()))
        if grams and len(grams & have) / len(grams) >= _COVERAGE:
            continue
        out.append(oid)
    return out


# --- C4 -------------------------------------------------------------------------------

@dataclass
class Pair:
    key: object
    value: object
    field: Optional[str]
    form: str                   # "a" | "b" | "c" | "d"


def _json_pairs(value: object, model: ItemModel, out: list) -> None:
    if isinstance(value, dict):
        scalars = [(k, v) for k, v in value.items() if _scalar(v)]
        ids = [(k, model.match(v)) for k, v in scalars if isinstance(v, str)]
        for kid, key in ids:
            if key is None:
                continue
            for k, v in scalars:
                if k != kid:
                    out.append(Pair(key, v, str(k), "a"))
        for k, v in value.items():
            if isinstance(v, list):
                for el in v:
                    key = model.match(el) if isinstance(el, str) else None
                    if key is not None:
                        out.append(Pair(key, k, None, "d"))
            if isinstance(v, (dict, list)):
                _json_pairs(v, model, out)
    elif isinstance(value, list):
        for v in value:
            _json_pairs(v, model, out)


def _cells(line: str) -> list:
    body = line.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|") and not body.endswith("\\|"):
        body = body[:-1]
    return [c.strip().replace("\\|", "|") for c in re.split(r"(?<!\\)\|", body)]


def _text_pairs(text: str, model: ItemModel, out: list) -> None:
    dec = json.JSONDecoder()
    spans = []
    i = 0
    while i < len(text):
        j = min([p for p in (text.find("[", i), text.find("{", i)) if p >= 0], default=-1)
        if j < 0:
            break
        try:
            val, end = dec.raw_decode(text, j)
        except ValueError:
            i = j + 1
            continue
        if isinstance(val, (dict, list)):
            _json_pairs(val, model, out)
            spans.append((j, end))
            i = end
        else:
            i = j + 1
    masked = list(text)
    for s, e in spans:
        for k in range(s, e):
            if masked[k] != "\n":
                masked[k] = " "
    lines = "".join(masked).split("\n")
    header: Optional[list] = None
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("|"):
            cells = _cells(stripped)
            if header is None:
                header = cells
                continue
            if all(_SEPARATOR_CELL.match(c) for c in cells if c):
                continue
            for idx, c in enumerate(cells):
                key = model.match(c)
                if key is None:
                    continue
                for idx2, c2 in enumerate(cells):
                    if idx2 != idx:
                        col = header[idx2] if idx2 < len(header) else None
                        out.append(Pair(key, c2, col, "b"))
            continue
        header = None
        if not stripped or len(stripped) > _LINE_MAX:
            continue
        body = _BULLET.sub("", line, count=1)
        for m, key in model.line_tokens(body):
            rest = body[:m.start()] + body[m.end():]
            cells = [c.strip() for c in _SPLIT.split(rest) if c.strip()]
            if len(cells) == 1:
                out.append(Pair(key, cells[0], None, "c"))


def pairs_of(own: object, model: ItemModel) -> list:
    """Identity-value pairs (a) to (d) in compose's own text (2.4)."""
    out: list = []
    if isinstance(own, (dict, list)):
        _json_pairs(own, model, out)
    for text in _strings(own):
        _text_pairs(text, model, out)
    return out


def invented_values(own: object, model: ItemModel, asked: list) -> tuple:
    """C4 (BRT-ORC-041): (case-1 item keys, {asked field: item keys}), each
    in input order."""
    asked_by_step: dict = {}
    for entry in asked or []:
        if isinstance(entry, dict):
            fld = str(entry.get("field"))
            asked_by_step.setdefault(str(entry.get("step")), {})[fld.casefold()] = fld
    case1: list = []
    case2: dict = {}
    for p in pairs_of(own, model):
        v = p.value
        if empty_like(v) or not has_value(v):
            continue
        n = norm(v)
        if n is None:
            continue
        if isinstance(v, str) and model.match(v) is not None:
            continue
        if n in model.own.get(p.key, set()) or n in model.paired.get(p.key, set()):
            continue
        if isinstance(n, str) and p.key not in case1:
            for f, voc in model.vocab.items():
                if n in voc and model.no_value(p.key, f):
                    case1.append(p.key)
                    break
        if p.field is not None and p.form in ("a", "b"):
            kc = p.field.strip().casefold()
            for sid in model.items[p.key].states:
                fld = asked_by_step.get(sid, {}).get(kc)
                if fld is not None:
                    keys = case2.setdefault(fld, [])
                    if p.key not in keys:
                        keys.append(p.key)
                    break
    case1.sort(key=model.order_of)
    for keys in case2.values():
        keys.sort(key=model.order_of)
    return case1, case2


def _examples(keys: list, model: ItemModel) -> str:
    return ", ".join(model.label(k) for k in keys[:3])


# --- all checks -----------------------------------------------------------------------

def check(value: object, steps: dict, outputs: dict, model: Optional[ItemModel], asked: list,
          quoted, step_ids, declined: bool = False) -> list:
    """``[(code, sentence)]`` for a reply's chosen answer (or a declined
    rationale), in the order 032, 037, 038, 040, 041. On a decline only the
    marker checks and C1 run (lead ruling 16)."""
    problems = list(rm.marker_problems(value, steps, outputs, quoted))
    resolver = rm.Resolver(steps, outputs)
    for kind, key in nested_markers(value, resolver):
        problems.append((CODE_NESTED, S_NESTED.format(kw=_kw(kind), target=key)))
    if not declined:
        own = own_text(value, resolver)
        placed = {t.key for t in placed_targets(value, resolver) if t.kind == "output"}
        for oid in unplaced_long_outputs(own, outputs, placed, step_ids):
            problems.append((CODE_UNPLACED, S_UNPLACED.format(id=oid)))
        if model is not None and model.items:
            case1, case2 = invented_values(own, model, asked)
            if case1:
                problems.append((CODE_INVENTED, S_CASE1.format(
                    k=len(case1), ids=_examples(case1, model), steps=" + ".join(model.step_ids))))
            for fld, keys in case2.items():
                problems.append((CODE_INVENTED, S_CASE2.format(
                    field=fld, k=len(keys), ids=_examples(keys, model))))
    unique: list = []
    for p in problems:
        if p not in unique:
            unique.append(p)
    return sorted(unique, key=lambda p: ORDER.index(p[0]) if p[0] in ORDER else len(ORDER))
