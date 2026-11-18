#!/usr/bin/env python
"""Check the claims table in BUILD.md section 7 against the documents.

Every claim a document makes carries a marker ``<!-- claim C01 -->``. Each
marker needs a row in the table, and each row needs a backing that exists.

Table row:  ``| C01 | claim text | backing |``

Backing notation (several backings in one cell are separated by ``;``):

    test:tests/path/test_file.py::test_name
    test:tests/path/test_file.py::TestClass::test_name
    result:bench/results/run/summary.json#a.b.0.c=42.5

A ``test:`` backing is checked by reading the file for ``def test_name`` (and
``class TestClass``); pytest is never run here. A ``result:`` backing names a
JSON file, a dotted path into it (list positions are numbers) and the value
expected there. The check reads the number written on the marker's line in
the document (the first number once the marker itself is removed; commas,
``$``, ``%`` and spaces are ignored) and requires it to equal the JSON value.

Exit 0 when everything holds (warnings may print). Exit 1 on any error.

    python scripts/check_claims.py
    python scripts/check_claims.py --build X.md --doc A.md --doc B.html --root DIR
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from decimal import Decimal, InvalidOperation
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
MARKER = re.compile(r"<!--\s*claim\s+(C\d+)\s*-->")
ROW = re.compile(r"^\|\s*(C\d+)\s*\|(.*)\|\s*$")
NUMBER = re.compile(r"[-+]?\$?\d[\d,]*(?:\.\d+)?%?|[-+]?\$?\.\d+%?")


def default_docs(root: Path) -> list[Path]:
    docs = [root / "README.md", root / "docs" / "user-guide.md"]
    docs += sorted((root / "docs" / "presentation").glob("**/*.html"))
    return [d for d in docs if d.is_file()]


def parse_table(build_text: str) -> dict[str, dict]:
    """Return {id: {"claim": str, "backings": [str, ...]}} from section 7."""
    m = re.search(r"^##\s+7\.\s+Claims table\s*$", build_text, re.M)
    if not m:
        raise ValueError("BUILD.md has no '## 7. Claims table' section")
    rest = build_text[m.end():]
    nxt = re.search(r"^##\s", rest, re.M)
    section = rest[: nxt.start()] if nxt else rest
    rows: dict[str, dict] = {}
    for line in section.splitlines():
        r = ROW.match(line.strip())
        if not r:
            continue
        cells = [c.strip() for c in r.group(2).split("|")]
        if len(cells) < 2:
            continue
        claim, backing = " | ".join(cells[:-1]), cells[-1]
        backings = [b.strip().strip("`").strip() for b in backing.split(";") if b.strip()]
        rows[r.group(1)] = {"claim": claim, "backings": backings}
    return rows


def find_markers(doc_paths: list[Path]) -> list[tuple[str, Path, int, str]]:
    found = []
    for p in doc_paths:
        for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            for m in MARKER.finditer(line):
                found.append((m.group(1), p, n, line))
    return found


def check_test_backing(spec: str, root: Path) -> str | None:
    parts = spec.split("::")
    file_part, names = parts[0], parts[1:]
    path = root / file_part
    if not path.is_file():
        return f"test file not found: {file_part}"
    if not names:
        return f"no test name in '{spec}'"
    text = path.read_text(encoding="utf-8")
    *classes, func = [re.sub(r"\[.*\]$", "", n) for n in names]
    for c in classes:
        if not re.search(rf"^\s*class\s+{re.escape(c)}\b", text, re.M):
            return f"class {c} not found in {file_part}"
    if not re.search(rf"^\s*(?:async\s+)?def\s+{re.escape(func)}\s*\(", text, re.M):
        return f"test {func} not found in {file_part}"
    return None


def json_lookup(data, dotted: str):
    cur = data
    for key in dotted.split("."):
        if isinstance(cur, list):
            cur = cur[int(key)]
        else:
            cur = cur[key]
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


def number_on_line(line: str) -> Decimal | None:
    text = MARKER.sub(" ", line)
    for m in NUMBER.finditer(text):
        d = to_decimal(m.group(0))
        if d is not None:
            return d
    return None


def check(build_path: Path, doc_paths: list[Path], root: Path) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        rows = parse_table(build_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [str(exc)], []

    markers = find_markers(doc_paths)
    used = {cid for cid, *_ in markers}

    for cid, doc, n, _line in markers:
        if cid not in rows:
            errors.append(f"{doc.name}:{n}: marker {cid} has no row in the claims table")

    for cid, row in rows.items():
        if not row["backings"]:
            errors.append(f"{cid}: the row has no backing")
        results_ok: Decimal | None = None
        has_result = False
        for b in row["backings"]:
            if b.startswith("test:"):
                err = check_test_backing(b[5:].strip(), root)
                if err:
                    errors.append(f"{cid}: {err}")
            elif b.startswith("result:"):
                has_result = True
                err, results_ok = check_result_value(b[7:].strip(), root)
                if err:
                    errors.append(f"{cid}: {err}")
            else:
                errors.append(f"{cid}: backing '{b}' must start with 'test:' or 'result:'")
        if has_result and results_ok is not None:
            for mid, doc, n, line in markers:
                if mid != cid:
                    continue
                shown = number_on_line(line)
                if shown is None:
                    errors.append(f"{cid}: {doc.name}:{n}: no number on the marker's line to compare")
                elif shown != results_ok:
                    errors.append(
                        f"{cid}: {doc.name}:{n}: the document says {shown}, "
                        f"the results file says {results_ok}"
                    )
        if cid not in used:
            warnings.append(f"{cid}: no document carries this claim's marker")
    return errors, warnings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", type=Path, default=REPO, help="repository root (default: this repo)")
    ap.add_argument("--build", type=Path, default=None, help="file holding the claims table (default: BUILD.md)")
    ap.add_argument("--doc", type=Path, action="append", default=None,
                    help="document to scan for markers (repeatable; default: README, user guide, deck)")
    args = ap.parse_args(argv)
    root = args.root.resolve()
    build = args.build or root / "BUILD.md"
    docs = args.doc if args.doc else default_docs(root)
    errors, warnings = check(build, [Path(d) for d in docs], root)
    for w in warnings:
        print(f"WARN  {w}")
    for e in errors:
        print(f"FAIL  {e}")
    if errors:
        print(f"{len(errors)} problem(s). Claims check failed.")
        return 1
    rows = parse_table(build.read_text(encoding="utf-8"))
    print(f"OK  {len(rows)} claim(s), {len(warnings)} warning(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
