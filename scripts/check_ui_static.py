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

CI (work order 5.6) runs `--strict`, which ignores the allow-list. Strict currently fails
until 4.3/5.3 archive the old pages.

Usage: python scripts/check_ui_static.py [--strict] [ui_dir]
"""
from __future__ import annotations

import hashlib
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

LEGACY_ALLOW_LIST = {
    "_header.html": "4a86a2279b3d4371960e16257eeee4c337a9cd6ad85dfaec0ca91ca7d3cd173d",
    "admin.html": "8171d20d57d0f4f97bfc3f1cd34e7b92cabaac974c67f7496e1d9b32df3ac0f5",
    "chat.html": "f067a475b937d5521eea81b746cced27b436bc84cc1899b66dcf6ed65a37f4b0",
    "dashboard.html": "558b7095b378600ee95863ba14762303d206f1e3912c4cb709791125cd2fdcb5",
    "index.html": "ed754ab116fd98ffc0fce47c55a51eb561d64fe6d49d3d2fd0d077f9868ec908",
    "keys.html": "77790c2e83d9c455bdcdb7436cc3afa90d93b1bd5f65b34909bdf2802d4fcd14",
    "logs.html": "7a182afc321317f29594c5835806610beecca9ca8e75d85980f15cea05cf10f9",
    "models.html": "9f54cc47d73d14e31d5a0012b19deb7c69f12bdc8bf8f3045298eaeb3b0057e9",
    "run.html": "48fe2b2ab782e76b89f39cb7ccd956cb04db28f078b6069a9c8a31da405e9557",
    "skills.html": "68ec89148ab9d3d42e658356938d894b98ceb7f0b04f575a7babe86e3d1a135c",
}
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
