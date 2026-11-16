"""Static check of the new UI pages (work order 4.1).

For each new page that exists: it links assets/app.css and assets/app.js, has no
<style> block longer than 15 lines, and contains no `cdn.`, `unpkg` or `alert(`.
Pages that do not exist yet are skipped and listed. Exit 1 on any violation.

Old pages that 4.2/4.3 will replace (same file name, no reference to assets/ at all)
are listed as "legacy" and skipped by default, because 4.1 may not edit them. Use
--strict (after 4.3) to check them too.

Usage: python scripts/check_ui_static.py [--strict] [ui_dir]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

PAGES = [
    "ask.html", "run.html", "runs.html", "admin_overview.html", "models.html",
    "keys.html", "rules.html", "settings.html", "skills.html", "logs.html",
]
MAX_STYLE_LINES = 15
FORBIDDEN = ("cdn.", "unpkg", "alert(")

_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", re.IGNORECASE | re.DOTALL)


def check_page(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8")
    problems: list[str] = []
    if not re.search(r"""<link\b[^>]*href=["']assets/app\.css["']""", text, re.IGNORECASE):
        problems.append("does not link assets/app.css")
    if not re.search(r"""<script\b[^>]*src=["']assets/app\.js["']""", text, re.IGNORECASE):
        problems.append("does not link assets/app.js")
    for m in _STYLE.finditer(text):
        lines = len(m.group(1).strip("\r\n").splitlines())
        if lines > MAX_STYLE_LINES:
            problems.append(f"<style> block is {lines} lines (limit {MAX_STYLE_LINES})")
    for needle in FORBIDDEN:
        if needle in text:
            problems.append(f"contains forbidden text {needle!r}")
    return problems


def run(ui_dir: Path, strict: bool = False) -> int:
    failed = False
    skipped: list[str] = []
    legacy: list[str] = []
    checked = 0
    for name in PAGES:
        path = ui_dir / name
        if not path.is_file():
            skipped.append(name)
            continue
        if not strict and "assets/" not in path.read_text(encoding="utf-8"):
            legacy.append(name)
            continue
        checked += 1
        problems = check_page(path)
        if problems:
            failed = True
            for p in problems:
                print(f"FAIL {name}: {p}")
        else:
            print(f"ok   {name}")
    if skipped:
        print("skipped (not built yet): " + ", ".join(skipped))
    if legacy:
        print("legacy, not yet replaced (use --strict to check): " + ", ".join(legacy))
    print(f"{checked} page(s) checked, {len(skipped)} skipped, {len(legacy)} legacy")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--strict"]
    ui_dir = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "ui"
    return run(ui_dir, strict="--strict" in argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
