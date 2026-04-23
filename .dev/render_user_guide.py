"""Render docs/user-guide.md from peak-context ledger entries.

Reads every .yaml file in .dev/ledger/ and renders sections for entries
that carry a 'user_guide' block. Ledger entries without that block are
skipped (they're internal or not user-facing).

Output matches the existing user-guide.md style: "How to use it" +
"If something goes wrong".

Usage:
    python .dev/render_user_guide.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

LEDGER_DIR = Path(__file__).parent / "ledger"
OUT_PATH = Path(__file__).parent.parent / "docs" / "user-guide.generated.md"


def _ts(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v or "")


def load_entries() -> list[dict]:
    entries = []
    for path in sorted(LEDGER_DIR.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        entries.append(data)
    entries.sort(key=lambda e: _ts(e.get("timestamp")))
    return entries


def render_section(e: dict) -> str | None:
    ug = e.get("user_guide")
    if not ug:
        return None
    heading = ug.get("heading") or e.get("title", "(untitled)")
    lines = [f"## {heading}"]
    summary = (e.get("summary_human") or "").strip()
    if summary:
        lines.append("")
        lines.append(summary)

    steps = ug.get("how_to_use") or []
    if steps:
        lines.append("")
        lines.append("### How to use it")
        for i, s in enumerate(steps, 1):
            lines.append(f"{i}. {s}")

    trouble = ug.get("troubleshooting") or []
    if trouble:
        lines.append("")
        lines.append("### If something goes wrong")
        for t in trouble:
            sym = t.get("symptom", "").strip()
            fix = t.get("cause_and_fix", "").strip()
            if sym and fix:
                lines.append(f"- **{sym}** — {fix}")

    codes = e.get("error_codes_added") or []
    if codes:
        lines.append("")
        lines.append("### Error codes related to this feature")
        for c in codes:
            lines.append(f"- `{c.get('code')}` — {c.get('meaning','')}")

    lines.append("")
    return "\n".join(lines)


def render(entries: list[dict]) -> str:
    header = [
        "# User Guide (generated)",
        "",
        f"> Generated from `.dev/ledger/` at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "> Do not hand-edit — re-run `python .dev/render_user_guide.py`.",
        "> Only ledger entries with a `user_guide:` block appear here.",
        "",
    ]
    sections = [s for s in (render_section(e) for e in entries) if s]
    if not sections:
        return "\n".join(header + ["_No user-facing features captured yet._", ""])
    return "\n".join(header + sections).rstrip() + "\n"


def main() -> int:
    if not LEDGER_DIR.exists():
        print(f"No ledger directory at {LEDGER_DIR}", file=sys.stderr)
        return 1
    entries = load_entries()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(entries), encoding="utf-8")
    covered = sum(1 for e in entries if e.get("user_guide"))
    print(f"Wrote {OUT_PATH} — {covered} of {len(entries)} ledger entries carried user_guide blocks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
