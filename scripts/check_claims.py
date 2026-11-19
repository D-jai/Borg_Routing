#!/usr/bin/env python
"""Check the claims table in BUILD.md section 7 against the documents.

Every claim a document makes carries a marker ``<!-- claim C01 -->``. Each
marker needs a row in the table, and each row needs a backing that exists.

Table row:  ``| C01 | claim text | backing |``

Backing notation (several backings in one cell are separated by ``;``):

    test:tests/path/test_file.py::test_name
    test:tests/path/test_file.py::TestClass::test_name
    result:bench/results/run/summary.json#a.b.0.c=42.5
    result:bench/results/run/A_1.json#quality.urgency=69.5%
    result:demo/runs/job.json#view.digest.floor=0.0309 round down
    text:bench/results/run/summary.md#B 120.3 70.1 3=70.1

A ``test:`` backing is checked by parsing the file with ``ast``: the function
must be a top-level def, or a method directly inside the named class (text in
strings, comments or nested helpers does not count); pytest is never run here. A ``result:`` backing names a
JSON file, a dotted path into it (list positions are numbers) and the value
expected there. The check reads the number written just before the marker on its line (the
last whole number before it; a token with a letter or ``-`` attached, such as
BRT-ORC-019, is not a number; commas, ``$`` and ``%`` are ignored) and requires
it to equal the value the row states. Every value backing of a row is compared.
The row's value is checked against the source first:

- ``=V`` alone: the source value equals V exactly;
- ``=V round``: the source value rounded half up to V's decimal places equals V;
- ``=V round down``: the source value cut to V's decimal places equals V (the UI's
  ``floorMoney`` shows an "at least" figure this way);
- ``V%`` with a source written without ``%``: the source is a fraction, so it is
  multiplied by 100 first (``0.695`` backs ``69.5%``).

A ``text:`` backing names any text file (a published report, a job file) and a piece
of text that must stand on one line of it. Both sides are compared after ``|``, ``*``
and backticks are turned into spaces and runs of spaces are made one, so a table row
can be quoted without its pipes. One number in that text must give V by the rules
above. The text may not hold ``;`` or ``|``.

``US$`` before a number is read as part of the number (``US$0.0309``).
Lines inside ``` fences are skipped.

Exit 0 when everything holds (warnings may print). Exit 1 on any error.

    python scripts/check_claims.py
    python scripts/check_claims.py --build X.md --doc A.md --doc B.html --root DIR
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*claim\s+(C\d+)\s*-->")
ANY_CLAIM_COMMENT = re.compile(r"<!--\s*claim\b", re.I)
ROW_START = re.compile(r"^\|\s*[Cc]")
ID_OK = re.compile(r"^C\d+$")
NUM_TOKEN = re.compile(r"[-+]?(?:US)?\$?\d[\d,]*(?:\.\d+)?%?")
VALUE_SPEC = re.compile(r"^(?P<value>\S+?)(?:\s+(?P<mode>round down|round))?$")


def default_docs(root: Path) -> list[Path]:
    docs = [root / "README.md", root / "docs" / "user-guide.md"]
    docs += sorted((root / "docs" / "presentation").glob("**/*.html"))
    return [d for d in docs if d.is_file()]


def parse_table(build_text: str) -> tuple[dict[str, dict], list[str]]:
    """Return ({id: {"claim", "backings"}}, errors) from section 7."""
    m = re.search(r"^##\s+7\.\s+Claims table\s*$", build_text, re.M)
    if not m:
        raise ValueError("BUILD.md has no '## 7. Claims table' section")
    rest = build_text[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    section = rest[: nxt.start()] if nxt else rest
    rows: dict[str, dict] = {}
    errors: list[str] = []
    for line in section.splitlines():
        line = line.strip()
        if not ROW_START.match(line):
            continue
        body = line.strip("|")
        cells = [c.strip() for c in body.split("|")]
        if len(cells) < 3:
            errors.append(f"table line has fewer than three cells (id, claim, backing): {line[:80]}")
            continue
        cid = cells[0]
        if not ID_OK.match(cid):
            what = "must be upper case" if re.match(r"^[Cc]\d+$", cid) else "is not a claim id like C01"
            errors.append(f"table id '{cid}' {what}")
            continue
        if cid in rows:
            errors.append(f"{cid}: duplicate id in the claims table")
            continue
        backings = [b.strip().strip("`").strip() for b in cells[-1].split(";") if b.strip()]
        rows[cid] = {"claim": " | ".join(cells[1:-1]), "backings": backings}
    return rows, errors


def find_markers(doc_paths: list[Path]) -> tuple[list[tuple[str, Path, int, str, int]], list[str]]:
    """Markers as (id, file, line no, line, column). Malformed ones are errors."""
    found, errors = [], []
    for p in doc_paths:
        in_fence = False
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("```"):
                # a line that opens and closes a fence by itself changes nothing
                if not (len(stripped) > 3 and stripped.endswith("```")):
                    in_fence = not in_fence
                continue
            if in_fence:
                continue
            strict = {m.start(): m for m in MARKER.finditer(line)}
            for m in ANY_CLAIM_COMMENT.finditer(line):
                if m.start() in strict:
                    found.append((strict[m.start()].group(1), p, n, line, m.start()))
                else:
                    errors.append(f"{p.name}:{n}: malformed claim marker (want <!-- claim C01 -->): "
                                  f"{line[m.start():m.start() + 40]!r}")
        if in_fence:
            errors.append(f"{p.name}: ends inside an open ``` fence")
    return found, errors


def _find_func(body: list[ast.stmt], name: str) -> bool:
    return any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name for n in body)


def check_test_backing(spec: str, root: Path) -> str | None:
    parts = spec.split("::")
    file_part, names = parts[0], parts[1:]
    path = root / file_part
    if not path.is_file():
        return f"test file not found: {file_part}"
    if not names:
        return f"no test name in '{spec}'"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return f"cannot parse {file_part}: {exc}"
    *classes, func = [re.sub(r"\[.*\]$", "", n) for n in names]
    body = tree.body
    for c in classes:
        cls = next((n for n in body if isinstance(n, ast.ClassDef) and n.name == c), None)
        if cls is None:
            return f"class {c} not found in {file_part}"
        body = cls.body
    if not _find_func(body, func):
        where = f"class {classes[-1]} in " if classes else ""
        return f"test {func} not found in {where}{file_part}"
    return None


def json_lookup(data, dotted: str):
    cur = data
    for key in dotted.split("."):
        cur = cur[int(key)] if isinstance(cur, list) else cur[key]
    return cur


def to_decimal(text: str) -> Decimal | None:
    t = re.sub(r"US|[,$%\s+]", "", str(text))
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_result_spec(spec: str) -> tuple[str, str, str] | None:
    m = re.match(r"^(.+?)#([^=]+)=(.+)$", spec)
    return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip()) if m else None


def parse_value(expected: str) -> tuple[Decimal, str, bool] | None:
    """'69.5% round' -> (Decimal('69.5'), 'round', True). None when unreadable."""
    m = VALUE_SPEC.match(expected.strip())
    if not m or not NUM_TOKEN.fullmatch(m.group("value")):
        return None
    want = to_decimal(m.group("value"))
    if want is None:
        return None
    return want, m.group("mode") or "exact", m.group("value").endswith("%")


def source_gives(source_value, source_text: str, want: Decimal, mode: str, pct: bool) -> bool:
    """Does the source number give the row's value V under the rules in the module text?"""
    got = to_decimal(source_value)
    if got is None:
        return False
    if pct and not str(source_text).endswith("%"):
        got = got * 100
    if mode == "exact":
        return got == want
    places = Decimal(1).scaleb(want.as_tuple().exponent)
    rounding = ROUND_DOWN if mode == "round down" else ROUND_HALF_UP
    return got.quantize(places, rounding=rounding) == want


def check_result_value(spec: str, root: Path) -> tuple[str | None, Decimal | None]:
    """Return (error, the row's value). The value is None on error."""
    parsed = parse_result_spec(spec)
    if not parsed:
        return f"bad result backing '{spec}' (want file.json#a.b=value)", None
    file_part, dotted, expected = parsed
    val = parse_value(expected)
    if val is None:
        return f"bad value '{expected}' in '{spec}' (want a number, then 'round' or 'round down' if rounded)", None
    want, mode, pct = val
    path = root / file_part
    if not path.is_file():
        return f"results file not found: {file_part}", None
    try:
        value = json_lookup(json.loads(path.read_text(encoding="utf-8")), dotted)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return f"path {dotted} not found in {file_part} ({exc!r})", None
    if isinstance(value, bool) or to_decimal(value) is None:
        return f"{file_part}#{dotted} is not a number: {value!r} vs {expected!r}", None
    if not source_gives(value, str(value), want, mode, pct):
        return f"{file_part}#{dotted} is {value}, but the row expects {expected}", None
    return None, want


def _flat(text: str) -> str:
    return " ".join(re.sub(r"[|*`]", " ", text).split())


def _tokens(text: str) -> list[tuple[int, int, str]]:
    """Numeric tokens as (start, end, token), with wrapping punctuation left out."""
    out = []
    for m in re.finditer(r"\S+", text):
        raw = m.group(0)
        lead = len(raw) - len(raw.lstrip("(\"'*`"))
        tok = raw.lstrip("(\"'*`").rstrip(".,;:)\"'*`")
        if tok and NUM_TOKEN.fullmatch(tok):
            out.append((m.start() + lead, m.start() + lead + len(tok), tok))
    return out


def check_text_value(spec: str, root: Path) -> tuple[str | None, Decimal | None]:
    """text:<file>#<text on one line>=<value>. Return (error, the row's value)."""
    if "#" not in spec or "=" not in spec:
        return f"bad text backing '{spec}' (want file#text=value)", None
    file_part, rest = spec.split("#", 1)
    quoted, expected = rest.rsplit("=", 1)
    file_part, quoted = file_part.strip(), _flat(quoted)
    val = parse_value(expected)
    if val is None or not quoted:
        return f"bad text backing '{spec}' (want file#text=value)", None
    want, mode, pct = val
    path = root / file_part
    if not path.is_file():
        return f"text file not found: {file_part}", None
    lines = path.read_text(encoding="utf-8").splitlines()
    if not any(quoted in _flat(ln) for ln in lines):
        return f"{file_part} has no line holding '{quoted}'", None
    if not any(source_gives(to_decimal(t), t, want, mode, pct) for _a, _b, t in _tokens(quoted)):
        return f"no number in '{quoted}' ({file_part}) gives the row's {expected.strip()}", None
    return None, want


def value_backing(b: str, root: Path) -> tuple[str | None, Decimal | None] | None:
    """(error, value) for a result: or text: backing; None for any other kind."""
    if b.startswith("result:"):
        return check_result_value(b[7:].strip(), root)
    if b.startswith("text:"):
        return check_text_value(b[5:].strip(), root)
    return None


def number_span(line: str, column: int) -> tuple[int, int, Decimal] | None:
    """The last whole numeric token before the marker, as (start, end, value)."""
    toks = _tokens(line[:column])
    if not toks:
        return None
    a, b, tok = toks[-1]
    return a, b, to_decimal(tok)


def number_before(line: str, column: int) -> Decimal | None:
    """The last whole numeric token before the marker. A token with a letter
    or '-' attached (BRT-ORC-019) is not a number."""
    span = number_span(line, column)
    return span[2] if span else None


def verified_number_spans(line: str, rows: dict[str, dict], root: Path) -> list[tuple[int, int]]:
    """Spans of the numbers on this line that a claim marker right after them backs:
    the marker's row has at least one value backing, every value backing checks
    against its source, and the number equals each row value. Used by the README guard."""
    spans = []
    for m in MARKER.finditer(line):
        row = rows.get(m.group(1))
        if not row:
            continue
        checked = [value_backing(b, root) for b in row["backings"]]
        checked = [c for c in checked if c is not None]
        if not checked or any(err for err, _v in checked):
            continue
        span = number_span(line, m.start())
        if span and all(span[2] == v for _e, v in checked):
            spans.append((span[0], span[1]))
    return spans


def check(build_path: Path, doc_paths: list[Path], root: Path) -> tuple[list[str], list[str], int]:
    errors: list[str] = []
    warnings: list[str] = []
    if not build_path.is_file():
        return [f"claims file not found: {build_path}"], [], 0
    missing = [d for d in doc_paths if not d.is_file()]
    errors += [f"document not found: {d}" for d in missing]
    doc_paths = [d for d in doc_paths if d.is_file()]
    try:
        rows, table_errors = parse_table(build_path.read_text(encoding="utf-8"))
    except ValueError as exc:
        return errors + [str(exc)], [], 0
    errors += table_errors

    markers, marker_errors = find_markers(doc_paths)
    errors += marker_errors
    used = {m[0] for m in markers}

    for cid, doc, n, _line, _col in markers:
        if cid not in rows:
            errors.append(f"{doc.name}:{n}: marker {cid} has no row in the claims table")

    for cid, row in rows.items():
        if not row["backings"]:
            errors.append(f"{cid}: the row has no backing")
        json_values: list[Decimal] = []
        for b in row["backings"]:
            if b.startswith("test:"):
                err = check_test_backing(b[5:].strip(), root)
                if err:
                    errors.append(f"{cid}: {err}")
            elif b.startswith(("result:", "text:")):
                err, val = value_backing(b, root)
                if err:
                    errors.append(f"{cid}: {err}")
                elif val is not None:
                    json_values.append(val)
            else:
                errors.append(f"{cid}: backing '{b}' must start with 'test:', 'result:' or 'text:'")
        if json_values:
            for mid, doc, n, line, col in markers:
                if mid != cid:
                    continue
                shown = number_before(line, col)
                if shown is None:
                    errors.append(f"{cid}: {doc.name}:{n}: no number just before the marker to compare")
                    continue
                for val in json_values:
                    if shown != val:
                        errors.append(f"{cid}: {doc.name}:{n}: the document says {shown}, "
                                      f"the row (checked against its source) says {val}")
        if cid not in used:
            warnings.append(f"{cid}: no document carries this claim's marker")
    return errors, warnings, len(rows)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=REPO, help="repository root (default: this repo)")
    ap.add_argument("--build", type=Path, default=None, help="file holding the claims table (default: BUILD.md)")
    ap.add_argument("--doc", type=Path, action="append", default=None,
                    help="document to scan for markers (repeatable; default: README, user guide, deck)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    build = root / (args.build or "BUILD.md")
    docs = [root / d for d in args.doc] if args.doc else default_docs(root)
    errors, warnings, count = check(build, docs, root)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"FAIL  {e}")
    if errors:
        print(f"{len(errors)} problem(s). Claims check failed.")
        return 1
    print(f"OK  {count} claim(s), {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
