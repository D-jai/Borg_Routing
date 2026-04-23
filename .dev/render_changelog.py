"""Render a CHANGELOG.md from peak-context ledger entries.

Reads every .yaml file in .dev/ledger/, groups by scope, sorts by timestamp,
and writes docs/CHANGELOG.md. Idempotent — run any time, gets current view.

Usage:
    python .dev/render_changelog.py
"""
from __future__ import annotations

import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _ts(v) -> str:
    if isinstance(v, datetime):
        return v.isoformat()
    return str(v or "")

LEDGER_DIR = Path(__file__).parent / "ledger"
OUT_PATH = Path(__file__).parent.parent / "docs" / "CHANGELOG.md"

TYPE_LABEL = {"feat": "Added", "fix": "Fixed", "refactor": "Changed",
              "internal": "Internal", "docs": "Docs", "chore": "Chore"}


def load_entries() -> list[dict]:
    entries = []
    for path in sorted(LEDGER_DIR.glob("*.yaml")):
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f)
        data["_file"] = path.name
        entries.append(data)
    return entries


def render(entries: list[dict]) -> str:
    by_scope: dict[str, list[dict]] = defaultdict(list)
    for e in entries:
        by_scope[e.get("scope", "unscoped")].append(e)
    for scope in by_scope:
        by_scope[scope].sort(key=lambda e: _ts(e.get("timestamp")))

    lines = [
        "# Changelog",
        "",
        f"> Generated from `.dev/ledger/` at {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}.",
        "> Do not hand-edit — re-run `python .dev/render_changelog.py`.",
        "",
    ]

    for scope in sorted(by_scope.keys()):
        lines.append(f"## {scope}")
        lines.append("")
        buckets: dict[str, list[dict]] = defaultdict(list)
        for e in by_scope[scope]:
            buckets[TYPE_LABEL.get(e.get("type", "feat"), "Other")].append(e)

        for label in ["Added", "Changed", "Fixed", "Internal", "Docs", "Chore", "Other"]:
            if not buckets.get(label):
                continue
            lines.append(f"### {label}")
            lines.append("")
            for e in buckets[label]:
                title = e.get("title", "(untitled)")
                eid = e.get("id", "")
                ts = _ts(e.get("timestamp"))[:10]
                flags = []
                if e.get("breaking_change"):
                    flags.append("**BREAKING**")
                if e.get("demo_value") == "high":
                    flags.append("demo")
                if not e.get("user_visible", True):
                    flags.append("internal")
                flag_str = f" _{', '.join(flags)}_" if flags else ""
                lines.append(f"- **{title}** — `{eid}`{flag_str} ({ts})")

                summary = (e.get("summary_human") or "").strip()
                if summary:
                    for para in summary.split("\n\n"):
                        para = " ".join(para.split())
                        if para:
                            lines.append(f"  {para}")

                surf = e.get("public_surface", {}).get("added") or []
                if surf:
                    lines.append("  New surface:")
                    for s in surf:
                        kind = s.get("kind", "")
                        path = s.get("path", "")
                        purpose = s.get("purpose", "")
                        lines.append(f"  - `{kind}` `{path}` — {purpose}")

                codes = e.get("error_codes_added") or []
                if codes:
                    codes_str = ", ".join(f"`{c.get('code')}`" for c in codes)
                    lines.append(f"  Error codes: {codes_str}")

                envs = e.get("env_vars_added") or []
                if envs:
                    envs_str = ", ".join(f"`{v.get('name')}`" for v in envs)
                    lines.append(f"  Env vars: {envs_str}")

                deps = e.get("dependencies_added") or []
                if deps:
                    deps_str = ", ".join(f"`{d.get('name')}`" for d in deps)
                    lines.append(f"  Dependencies: {deps_str}")

                lines.append("")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    if not LEDGER_DIR.exists():
        print(f"No ledger directory at {LEDGER_DIR}", file=sys.stderr)
        return 1
    entries = load_entries()
    if not entries:
        print("No ledger entries found.", file=sys.stderr)
        return 1
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(render(entries), encoding="utf-8")
    print(f"Wrote {OUT_PATH} from {len(entries)} ledger entries.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
