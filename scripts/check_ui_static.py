"""Static check of the UI (work order 4.1, tightened by the 4.1 review fixes).

Scans EVERY ui/*.html page plus ui/assets/app.css and ui/assets/app.js (never
ui/assets/vendor/). Case-insensitive. HTML comments are stripped from pages first.

Pages must link assets/app.css (<link rel="stylesheet" href=...>) and assets/app.js
(<script src=...>), and have no <style> block longer than 15 lines. Every scanned file
must have no src=/href=/@import/url( starting with http:, https: or //, no `alert (`
call, and no cdn host or unpkg.

Old pages that 4.2/4.3/5.3 replace are named in LEGACY_ALLOW_LIST. An allow-listed page
that does not link assets/app.js is reported "allowed legacy: <name>" and not checked.
Any other page, or an allow-listed name that already links assets/app.js, is checked
fully. The list must be empty by 5.6; --strict ignores it (CI uses --strict).

Usage: python scripts/check_ui_static.py [--strict] [ui_dir]
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LEGACY_ALLOW_LIST = [
    "_header.html", "admin.html", "chat.html", "dashboard.html", "index.html",
    "keys.html", "logs.html", "models.html", "run.html", "skills.html",
]
MAX_STYLE_LINES = 15

_FLAGS = re.IGNORECASE | re.DOTALL
_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
_STYLE = re.compile(r"<style\b[^>]*>(.*?)</style\s*>", _FLAGS)
_TAG_LINK = re.compile(r"<link\b[^>]*>", _FLAGS)
_TAG_SCRIPT = re.compile(r"<script\b[^>]*>", _FLAGS)
_EXTERNAL = [
    (re.compile(r"""\b(?:src|href)\s*=\s*["']?\s*(?:https?:|//)""", _FLAGS), "external src/href"),
    (re.compile(r"""@import\s*(?:url\(\s*)?["']?\s*(?:https?:|//)""", _FLAGS), "external @import"),
    (re.compile(r"""\burl\(\s*["']?\s*(?:https?:|//)""", _FLAGS), "external url()"),
    (re.compile(r"\balert\s*\(", _FLAGS), "alert call"),
    (re.compile(r"cdn|unpkg", _FLAGS), "cdn or unpkg host"),
]


def _attr(tag: str, name: str) -> str | None:
    m = re.search(rf"""\b{name}\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]+))""", tag, _FLAGS)
    if not m:
        return None
    return next(g for g in m.groups() if g is not None).strip()


def _scan_text(text: str) -> list[str]:
    return [f"contains {label}" for rx, label in _EXTERNAL if rx.search(text)]


def links_app_js(text: str) -> bool:
    text = _COMMENT.sub("", text)
    return any((_attr(t, "src") or "").lower() == "assets/app.js" for t in _TAG_SCRIPT.findall(text))


def check_page(path: Path) -> list[str]:
    text = _COMMENT.sub("", path.read_text(encoding="utf-8"))
    problems: list[str] = []
    css_ok = any(
        (_attr(t, "rel") or "").lower() == "stylesheet"
        and (_attr(t, "href") or "").lower() == "assets/app.css"
        for t in _TAG_LINK.findall(text)
    )
    if not css_ok:
        problems.append('does not link assets/app.css with rel="stylesheet"')
    if not links_app_js(text):
        problems.append("does not link assets/app.js")
    for m in _STYLE.finditer(text):
        lines = len(m.group(1).strip("\r\n").splitlines())
        if lines > MAX_STYLE_LINES:
            problems.append(f"<style> block is {lines} lines (limit {MAX_STYLE_LINES})")
    problems += _scan_text(text)
    return problems


def check_asset(path: Path) -> list[str]:
    return _scan_text(path.read_text(encoding="utf-8"))


def run(ui_dir: Path, strict: bool = False) -> int:
    failed = False
    checked = legacy = 0

    def report(name: str, problems: list[str]) -> None:
        nonlocal failed, checked
        checked += 1
        if problems:
            failed = True
            for p in problems:
                print(f"FAIL {name}: {p}")
        else:
            print(f"ok   {name}")

    for path in sorted(ui_dir.glob("*.html")):
        text = path.read_text(encoding="utf-8")
        if not strict and path.name in LEGACY_ALLOW_LIST and not links_app_js(text):
            legacy += 1
            print(f"allowed legacy: {path.name}")
            continue
        report(path.name, check_page(path))
    for name in ("app.css", "app.js"):
        path = ui_dir / "assets" / name
        if path.is_file():
            report(f"assets/{name}", check_asset(path))
    print(f"{checked} file(s) checked, {legacy} allowed legacy")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--strict"]
    ui_dir = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "ui"
    return run(ui_dir, strict="--strict" in argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
