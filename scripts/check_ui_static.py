"""Static check of the UI (work order 4.1, tightened by the 4.1 review fixes).

Pages (ui/*.html) are parsed with the stdlib html.parser, so comments, attribute names
(`data-src` is not `src`) and script/style text are handled as a browser would. Every
file under ui/assets/ with extension .css or .js is scanned (never ui/assets/vendor/);
ui/assets/app.css and ui/assets/app.js must exist.

Pages must link assets/app.css (<link rel="stylesheet" href=...>) and assets/app.js
(<script src=...>); a leading "./" or "/ui/" prefix and a ?query or #fragment are fine.
They may have no <style> block longer than 15 lines. Nothing may load from outside:
src, href, srcset, poster, data, action and formaction in html, @import and url( in css,
import and fetch literals in js must not start with http:, https: or // (xmlns is not a
load). No `alert (` call in js, inline <script> or inline event handlers. `cdn` and
`unpkg` are rejected inside URL values only, not in prose.

Old pages that 4.2/4.3/5.3 replace are named in LEGACY_ALLOW_LIST, mapped to the sha256
of their committed content (CRLF normalised to LF). A page is exempt ("allowed legacy:
<name>") only while its content still hashes to that value; any edit removes the
exemption and the page is checked fully. The list must be empty by 5.6.

CI (work order 5.6) runs `--strict`, which ignores the allow-list. With the list empty
(since 4.2), strict and default give the same result.

Usage: python scripts/check_ui_static.py [--strict] [ui_dir]
"""
from __future__ import annotations

import hashlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

# Empty since 4.2: chat.html was archived (replaced by ask.html) and run.html rewritten.
LEGACY_ALLOW_LIST: dict[str, str] = {}
MAX_STYLE_LINES = 15
URL_ATTRS = ("src", "href", "srcset", "poster", "data", "action", "formaction")

_I = re.IGNORECASE
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.DOTALL)
_CSS_URL = re.compile(r"""\burl\(\s*(?:"([^"]*)"|'([^']*)'|([^)\s]*))""", _I)
_CSS_IMPORT = re.compile(r"""@import\s*(?:"([^"]*)"|'([^']*)')""", _I)
_JS_LOADS = [
    ("import", re.compile(r"""\bimport\s*\(\s*["'`]([^"'`]*)""")),
    ("import", re.compile(r"""\bimport\b[^;()'"`]*?\bfrom\s*["'`]([^"'`]*)""")),
    ("import", re.compile(r"""\bimport\s*["'`]([^"'`]*)""")),
    ("fetch", re.compile(r"""\bfetch\s*\(\s*["'`]([^"'`]*)""")),
]
_ALERT = re.compile(r"\balert\s*\(", _I)
_CDN = re.compile(r"cdn|unpkg", _I)


def _is_external(url: str) -> bool:
    return url.strip().lower().startswith(("http:", "https:", "//"))


def _url_problems(url: str, label: str) -> list[str]:
    out = []
    if _is_external(url):
        out.append(f"contains external {label}")
    if _CDN.search(url):
        out.append(f"contains cdn or unpkg host in {label}")
    return out


def _scan_css(text: str) -> list[str]:
    text = _CSS_COMMENT.sub("", text)
    out: list[str] = []
    for m in _CSS_IMPORT.finditer(text):
        out += _url_problems(next(g for g in m.groups() if g is not None), "@import")
    for m in _CSS_URL.finditer(text):
        out += _url_problems(next(g for g in m.groups() if g is not None), "url()")
    return out


def _scan_js(text: str) -> list[str]:
    out: list[str] = []
    for label, rx in _JS_LOADS:
        for m in rx.finditer(text):
            out += _url_problems(m.group(1), f"js {label}")
    if _ALERT.search(text):
        out.append("contains alert call")
    return out


def _dedupe(items: list[str]) -> list[str]:
    return list(dict.fromkeys(items))


class _Page(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tags: list[tuple[str, dict[str, str]]] = []
        self.styles: list[str] = []
        self.scripts: list[str] = []
        self._open: str | None = None
        self._buf: list[str] = []

    def handle_starttag(self, tag, attrs):
        d: dict[str, str] = {}
        for k, v in attrs:
            d.setdefault(k.lower(), v or "")  # the first duplicate wins, as in a browser
        self.tags.append((tag.lower(), d))
        if tag.lower() in ("script", "style"):
            self._open, self._buf = tag.lower(), []

    handle_startendtag = handle_starttag

    def handle_data(self, data):
        if self._open:
            self._buf.append(data)

    def handle_endtag(self, tag):
        if self._open and tag.lower() == self._open:
            (self.styles if self._open == "style" else self.scripts).append("".join(self._buf))
            self._open = None

    def close(self):
        super().close()
        if self._open:  # unterminated script/style
            (self.styles if self._open == "style" else self.scripts).append("".join(self._buf))
            self._open = None


def _norm_path(url: str) -> str:
    url = url.strip().lower().split("#", 1)[0].split("?", 1)[0]
    return url[2:] if url.startswith("./") else url


def _is_asset(url: str, name: str) -> bool:
    p = _norm_path(url)
    return not _is_external(p) and (p == name or p.endswith("/" + name))


def _parse(text: str) -> _Page:
    page = _Page()
    page.feed(text)
    page.close()
    return page


def check_page_text(text: str) -> list[str]:
    page = _parse(text)
    problems: list[str] = []
    css_ok = any(
        t == "link" and "stylesheet" in a.get("rel", "").lower().split() and _is_asset(a.get("href", ""), "assets/app.css")
        for t, a in page.tags
    )
    js_ok = any(t == "script" and _is_asset(a.get("src", ""), "assets/app.js") for t, a in page.tags)
    if not css_ok:
        problems.append('does not link assets/app.css with rel="stylesheet"')
    if not js_ok:
        problems.append("does not link assets/app.js")
    for css in page.styles:
        lines = len(css.strip("\r\n").splitlines())
        if lines > MAX_STYLE_LINES:
            problems.append(f"<style> block is {lines} lines (limit {MAX_STYLE_LINES})")
        problems += _scan_css(css)
    for js in page.scripts:
        problems += _scan_js(js)
    for _tag, attrs in page.tags:
        for name, value in attrs.items():
            if name in URL_ATTRS:
                urls = [c.split()[0] for c in value.split(",") if c.split()] if name == "srcset" else [value]
                for u in urls:
                    problems += _url_problems(u, f"{name} URL")
            elif name == "style":
                problems += _scan_css(value)
            elif name.startswith("on"):
                problems += _scan_js(value)
    return _dedupe(problems)


def check_page(path: Path) -> list[str]:
    return check_page_text(path.read_bytes().decode("utf-8", errors="replace"))


def check_asset(path: Path) -> list[str]:
    text = path.read_text(encoding="utf-8", errors="replace")
    return _dedupe(_scan_css(text) if path.suffix == ".css" else _scan_js(text))


def content_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes().replace(b"\r\n", b"\n")).hexdigest()


def _assets(ui_dir: Path) -> list[Path]:
    root = ui_dir / "assets"
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.rglob("*")
        if p.is_file() and p.suffix.lower() in (".css", ".js") and "vendor" not in p.relative_to(root).parts[:1]
    )


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
        if not strict and LEGACY_ALLOW_LIST.get(path.name) == content_hash(path):
            legacy += 1
            print(f"allowed legacy: {path.name}")
            continue
        report(path.name, check_page(path))
    for name in ("app.css", "app.js"):
        if not (ui_dir / "assets" / name).is_file():
            report(f"assets/{name}", ["required file is missing"])
    for path in _assets(ui_dir):
        report(path.relative_to(ui_dir).as_posix(), check_asset(path))
    print(f"{checked} file(s) checked, {legacy} allowed legacy")
    return 1 if failed else 0


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if a != "--strict"]
    ui_dir = Path(args[0]) if args else Path(__file__).resolve().parent.parent / "ui"
    return run(ui_dir, strict="--strict" in argv[1:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
