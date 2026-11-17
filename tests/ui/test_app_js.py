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
        const p = h.Borg.api('/api/x').catch((e) => { status = e.status; });
        await new Promise((r) => setTimeout(r, 0));
        const slot0 = h.doc.body.children.find((c) => c.attrs.id === 'notice-slot');
        slot0.children[0].children[3].click(); // Cancel on the inline token form
        await p;
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


def _form_js():
    return """
        const slotOf = (h) => h.doc.body.children.find((c) => c.attrs.id === 'notice-slot');
        const tick = () => new Promise((r) => setTimeout(r, 0));
    """


def test_401_asks_for_the_token_once_keeps_it_and_retries(tmp_path):
    out = run_js(
        tmp_path,
        _form_js() + """
        const calls = [];
        const fetch = async (url, init) => {
          const a = init.headers['Authorization'] || null;
          calls.push(a);
          const ok = a === 'Bearer good';
          return {ok, status: ok ? 200 : 401, headers: {get: () => 'application/json'},
                  json: async () => (ok ? {done: true} : {detail: {error: 'BRT-API-045'}}), text: async () => ''};
        };
        const h = H.load({fetch});
        let alerts = 0; h.window.alert = () => { alerts++; };
        const first = h.Borg.api('/keys', {method: 'POST', json: {}});
        const second = h.Borg.api('/config', {method: 'POST', json: {}});
        await tick(); await tick();
        const slot = slotOf(h);
        const forms = slot.children.length;
        const box = slot.children[0];
        box.children[1].value = 'good';
        box.children[2].click(); // Save token
        const r1 = await first, r2 = await second;
        return {forms, r1, r2, calls, stored: h.window.sessionStorage.getItem('borg_admin_token'),
                left: slot.children.length, alerts};
        """,
    )
    assert out["forms"] == 1 and out["left"] == 0 and out["alerts"] == 0
    assert out["r1"] == {"done": True} and out["r2"] == {"done": True}
    assert out["stored"] == "good"
    assert out["calls"] == [None, None, "Bearer good", "Bearer good"]


def test_wrong_token_is_dropped_and_retry_happens_only_once(tmp_path):
    out = run_js(
        tmp_path,
        _form_js() + """
        let n = 0;
        const fetch = async () => { n++; return {ok: false, status: 401, headers: {get: () => 'application/json'},
          json: async () => ({detail: 'no'}), text: async () => ''}; };
        const h = H.load({fetch});
        h.window.sessionStorage.setItem('borg_admin_token', 'old');
        let status = null;
        const p = h.Borg.api('/keys', {method: 'POST', json: {}}).catch((e) => { status = e.status; });
        await tick(); await tick();
        const box = slotOf(h).children[0];
        box.children[1].value = 'typed';
        box.children[2].click();
        await p;
        return {n, status, stored: h.window.sessionStorage.getItem('borg_admin_token')};
        """,
    )
    assert out["n"] == 2 and out["status"] == 401  # first try, one retry, then stop
    assert out["stored"] == ""  # the wrong token is not kept


def test_403_without_a_server_token_explains_remote_admin(tmp_path):
    out = run_js(
        tmp_path,
        """
        const fetch = async () => ({ok: false, status: 403, headers: {get: () => 'application/json'},
          json: async () => ({detail: {error: 'BRT-API-046', message: 'x'}}), text: async () => ''});
        const h = H.load({fetch});
        let alerts = 0; h.window.alert = () => { alerts++; };
        let msg = null;
        try { await h.Borg.api('/config', {method: 'POST', json: {}}); } catch (e) { msg = e.message; }
        const slot = h.doc.body.children.find((c) => c.attrs.id === 'notice-slot');
        return {msg, shown: slot.children[0].textContent, alerts};
        """,
    )
    assert "BORG_ADMIN_TOKEN" in out["msg"] and out["shown"] == out["msg"] and out["alerts"] == 0


def test_download_goes_through_api_with_token_retry_and_saves_a_blob(tmp_path):
    out = run_js(
        tmp_path,
        _form_js() + """
        const calls = [];
        const fetch = async (url, init) => {
          const a = init.headers['Authorization'] || null;
          calls.push([url, a]);
          const ok = a === 'Bearer good';
          return {ok, status: ok ? 200 : 401, headers: {get: () => (ok ? 'application/x-ndjson' : 'application/json')},
                  json: async () => ({detail: {error: 'BRT-API-045'}}), text: async () => '{"a":1}'};
        };
        const h = H.load({fetch});
        let alerts = 0; h.window.alert = () => { alerts++; };
        const made = [], saved = [];
        h.window.URL.createObjectURL = (b) => { made.push(b); return 'blob:x'; };
        h.window.URL.revokeObjectURL = () => {};
        const realCreate = h.doc.createElement;
        h.doc.createElement = (t) => { const e = realCreate(t); if (t === 'a') saved.push(e); return e; };
        const p = h.Borg.download('/logs/export?limit=1000', 'borg_route_logs.jsonl', 'application/x-ndjson');
        await tick(); await tick();
        const box = slotOf(h).children[0];
        box.children[1].value = 'good';
        box.children[2].click();
        await p;
        return {calls, alerts, anchors: saved.map((e) => [e.attrs.href, e.attrs.download]),
                size: made.length ? made[0].size : null, type: made.length ? made[0].type : null};
        """,
    )
    assert out["alerts"] == 0
    assert out["calls"] == [["/logs/export?limit=1000", None], ["/logs/export?limit=1000", "Bearer good"]]
    assert out["anchors"] == [["blob:x", "borg_route_logs.jsonl"]]
    assert out["size"] == 7 and out["type"] == "application/x-ndjson"


# ---- markdown never loads from another site (4.2 review) ----
# Node has no DOM, so DOMPurify itself cannot run here. These tests use the real marked and the
# real app.js hook; a stand-in sanitiser feeds each <img>/<a> of marked's output through the
# hook DOMPurify would run ("afterSanitizeAttributes") and writes the result back. The
# Content-Security-Policy header (tests/api/test_csp.py) is the second line.

STANDIN = r"""
const {Borg, window} = H.load({vendor: ['marked.min.js']});
const hooks = [];
window.DOMPurify = {
  isSupported: true,
  addHook(name, fn) { hooks.push([name, fn]); },
  sanitize(html) {
    return html.replace(/<(img|a)\b([^>]*)>/g, (m, tag, rest) => {
      const el = new H.FakeEl(tag);
      rest.replace(/([\w:-]+)="([^"]*)"/g, (mm, k, v) => { el.setAttribute(k, v); return mm; });
      hooks.filter(h => h[0] === 'afterSanitizeAttributes').forEach(h => h[1](el));
      return '<' + tag + Object.keys(el.attrs).map(k => ' ' + k + '="' + el.attrs[k] + '"').join('') + '>';
    });
  },
};
"""


def test_markdown_drops_an_external_image_src(tmp_path):
    out = run_js(tmp_path, STANDIN + "return [Borg.markdown('![](https://x.example/p.png)'), hooks.length];")
    html, hooks = out
    assert "<img" in html and "https://x.example" not in html and "src=" not in html
    assert hooks == 1


def test_markdown_keeps_same_origin_and_data_images_and_registers_the_hook_once(tmp_path):
    out = run_js(
        tmp_path,
        STANDIN + "Borg.markdown('a'); Borg.markdown('b');"
        "return [Borg.markdown('![](/ui/assets/logo.svg) ![](data:image/png;base64,AAAA) "
        "![](http://127.0.0.1:8000/x.png)'), hooks.length];",
    )
    html, hooks = out
    assert 'src="/ui/assets/logo.svg"' in html
    assert 'src="data:image/png;base64,AAAA"' in html
    assert 'src="http://127.0.0.1:8000/x.png"' in html
    assert hooks == 1


def test_markdown_turns_an_external_link_into_plain_text(tmp_path):
    out = run_js(
        tmp_path,
        STANDIN + "return [Borg.markdown('[site](https://evil.example/a) [back](#top) [x](javascript:alert(1)) [ok](/ui/runs.html)')];",
    )
    html = out[0]
    assert "evil.example" not in html and "javascript:" not in html
    assert 'href="#top"' in html and 'href="/ui/runs.html"' in html


def test_the_hook_drops_srcset_and_poster_that_leave_the_origin(tmp_path):
    out = run_js(
        tmp_path,
        """
        const {Borg} = H.load();
        const img = new H.FakeEl('img');
        img.setAttribute('srcset', '/a.png 1x, https://x.example/b.png 2x');
        img.setAttribute('poster', 'https://x.example/p.png');
        img.setAttribute('src', 'data:text/html;base64,AAAA');
        Borg.dropExternalLoads(img);
        const ok = new H.FakeEl('img');
        ok.setAttribute('srcset', '/a.png 1x, /b.png 2x');
        Borg.dropExternalLoads(ok);
        return [img.attrs, ok.attrs];
        """,
    )
    assert out[0] == {}
    assert out[1] == {"srcset": "/a.png 1x, /b.png 2x"}
