"""Render docs/presentation.generated.md from peak-context ledger entries.

Picks entries where demo_value is medium/high AND a presentation block
is present. Produces one slide per entry with hero bullet, demo script,
and slide angle — ready to paste into slides or a speaker script.

Usage:
    python .dev/render_presentation.py
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

LEDGER_DIR = Path(__file__).parent / "ledger"
OUT_PATH = Path(__file__).parent.parent / "docs" / "presentation.generated.md"


def _ts(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v or "")


def load_entries() -> list[dict]:
    entries = []
    for path in sorted(LEDGER_DIR.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as f:
            entries.append(yaml.safe_load(f))
    entries.sort(key=lambda e: _ts(e.get("timestamp")))
    return entries


def render(entries: list[dict]) -> str:
    picked = [e for e in entries
              if e.get("presentation") and e.get("demo_value") in ("medium", "high")]
    header = [
        "# Presentation Deck Source (generated)",
        "",
        f"> Generated from `.dev/ledger/` at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "> One slide per demo-worthy feature. Copy into your slide tool of choice.",
        "",
    ]
    if not picked:
        return "\n".join(header + ["_No demo-worthy features captured yet._", ""])

    slides = []
    for i, e in enumerate(picked, 1):
        p = e["presentation"]
        title = e.get("title", "")
        hero = p.get("hero_bullet", "").strip()
        angle = p.get("slide_angle", "").strip()
        script = p.get("demo_script") or []

        slide = [f"## Slide {i}: {title}", ""]
        if hero:
            slide.append(f"**Hero bullet:** {hero}")
            slide.append("")
        if angle:
            slide.append(f"*Angle:* {angle}")
            slide.append("")
        if script:
            slide.append("**Live demo script:**")
            for step in script:
                slide.append(f"- {step}")
            slide.append("")
        slides.append("\n".join(slide))

    return "\n".join(header + slides).rstrip() + "\n"


def main() -> int:
    if not LEDGER_DIR.exists():
        print(f"No ledger directory at {LEDGER_DIR}", file=sys.stderr)
        return 1
    entries = load_entries()
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(entries), encoding="utf-8")
    picked = sum(1 for e in entries if e.get("presentation") and e.get("demo_value") in ("medium", "high"))
    print(f"Wrote {OUT_PATH} — {picked} of {len(entries)} entries produced slides.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
