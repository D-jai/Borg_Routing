#!/usr/bin/env python
"""Check the claims table in BUILD.md section 7 against the documents.

Every claim a document makes carries a marker ``<!-- claim C01 -->``. Each
marker needs a row in the table, and each row needs a backing that exists.

Table row:  ``| C01 | claim text | backing |``

Backing notation (several backings in one cell are separated by ``;``):

    test:tests/path/test_file.py::test_name
    test:tests/path/test_file.py::TestClass::test_name
    result:bench/results/run/summary.json#a.b.0.c=42.5

A ``test:`` backing is checked by parsing the file with ``ast``: the function
must be a top-level def, or a method directly inside the named class (text in
strings, comments or nested helpers does not count); pytest is never run here. A ``result:`` backing names a
JSON file, a dotted path into it (list positions are numbers) and the value
expected there. The check reads the number written just before the marker on its line (the
last whole number before it; a token with a letter or ``-`` attached, such as
BRT-ORC-019, is not a number; commas, ``$`` and ``%`` are ignored) and requires
it to equal the JSON value. Every ``result:`` backing of a row is compared.
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
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*claim\s+(C\d+)\s*-->")
ANY_CLAIM_COMMENT = re.compile(r"<!--\s*claim\b", re.I)
ROW_START = re.compile(r"^\|\s*[Cc]")
ID_OK = re.compile(r"^C\d+$")
NUM_TOKEN = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?")


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
            if line.lstrip().startswith("```"):
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
    t = re.sub(r"[,$%\s+]", "", str(text))
    try:
        return Decimal(t)
    except InvalidOperation:
        return None


def parse_result_spec(spec: str) -> tuple[str, str, str] | None:
    m = re.match(r"^(.+?)#([^=]+)=(.+)$", spec)
    return (m.group(1).strip(), m.group(2).strip(), m.group(3).strip()) if m else None


def check_result_value(spec: str, root: Path) -> tuple[str | None, Decimal | None]:
    """Return (error, json_value). json_value is None on error."""
    parsed = parse_result_spec(spec)
    if not parsed:
        return f"bad result backing '{spec}' (want file.json#a.b=value)", None
    file_part, dotted, expected = parsed
    path = root / file_part
    if not path.is_file():
        return f"results file not found: {file_part}", None
    try:
        value = json_lookup(json.loads(path.read_text(encoding="utf-8")), dotted)
    except (KeyError, IndexError, ValueError, TypeError) as exc:
        return f"path {dotted} not found in {file_part} ({exc!r})", None
    got, want = to_decimal(value), to_decimal(expected)
    if got is None or want is None:
        return f"{file_part}#{dotted} is not a number: {value!r} vs {expected!r}", None
    if got != want:
        return f"{file_part}#{dotted} is {value}, but the row expects {expected}", None
    return None, got


def number_before(line: str, column: int) -> Decimal | None:
    """The last whole numeric token before the marker. A token with a letter
    or '-' attached (BRT-ORC-019) is not a number."""
    last = None
    for tok in line[:column].split():
        tok = tok.strip("(\"'").rstrip(".,;:)\"'")
        if NUM_TOKEN.fullmatch(tok):
            last = to_decimal(tok)
    return last


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
            elif b.startswith("result:"):
                err, val = check_result_value(b[7:].strip(), root)
                if err:
                    errors.append(f"{cid}: {err}")
                elif val is not None:
                    json_values.append(val)
            else:
                errors.append(f"{cid}: backing '{b}' must start with 'test:' or 'result:'")
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
                                      f"the results file says {val}")
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
