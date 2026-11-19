"""Interface check (work order 4.6): every page, in a real browser, against the replay server.

Runs in CI only (job `ui_check` in .github/workflows/ci.yml; planner ruling 38a): Playwright
and a browser are never installed on the maintainer's laptop. The app must already be running
in replay mode (BORG_DEMO_MODE=replay) at --base-url.

For each page in PAGES, at 390 and 1280 px wide, light and dark:
  load it, wait for network idle, then fail on
  - any uncaught script error, any console error, any request that failed or answered >= 400,
    except the refusals that page is expected to get in replay (PAGES[...]["expected_failures"]):
    the admin pages read routes that replay refuses with 403 (src/api/replay.py), and the
    browser logs each refusal as a console error. Those exact requests are allowed, and each
    must happen: a listed refusal that does not happen fails too, so the list cannot go stale;
  - a required element missing, hidden, or without its expected words (PAGES[...]["required"]);
  - horizontal scroll at 390 px;
  then save a full-page screenshot.
Then, once (1280 px, light):
  - Ask: play one recorded run to the end and check the receipt (at least three steps);
  - Run: open that playback and check the header tiles and at least three step nodes;
  - Ask: render hostile markdown through Borg.markdown (the real marked + DOMPurify path) and
    check that no on* attribute and no javascript: link reaches the DOM.

Every failure prints one line: FAIL <page> <width> <theme>: <what failed>. Exit 1 on any
failure, 2 when the server cannot be reached. A JSON report and the screenshots go to --out.

Usage: python scripts/check_ui.py [--base-url URL] [--out DIR] [--speed N] [--job N]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterable, Optional
from urllib.parse import urlsplit

WIDTHS = (390, 1280)
THEMES = ("light", "dark")
HEIGHT = {390: 844, 1280: 900}
PHONE = 390
THEME_KEY = "borg_theme"          # ui/assets/app.js keeps the chosen theme under this key
REFUSED = 403                     # replay's answer to every admin read (BRT-RPL-001)
REFUSAL_NOTICE = {"css": "#notice-slot .notice.bad", "text": "BRT-RPL-001"}
LOAD_ERROR = "Failed to load resource"   # Chromium's console line for a response >= 400

# On every page: the shell app.js builds, and the replay notice it shows.
SHELL = [
    {"css": "header.topbar"},
    {"css": "header.topbar #theme"},
    {"css": "nav.side a", "min": 9},
    {"css": "#replay-chip", "text": "Replay of a recorded run"},
]

# One entry per page in ui/. "required": what a replay visitor sees once the page has loaded
# (css selector, at least "min" matches (default 1), visible unless "visible" is False, and,
# when "text" is given, one match containing it). "api_ok": the API reads the page makes on
# load that replay answers. "expected_failures": the reads replay refuses, as
# (method, path?query, status), exactly as the page's script sends them.
PAGES: dict = {
    "ask": {
        "required": [
            {"css": "#replay-runs"},
            {"css": "#replay-runs button", "min": 3, "text": "Play Job"},
            {"css": "#thread", "visible": False},
            {"css": "#thread p.muted", "text": "Choose a recorded demo run"},
            {"css": "#q", "visible": False},
            {"css": "#send", "visible": False},
            {"css": "#clear"},
        ],
        "api_ok": [("GET", "/replay/status"), ("GET", "/models/matrix")],
        "expected_failures": [],
    },
    "run": {
        "query": "?id={run_id}",
        "required": [
            {"css": "#loading", "visible": False},
            {"css": "#content"},
            {"css": "#status-chip .status", "text": "Completed"},
            {"css": "#prompt"},
            {"css": "#tiles .tile", "min": 4},
            {"css": "#plan-box svg.plan g.node:not(.core)", "min": 3},
            {"css": "#panel"},
            {"css": "#timeline li", "min": 1},
        ],
        "api_ok": [("GET", "/replay/status"), ("GET", "/models/matrix"), ("GET", "/runs/{run_id}")],
        "expected_failures": [],
    },
    "runs": {
        "required": [
            {"css": "#counts", "text": " runs · "},
            {"css": "#list table"},
            {"css": "#list tbody tr", "min": 3},
            {"css": "#reload"},
        ],
        "api_ok": [("GET", "/replay/status"), ("GET", "/runs?limit=200")],
        "expected_failures": [],
    },
    # Every read here is quiet (no notice): the page shows its "not set" / empty states.
    "admin_overview": {
        "required": [
            {"css": "#checklist .check", "min": 5},
            {"css": "#checklist", "text": "Cloud key not saved"},
            {"css": "#tiles .tile", "min": 3},
            {"css": "#spend", "text": "Nothing spent yet"},
            {"css": "#runs tbody tr", "min": 3},
        ],
        "api_ok": [("GET", "/replay/status"), ("GET", "/models/matrix"), ("GET", "/runs?limit=200")],
        "expected_failures": [("GET", "/keys/status", REFUSED), ("GET", "/status/local", REFUSED),
                              ("GET", "/config/effective", REFUSED), ("GET", "/logs/cost-summary", REFUSED)],
    },
    "models": {
        "required": [
            {"css": "#discover"},
            {"css": "#research"},
            {"css": "#pool table"},
            {"css": "#pool tbody tr", "min": 1},
            {"css": "#pool input[type=checkbox]", "min": 1},
        ],
        "api_ok": [("GET", "/replay/status"), ("GET", "/models/matrix")],
        "expected_failures": [],
    },
    "keys": {
        "required": [
            REFUSAL_NOTICE,
            {"css": "#openrouter-form"},
            {"css": "#openrouter-state", "text": "Checking"},
            {"css": "#tavily-state"},
        ],
        "api_ok": [("GET", "/replay/status")],
        "expected_failures": [("GET", "/keys/status", REFUSED)],
    },
    "rules": {
        "required": [
            REFUSAL_NOTICE,
            {"css": "#global-rules"},
            {"css": "#ledger", "text": "could not be loaded"},
            {"css": "#new-rule"},
        ],
        "api_ok": [("GET", "/replay/status")],
        "expected_failures": [("GET", "/config", REFUSED), ("GET", "/config/rules", REFUSED)],
    },
    "settings": {
        "required": [
            REFUSAL_NOTICE,
            {"css": "#groups", "text": "could not be loaded"},
        ],
        "api_ok": [("GET", "/replay/status")],
        "expected_failures": [("GET", "/config/effective", REFUSED)],
    },
    "skills": {
        "required": [
            REFUSAL_NOTICE,
            {"css": "#skills", "text": "could not be loaded"},
        ],
        "api_ok": [("GET", "/replay/status")],
        "expected_failures": [("GET", "/skills", REFUSED)],
    },
    "logs": {
        "required": [
            REFUSAL_NOTICE,
            {"css": ".tabs button", "min": 3},
            {"css": "#events", "text": "could not be loaded"},
            {"css": "#f-run"},
        ],
        "api_ok": [("GET", "/replay/status")],
        "expected_failures": [("GET", "/logs?limit=200", REFUSED)],
    },
}

# Allowed on any page and never required: a browser may ask for a favicon the pages do not
# declare, and replay refuses (403) or the server lacks (404) it.
TOLERATED = {("GET", "/favicon.ico", 403), ("GET", "/favicon.ico", 404)}

# The hostile markdown for the sanitiser probe. `<img src=x>` may stay (a same-origin relative
# load) without its handler, so the browser asks for /ui/x and gets 404: allowed during the
# probe (not required: a sanitiser that drops the src is fine too), nothing else is.
XSS_MARKDOWN = ('Before.\n\n<img src=x onerror="window.__borgXss=1">\n\n'
                '[x](javascript:window.__borgXss=2)\n\n'
                '<a href="javascript:window.__borgXss=3">y</a> <a href=" JaVaScRiPt:window.__borgXss=4">z</a>\n')
XSS_ALLOWED = {("GET", "/ui/x", 404)}

REQUIRED_KEYS = {"css", "min", "text", "visible"}


# --- pure parts (tested in tests/scripts/test_check_ui.py) ------------------------------------

def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Interface check against a replay-mode BORG_ROUTE.")
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="where the replay server runs")
    p.add_argument("--out", default="ui_check_out", help="folder for screenshots and report.json")
    p.add_argument("--speed", type=float, default=None,
                   help="replay speed factor for the played run (default: the server's, at most about 60 s)")
    p.add_argument("--job", default="1", help="which recorded job the Ask page plays (default 1)")
    p.add_argument("--play-timeout", type=float, default=120.0, help="seconds to wait for the played run")
    p.add_argument("--settle", type=float, default=5.0, help="seconds a page gets for its required elements")
    p.add_argument("--pages", default=",".join(PAGES), help="comma-separated subset of pages")
    a = p.parse_args(argv)
    if a.speed is not None and not a.speed > 0:
        p.error("--speed must be a positive number")
    a.base_url = a.base_url.rstrip("/")
    a.pages = [x.strip() for x in a.pages.split(",") if x.strip()]
    unknown = [x for x in a.pages if x not in PAGES]
    if unknown:
        p.error("unknown page(s): " + ", ".join(unknown))
    return a


def spec_problems(pages: dict = PAGES) -> list:
    """Why the PAGES table is malformed ([] when it is well-formed)."""
    out = []
    for name, spec in pages.items():
        req = spec.get("required")
        if not isinstance(req, list) or not req:
            out.append(f"{name}: no required elements")
            req = []
        for i, r in enumerate(req):
            if not isinstance(r, dict) or not isinstance(r.get("css"), str) or not r["css"].strip():
                out.append(f"{name}: required[{i}] has no css selector")
                continue
            if set(r) - REQUIRED_KEYS:
                out.append(f"{name}: required[{i}] has unknown keys {sorted(set(r) - REQUIRED_KEYS)}")
            if not isinstance(r.get("min", 1), int) or r.get("min", 1) < 1:
                out.append(f"{name}: required[{i}] min must be a whole number >= 1")
            if "text" in r and (not isinstance(r["text"], str) or not r["text"]):
                out.append(f"{name}: required[{i}] text must be non-empty words")
        for key in ("api_ok", "expected_failures"):
            if not isinstance(spec.get(key), list):
                out.append(f"{name}: {key} must be a list")
        for f in spec.get("expected_failures") or []:
            if not (isinstance(f, tuple) and len(f) == 3 and f[0] in ("GET", "POST", "PATCH", "DELETE", "PUT")
                    and isinstance(f[1], str) and f[1].startswith("/") and isinstance(f[2], int) and f[2] >= 400):
                out.append(f"{name}: bad expected failure {f!r}")
        for c in spec.get("api_ok") or []:
            if not (isinstance(c, tuple) and len(c) == 2 and isinstance(c[1], str) and c[1].startswith("/")):
                out.append(f"{name}: bad api_ok entry {c!r}")
    return out


def path_of(url: str) -> str:
    """'/path?query' of a URL (the origin dropped), as PAGES lists requests."""
    parts = urlsplit(url)
    return (parts.path or "/") + ("?" + parts.query if parts.query else "")


def classify(responses: Iterable, request_failures: Iterable, console_errors: Iterable,
             page_errors: Iterable, expected: Iterable, tolerated: Iterable = TOLERATED) -> list:
    """What went wrong on one page load, as plain sentences ([] when nothing did).

    responses: (method, url, status) of every response; request_failures: (method, url, error)
    of every request that got no response; console_errors: (text, url the console names or "");
    page_errors: uncaught script errors. A response >= 400 is allowed only when listed in
    `expected` or `tolerated`; every `expected` refusal must also have happened. A console error
    is allowed only when it is the browser's own line for an allowed response."""
    expected, tolerated = set(expected), set(tolerated)
    out, allowed_paths, allowed_status, seen = [], set(), set(), set()
    for method, url, status in responses:
        if status < 400:
            continue
        key = (method.upper(), path_of(url), int(status))
        if key in expected or key in tolerated:
            seen.add(key)
            allowed_paths.add(path_of(url))
            allowed_status.add(int(status))
        else:
            out.append(f"failed request: {key[0]} {key[1]} -> {key[2]}")
    for key in sorted(expected - seen):
        out.append(f"expected refusal did not happen: {key[0]} {key[1]} -> {key[2]} (update PAGES)")
    for method, url, error in request_failures:
        out.append(f"request failed: {method.upper()} {path_of(url)} ({error})")
    for text, where in console_errors:
        text = str(text)
        if text.startswith(LOAD_ERROR):
            if where and path_of(where) in allowed_paths:
                continue
            if not where and any(f"status of {s}" in text for s in allowed_status):
                continue
        out.append("console error: " + " ".join(text.split())[:300])
    for err in page_errors:
        out.append("uncaught script error: " + " ".join(str(err).split())[:300])
    return out


def page_url(base: str, name: str, run_id: str) -> str:
    return f"{base}/ui/{name}.html" + PAGES[name].get("query", "").format(run_id=run_id)


# --- the browser part (CI only) -----------------------------------------------------------------

_CHECK_JS = """(reqs) => {
  function shown(e) {
    const s = getComputedStyle(e);
    return s.display !== 'none' && s.visibility !== 'hidden' && e.getClientRects().length > 0;
  }
  return reqs.map((r) => {
    let found;
    try { found = Array.from(document.querySelectorAll(r.css)); } catch (err) { return 'bad selector ' + r.css; }
    const min = r.min || 1;
    const want = r.visible === false ? found : found.filter(shown);
    if (want.length < min) {
      return found.length >= min ? r.css + ': present but hidden (' + want.length + ' of ' + min + ' shown)'
        : r.css + ': ' + found.length + ' found, at least ' + min + ' required';
    }
    if (r.text && !want.some((e) => (e.textContent || '').includes(r.text))) {
      return r.css + ': no match says "' + r.text + '"';
    }
    return null;
  }).filter(Boolean);
}"""

_SCROLL_JS = """() => {
  const d = document.documentElement, b = document.body;
  return Math.max(d.scrollWidth, b ? b.scrollWidth : 0) - d.clientWidth;
}"""

_XSS_JS = """(src) => {
  const box = document.createElement('div');
  box.id = 'xss-probe';
  box.innerHTML = window.Borg.markdown(src);
  document.getElementById('page').appendChild(box);
  const bad = [];
  box.querySelectorAll('*').forEach((e) => {
    Array.from(e.attributes).forEach((a) => {
      if (/^on/i.test(a.name)) bad.push('<' + e.tagName.toLowerCase() + '> kept ' + a.name);
      const v = String(a.value).replace(/[\\s\\u0000-\\u001f]/g, '').toLowerCase();
      if (/^(href|src|xlink:href|action|formaction)$/i.test(a.name) && v.startsWith('javascript:'))
        bad.push('<' + e.tagName.toLowerCase() + '> kept ' + a.name + '="' + a.value + '"');
    });
  });
  return {bad: bad, html: box.innerHTML.slice(0, 500), imgs: box.querySelectorAll('img').length};
}"""


class Recorder:
    """Collects what a page did, for classify()."""

    def __init__(self, page):
        self.responses, self.request_failures, self.console_errors, self.page_errors = [], [], [], []
        page.on("response", lambda r: self.responses.append((r.request.method, r.url, r.status)))
        page.on("requestfailed", lambda r: self.request_failures.append((r.method, r.url, r.failure or "failed")))
        page.on("console", self._console)
        page.on("pageerror", lambda e: self.page_errors.append(str(e)))

    def _console(self, msg):
        if msg.type == "error":
            loc = msg.location or {}
            self.console_errors.append((msg.text, loc.get("url") or ""))

    def problems(self, expected) -> list:
        return classify(self.responses, self.request_failures, self.console_errors, self.page_errors, expected)


class Report:
    def __init__(self, out: Path):
        self.out, self.checks, self.failures = out, [], 0

    def add(self, page: str, width, theme: str, problems: list, **extra) -> None:
        for p in problems:
            print(f"FAIL {page} {width} {theme}: {p}", flush=True)
        self.failures += len(problems)
        self.checks.append(dict(page=page, width=width, theme=theme, ok=not problems, failures=problems, **extra))
        if not problems:
            print(f"ok   {page} {width} {theme}", flush=True)


def _settle(page, required: list, seconds: float) -> list:
    deadline = time.monotonic() + seconds
    while True:
        missing = page.evaluate(_CHECK_JS, required)
        if not missing or time.monotonic() >= deadline:
            return missing
        page.wait_for_timeout(250)


def _new_context(browser, width: int, theme: str):
    ctx = browser.new_context(viewport={"width": width, "height": HEIGHT[width]}, color_scheme=theme)
    ctx.add_init_script(f"try {{ localStorage.setItem('{THEME_KEY}', '{theme}'); }} catch (e) {{}}")
    return ctx


def check_page(ctx, report: Report, args, name: str, width: int, theme: str, run_id: str) -> None:
    page = ctx.new_page()
    rec = Recorder(page)
    problems, shot = [], args.out / f"{name}_{width}_{theme}.png"
    try:
        page.goto(page_url(args.base_url, name, run_id), wait_until="networkidle", timeout=30000)
        problems += _settle(page, SHELL + PAGES[name]["required"], args.settle)
        applied = page.evaluate("() => document.documentElement.getAttribute('data-theme')")
        if applied != theme:
            problems.append(f"theme not applied (data-theme is {applied!r})")
        if width == PHONE:
            extra = page.evaluate(_SCROLL_JS)
            if extra > 1:
                problems.append(f"horizontal scroll at {width} px ({extra} px wider than the screen)")
        page.screenshot(path=str(shot), full_page=True)
    except Exception as exc:  # a timeout or a crash is a failure of this page, not of the run
        problems.append(f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
    problems += rec.problems(PAGES[name]["expected_failures"])
    report.add(name, width, theme, problems, screenshot=shot.name if shot.exists() else None)
    page.close()


def play_run(ctx, report: Report, args, run: dict) -> Optional[str]:
    """Ask: play one recorded run to the end; check the receipt. Returns the playback's run id."""
    page = ctx.new_page()
    rec = Recorder(page)
    problems, sid, shot = [], None, args.out / "ask_played_1280_light.png"
    if args.speed is not None:
        def with_speed(route):
            try:
                body = json.loads(route.request.post_data or "{}")
                body["replay_speed"] = args.speed
                route.continue_(post_data=json.dumps(body))
            except ValueError:
                route.continue_()
        page.route("**/v1/chat/completions", with_speed)
    try:
        page.goto(page_url(args.base_url, "ask", ""), wait_until="networkidle", timeout=30000)
        button = page.locator("#replay-runs button", has_text=f"Play Job {run.get('job')}:")
        if button.count() != 1:
            raise AssertionError(f"{button.count()} buttons for job {run.get('job')}, expected 1")
        started = time.monotonic()
        button.click()
        page.locator("#thread .receipt .receipt-grid .stat").first.wait_for(timeout=args.play_timeout * 1000)
        print(f"     ask played job {run.get('job')} in {time.monotonic() - started:.1f} s", flush=True)
        stats = page.evaluate("""() => Array.from(document.querySelectorAll('#thread .receipt .receipt-grid .stat'))
            .map((s) => [s.querySelector('.k').textContent, s.querySelector('.v').textContent])""")
        steps = dict(stats).get("Steps")
        if len(stats) < 4:
            problems.append(f"receipt has {len(stats)} figures, at least 4 expected")
        if not (steps or "").strip().isdigit() or int(steps) < 3:
            problems.append(f"receipt says Steps {steps!r}, at least 3 expected")
        link = page.locator("#thread article.answer a[href^='run.html?id=']").first
        href = link.get_attribute("href") or ""
        sid = href.split("id=", 1)[1] if "id=" in href else None
        if not sid:
            problems.append("the played answer has no link to its run")
        if page.locator("#thread .receipt").count() != 1:
            problems.append("expected exactly one receipt after one play")
        page.screenshot(path=str(shot), full_page=True)
    except Exception as exc:
        problems.append(f"play: {type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
    problems += rec.problems([])
    report.add("ask (play)", 1280, "light", problems, screenshot=shot.name if shot.exists() else None, run_id=sid)
    page.close()
    return sid


def check_played_run(ctx, report: Report, args, sid: str) -> None:
    """Run: the playback the Ask page made, opened by its own id, shows the run at its end."""
    page = ctx.new_page()
    rec = Recorder(page)
    problems, shot = [], args.out / "run_played_1280_light.png"
    try:
        page.goto(page_url(args.base_url, "run", sid), wait_until="networkidle", timeout=30000)
        problems += _settle(page, SHELL + PAGES["run"]["required"], args.settle)
        page.screenshot(path=str(shot), full_page=True)
    except Exception as exc:
        problems.append(f"{type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
    problems += rec.problems([])
    report.add("run (played)", 1280, "light", problems, screenshot=shot.name if shot.exists() else None)
    page.close()


def check_markdown(ctx, report: Report, args) -> None:
    page = ctx.new_page()
    rec = Recorder(page)
    problems, result = [], None
    try:
        page.goto(page_url(args.base_url, "ask", ""), wait_until="networkidle", timeout=30000)
        if not page.evaluate("() => !!(window.marked && window.DOMPurify && window.Borg && window.Borg.markdown)"):
            raise AssertionError("marked, DOMPurify or Borg.markdown is not loaded on Ask")
        result = page.evaluate(_XSS_JS, XSS_MARKDOWN)
        page.wait_for_timeout(1500)          # let the kept <img> load (and fail) before judging
        problems += [f"sanitiser: {b}" for b in result["bad"]]
        if result["imgs"] < 1:
            problems.append("sanitiser probe: the <img> was dropped entirely, so the onerror test proved nothing")
        fired = page.evaluate("() => window.__borgXss")
        if fired is not None:
            problems.append(f"sanitiser: injected script ran (window.__borgXss = {fired!r})")
    except Exception as exc:
        problems.append(f"markdown: {type(exc).__name__}: {' '.join(str(exc).split())[:300]}")
    problems += classify(rec.responses, rec.request_failures, rec.console_errors, rec.page_errors,
                         expected=[], tolerated=TOLERATED | XSS_ALLOWED)
    report.add("ask (markdown)", 1280, "light", problems, sanitised=(result or {}).get("html"))
    page.close()


def replay_status(base: str) -> dict:
    with urllib.request.urlopen(base + "/replay/status", timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def main(argv: Optional[list] = None) -> int:
    args = parse_args(argv)
    bad = spec_problems()
    if bad:
        for b in bad:
            print("FAIL spec: " + b)
        return 1
    args.out = Path(args.out)
    args.out.mkdir(parents=True, exist_ok=True)
    try:
        status = replay_status(args.base_url)
    except Exception as exc:
        print(f"FAIL server: {args.base_url}/replay/status could not be read ({exc})")
        return 2
    if status.get("mode") != "replay":
        print(f"FAIL server: not in replay mode (mode={status.get('mode')!r}); start it with BORG_DEMO_MODE=replay")
        return 2
    runs = status.get("runs") or []
    run = next((r for r in runs if str(r.get("job")) == str(args.job)), None)
    if run is None:
        print(f"FAIL server: no recorded run for job {args.job} (have {[r.get('job') for r in runs]})")
        return 2

    from playwright.sync_api import sync_playwright  # noqa: PLC0415  (CI only: the ui_test extra)

    report = Report(args.out)
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            for width in WIDTHS:
                for theme in THEMES:
                    ctx = _new_context(browser, width, theme)
                    for name in args.pages:
                        check_page(ctx, report, args, name, width, theme, run["run_id"])
                    ctx.close()
            ctx = _new_context(browser, 1280, "light")
            if "ask" in args.pages:
                sid = play_run(ctx, report, args, run)
                if sid and "run" in args.pages:
                    check_played_run(ctx, report, args, sid)
                check_markdown(ctx, report, args)
            ctx.close()
        finally:
            browser.close()

    (args.out / "report.json").write_text(json.dumps({
        "base_url": args.base_url, "job": args.job, "speed": args.speed, "failures": report.failures,
        "checks": report.checks}, indent=2), encoding="utf-8")
    total = len(report.checks)
    print(f"{total - sum(not c['ok'] for c in report.checks)} of {total} checks passed; "
          f"{report.failures} failure(s). Screenshots and report.json in {args.out}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
