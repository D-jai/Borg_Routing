"""Behaviour tests for ui/assets/app.js, run under node with a minimal fake DOM (harness.js).

Skipped only when node is not on PATH. The DOMPurify sanitising of real HTML needs a real
DOM; that case is covered by 4.6's browser check. Here we test the escaped fallback path.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="node is not on PATH; app.js behaviour tests need it")

HARNESS = (Path(__file__).parent / "harness.js").as_posix()


def run_js(tmp_path, body):
    """Run `body` (an async JS function body that may `return` a JSON-able value)."""
    script = tmp_path / "scenario.js"
    script.write_text(
        f"const H = require({json.dumps(HARNESS)});\n"
        f"(async () => {{ {body} }})().then(\n"
        "  (r) => { console.log(JSON.stringify(r === undefined ? null : r)); },\n"
        "  (e) => { console.error(e && e.stack || String(e)); process.exit(1); });\n",
        encoding="utf-8",
    )
    env = dict(os.environ, TZ="America/Toronto")
    p = subprocess.run([NODE, str(script)], capture_output=True, text=True, env=env, timeout=60)
    assert p.returncode == 0, p.stderr
    return json.loads(p.stdout.strip().splitlines()[-1])


def call(tmp_path, fn, args):
    return run_js(
        tmp_path,
        f"const {{Borg}} = H.load(); return Borg[{json.dumps(fn)}](...{json.dumps(args)});",
    )


# ---- money ----

@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "$0.00"),
        (12.3456, "$12.35"),
        (0.5, "$0.500"),
        (0.0123456, "$0.0123"),
        (0.00000123, "$0.00000123"),
        (0.00000000012345, "$1.23e-10"),
        (0.9996, "$1.00"),
        (1, "$1.00"),
        (-0.0042, "-$0.00420"),
        ("0.25", "$0.250"),
    ],
)
def test_money_values(tmp_path, value, expected):
    assert call(tmp_path, "money", [value]) == expected


def test_money_never_zero_for_nonzero(tmp_path):
    for v in (1e-3, 4.9e-5, 1e-7, 3.3e-8, 2e-9):
        out = call(tmp_path, "money", [v])
        assert out.strip("$.0e-") != "", out
        assert float(out.lstrip("$")) > 0


def test_money_non_numbers_give_dash(tmp_path):
    out = run_js(
        tmp_path,
        "const {Borg} = H.load(); return [true, false, [], '', '  ', null, undefined, NaN, {}, 'abc', Infinity]"
        ".map((v) => Borg.money(v));",
    )
    assert out == ["-"] * 11


# ---- duration ----

@pytest.mark.parametrize(
    "value,expected",
    [
        (0, "0 s"),
        (3.26, "3.3 s"),
        (42.4, "42 s"),
        (59.4, "59 s"),
        (59.5, "1 min 0 s"),
        (59.9, "1 min 0 s"),
        (60, "1 min 0 s"),
        (125, "2 min 5 s"),
        (3725, "1 h 2 min"),
        (-1, "-"),
        ("", "-"),
        (None, "-"),
        (True, "-"),
    ],
)
def test_duration(tmp_path, value, expected):
    assert call(tmp_path, "duration", [value]) == expected


# ---- dates (TZ=America/Toronto) ----

@pytest.mark.parametrize(
    "value,expected",
    [
        ("2026-11-16", "16-Nov-2026"),  # date only: no invented time
        ("2026-11-16 10:11:20", "16-Nov-2026 05:11"),  # SQLite, UTC -> EST
        ("2026-07-16 10:11:20", "16-Jul-2026 06:11"),  # EDT
        ("2026-11-16T10:11:20Z", "16-Nov-2026 05:11"),
        ("2026-11-16T10:11:20-05:00", "16-Nov-2026 10:11"),
        ("2026-11-16T10:11:20.250", "16-Nov-2026 05:11"),
        (0, "-"),
        (None, "-"),
        ("", "-"),
        ("not a date", "-"),
        (True, "-"),
    ],
)
def test_date_time(tmp_path, value, expected):
    assert call(tmp_path, "dateTime", [value]) == expected


def test_time_only(tmp_path):
    assert call(tmp_path, "timeOnly", ["2026-11-16 10:11:20"]) == "05:11"
    assert call(tmp_path, "timeOnly", ["2026-11-16"]) == "-"
    assert call(tmp_path, "timeOnly", [0]) == "-"


# ---- markdown ----

ATTACKS = "<img src=x onerror=alert(1)>\n\n[x](javascript:alert(1))"


def test_markdown_fallback_escapes_without_libraries(tmp_path):
    out = run_js(tmp_path, f"const {{Borg}} = H.load(); return Borg.markdown({json.dumps(ATTACKS)});")
    assert "<img" not in out and "<a " not in out
    assert "&lt;img" in out


def test_markdown_escapes_when_purify_cannot_run(tmp_path):
    # Node has no DOM, so DOMPurify reports isSupported=false; the output must still be inert.
    out = run_js(
        tmp_path,
        "const {Borg, window} = H.load({vendor: ['marked.min.js', 'purify.min.js']});"
        f"return {{loaded: !!window.marked, supported: window.DOMPurify && window.DOMPurify.isSupported, "
        f"html: Borg.markdown({json.dumps(ATTACKS)})}};",
    )
    assert out["loaded"] is True
    assert "<img" not in out["html"] and "<a " not in out["html"]


# Real DOMPurify sanitising of <img onerror> and javascript: links needs a real DOM (no jsdom
# without new dependencies): covered by the 4.6 browser check.


# ---- api() ----

def test_api_sends_token_only_to_same_origin(tmp_path):
    out = run_js(
        tmp_path,
        """
        const calls = [];
        const fetch = async (url, init) => { calls.push({url, auth: init.headers.Authorization || null});
          return {ok: true, status: 200, headers: {get: () => 'application/json'}, json: async () => ({ok: 1}), text: async () => ''}; };
        const {Borg} = H.load({fetch});
        Borg.setToken('secret');
        await Borg.api('/api/x');
        await Borg.api('api/y');
        await Borg.api('http://127.0.0.1:8000/api/z');
        await Borg.api('https://evil.example.com/steal');
        await Borg.api('//evil.example.com/steal');
        await Borg.api('http://127.0.0.1:9999/other-port');
        return calls;
        """,
    )
    assert [c["auth"] for c in out] == ["Bearer secret", "Bearer secret", "Bearer secret", None, None, None]


def test_api_failure_shows_inline_notice_not_alert(tmp_path):
    out = run_js(
        tmp_path,
        """
        let alerts = 0;
        const fetch = async () => ({ok: false, status: 401, headers: {get: () => 'application/json'},
          json: async () => ({detail: 'no'}), text: async () => ''});
        const h = H.load({fetch});
        h.window.alert = () => { alerts++; };
        let status = null;
        try { await h.Borg.api('/api/x'); } catch (e) { status = e.status; }
        const slot = h.doc.body.children.find((c) => c.attrs.id === 'notice-slot');
        const notice = slot && slot.children[0];
        return {alerts, status, text: notice && notice.textContent, cls: notice && notice.className};
        """,
    )
    assert out["alerts"] == 0 and out["status"] == 401
    assert "admin token" in out["text"] and "bad" in out["cls"]


def test_api_unreachable_server_gives_inline_notice(tmp_path):
    out = run_js(
        tmp_path,
        """
        const fetch = async () => { throw new TypeError('network'); };
        const h = H.load({fetch});
        let err = null;
        try { await h.Borg.api('/api/x'); } catch (e) { err = e; }
        return {status: err.status, message: err.message};
        """,
    )
    assert out["status"] == 0 and "Could not reach the server" in out["message"]


# ---- theme / storage ----

def test_theme_storage_that_throws_is_survived(tmp_path):
    out = run_js(
        tmp_path,
        """
        const h = H.load({throwingStorage: true});   // load runs applySavedTheme with throwing getItem
        return {
          get: h.Borg.storage.get('localStorage', 'borg_theme'),
          set: h.Borg.storage.set('localStorage', 'borg_theme', 'dark'),
          token: h.Borg.getToken(),
          setToken: h.Borg.setToken('x'),
        };
        """,
    )
    assert out == {"get": None, "set": False, "token": None, "setToken": False}


def test_saved_theme_is_applied_and_written(tmp_path):
    out = run_js(
        tmp_path,
        """
        const h = H.load();
        h.Borg.storage.set('localStorage', 'borg_theme', 'dark');
        return {get: h.Borg.storage.get('localStorage', 'borg_theme')};
        """,
    )
    assert out == {"get": "dark"}
