import hashlib
import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "check_ui_static", Path(__file__).resolve().parents[2] / "scripts" / "check_ui_static.py"
)
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

GOOD = (
    '<!doctype html><html><head><link rel="stylesheet" href="assets/app.css"></head>'
    '<body><main id="page"></main><script src="assets/app.js"></script></body></html>'
)
BAD = (
    '<!doctype html><html><head><link rel="icon" href="assets/logo.svg"><script src="https://cdn.example.com/x.js"></script></head>'
    "<body><script>alert('x')</script></body></html>"
)
OLD = "<!doctype html><html><body><h1>old page</h1></body></html>"


def write(ui, name, text):
    p = ui / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


@pytest.fixture
def ui(tmp_path):
    """A ui dir that already has the two required clean assets."""
    write(tmp_path, "assets/app.css", "body{margin:0}")
    write(tmp_path, "assets/app.js", "var a = 1;")
    return tmp_path


def sha(text):
    return hashlib.sha256(text.replace("\r\n", "\n").encode("utf-8")).hexdigest()


def test_good_page_passes(ui):
    write(ui, "ask.html", GOOD)
    assert check.run(ui) == 0


def test_bad_page_fails(ui, capsys):
    write(ui, "ask.html", GOOD)
    write(ui, "keys_new.html", BAD)
    assert check.run(ui) == 1
    out = capsys.readouterr().out
    assert "keys_new.html" in out and "app.css" in out


def test_long_style_block_fails(ui):
    style = "<style>\n" + "a{}\n" * 16 + "</style>"
    write(ui, "logs_new.html", GOOD.replace("</head>", style + "</head>"))
    assert check.run(ui) == 1


def test_main_exit_code(ui):
    write(ui, "ask.html", BAD)
    assert check.main(["x", str(ui)]) == 1


def test_every_page_is_scanned_not_a_fixed_list(ui):
    write(ui, "brand_new_page.html", OLD)
    assert check.run(ui) == 1


def test_comment_only_links_fail(ui):
    page = GOOD.replace("<link", "<!-- <link").replace('app.css">', 'app.css"> -->')
    page = page.replace("<script", "<!-- <script").replace("</script>", "</script> -->")
    write(ui, "ask.html", page)
    assert check.run(ui) == 1


def test_rel_preload_fails(ui):
    write(ui, "ask.html", GOOD.replace('rel="stylesheet"', 'rel="preload"'))
    assert check.run(ui) == 1


def test_attribute_order_is_free(ui):
    page = GOOD.replace('<link rel="stylesheet" href="assets/app.css">', '<link href="assets/app.css" rel="stylesheet">')
    page = page.replace('<script src="assets/app.js">', '<script defer src="assets/app.js">')
    write(ui, "ask.html", page)
    assert check.run(ui) == 0


@pytest.mark.parametrize("href", ["./assets/app.css", "assets/app.css?v=2", "/ui/assets/app.css", "assets/app.css#x"])
def test_css_href_variants_accepted(ui, href):
    write(ui, "ask.html", GOOD.replace('"assets/app.css"', f'"{href}"'))
    assert check.run(ui) == 0


@pytest.mark.parametrize("src", ["./assets/app.js", "assets/app.js?v=2", "/ui/assets/app.js", "assets/app.js#x"])
def test_js_src_variants_accepted(ui, src):
    write(ui, "ask.html", GOOD.replace('"assets/app.js"', f'"{src}"'))
    assert check.run(ui) == 0


def test_lookalike_asset_path_rejected(ui):
    write(ui, "ask.html", GOOD.replace('"assets/app.js"', '"xassets/app.js"'))
    assert check.run(ui) == 1


def test_stylesheet_rel_token_among_others(ui):
    write(ui, "ask.html", GOOD.replace('rel="stylesheet"', 'rel="alternate stylesheet"'))
    assert check.run(ui) == 0


@pytest.mark.parametrize(
    "extra",
    [
        '<script src="https://CDN.Example.com/x.js"></script>',
        '<script src="//unpkg.com/x"></script>',
        '<script src="https://cdnjs.cloudflare.com/x.js"></script>',
        '<link rel="stylesheet" href="HTTP://fonts.example.com/a.css">',
        '<script>window.alert ("x")</script>',
        "<script>ALERT\n(1)</script>",
        "<style>@import url('https://fonts.example.com/a.css');</style>",
        '<img srcset="a.png 1x, https://x.example.com/b.png 2x">',
        '<video poster="//x.example.com/p.png"></video>',
        '<object data="https://x.example.com/o.swf"></object>',
        '<form action="https://x.example.com/post"></form>',
        '<button formaction=" https://x.example.com/post">go</button>',
        '<button onclick="alert (1)">go</button>',
        "<script>import('https://x.example.com/m.js')</script>",
        "<script>fetch('//x.example.com/d')</script>",
    ],
)
def test_forbidden_content_fails(ui, extra):
    write(ui, "ask.html", GOOD.replace("</body>", extra + "</body>"))
    assert check.run(ui) == 1


def test_alert_word_without_call_is_fine(ui):
    write(ui, "ask.html", GOOD.replace("</body>", "<p>alerts are inline</p></body>"))
    assert check.run(ui) == 0


def test_prose_cdn_is_fine(ui):
    write(ui, "ask.html", GOOD.replace("</body>", "<p>Nothing loads from a CDN. No unpkg either.</p></body>"))
    assert check.run(ui) == 0


def test_inline_svg_xmlns_is_fine(ui):
    svg = '<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink"><use/></svg>'
    write(ui, "ask.html", GOOD.replace("</body>", svg + "</body>"))
    assert check.run(ui) == 0


def test_data_src_is_not_src(ui):
    # a data-src before the real src must not hide or fake the real attribute
    page = GOOD.replace('<script src="assets/app.js">', '<script data-src="assets/app.js" src="https://x.example.com/a.js">')
    write(ui, "ask.html", page)
    assert check.run(ui) == 1
    page = GOOD.replace('<script src="assets/app.js">', '<script data-src="https://x.example.com/a.js" src="assets/app.js">')
    write(ui, "ask.html", page)
    assert check.run(ui) == 0
    # data-src alone is not a link to app.js
    write(ui, "ask.html", GOOD.replace('<script src="assets/app.js">', '<script data-src="assets/app.js">'))
    assert check.run(ui) == 1


def test_comment_marker_inside_script_string_does_not_hide_real_tag(ui):
    page = GOOD.replace(
        "</body>",
        "<script>var s = '<!--';</script>"
        '<script src="https://x.example.com/evil.js"></script></body>',
    )
    write(ui, "ask.html", page)
    assert check.run(ui) == 1


def test_external_import_in_css_fails(ui):
    write(ui, "ask.html", GOOD)
    write(ui, "assets/app.css", '@import "https://fonts.example.com/a.css";\nbody{}')
    assert check.run(ui) == 1


def test_external_url_in_css_fails(ui):
    write(ui, "ask.html", GOOD)
    write(ui, "assets/app.css", "body{background:url(//x.example.com/a.png)}")
    assert check.run(ui) == 1


def test_alert_in_js_fails_and_vendor_is_skipped(ui):
    write(ui, "ask.html", GOOD)
    write(ui, "assets/app.js", "window.alert (1);")
    assert check.run(ui) == 1
    (ui / "assets" / "app.js").write_text("var a = 1;", encoding="utf-8")
    write(ui, "assets/vendor/lib.min.js", "alert(1); https://cdn.example.com")
    write(ui, "assets/vendor/lib.css", "@import 'https://cdn.example.com/a.css';")
    assert check.run(ui) == 0


def test_other_asset_js_with_external_fetch_and_alert_fails(ui, capsys):
    write(ui, "ask.html", GOOD)
    write(ui, "assets/ask.js", "fetch('https://api.example.com/x'); alert(1);")
    assert check.run(ui) == 1
    assert "assets/ask.js" in capsys.readouterr().out


def test_other_asset_css_with_external_import_fails(ui, capsys):
    write(ui, "ask.html", GOOD)
    write(ui, "assets/run.css", "@import url(https://fonts.example.com/a.css);")
    assert check.run(ui) == 1
    assert "assets/run.css" in capsys.readouterr().out


def test_nested_asset_is_scanned(ui):
    write(ui, "ask.html", GOOD)
    write(ui, "assets/lib/deep.js", "import x from 'https://x.example.com/m.js';")
    assert check.run(ui) == 1


@pytest.mark.parametrize("missing", ["app.js", "app.css"])
def test_missing_required_asset_fails(ui, missing, capsys):
    write(ui, "ask.html", GOOD)
    (ui / "assets" / missing).unlink()
    assert check.run(ui) == 1
    assert f"assets/{missing}" in capsys.readouterr().out


@pytest.mark.parametrize(
    "js",
    [
        "import x from 'https://x.example.com/m.js';",
        "import 'https://x.example.com/m.js';",
        "const m = await import('//x.example.com/m.js');",
        'fetch("//x.example.com/d");',
    ],
)
def test_external_js_loads_fail(ui, js):
    write(ui, "assets/app.js", js)
    assert check.run(ui) == 1


def test_local_js_import_and_fetch_pass(ui):
    write(ui, "assets/app.js", "import x from './m.js'; fetch('/api/run');")
    assert check.run(ui) == 0


def test_legacy_page_matching_hash_is_exempt_default_fails_strict(ui, monkeypatch, capsys):
    monkeypatch.setattr(check, "LEGACY_ALLOW_LIST", {"chat.html": sha(OLD)})
    write(ui, "chat.html", OLD)
    assert check.run(ui) == 0
    assert "allowed legacy: chat.html" in capsys.readouterr().out
    assert check.run(ui, strict=True) == 1


def test_legacy_hash_ignores_crlf(ui, monkeypatch):
    monkeypatch.setattr(check, "LEGACY_ALLOW_LIST", {"chat.html": sha(OLD + "\n")})
    (ui / "chat.html").write_bytes((OLD + "\r\n").encode("utf-8"))
    assert check.run(ui) == 0


def test_edited_legacy_page_loses_exemption(ui, monkeypatch):
    monkeypatch.setattr(check, "LEGACY_ALLOW_LIST", {"chat.html": sha(OLD)})
    edited = OLD.replace("</body>", '<script src="https://cdn.example.com/x.js"></script></body>')
    write(ui, "chat.html", edited)
    assert check.run(ui) == 1


def test_edited_legacy_page_that_is_good_passes(ui, monkeypatch):
    monkeypatch.setattr(check, "LEGACY_ALLOW_LIST", {"run.html": sha(OLD)})
    write(ui, "run.html", GOOD)
    assert check.run(ui) == 0
    write(ui, "run.html", GOOD.replace('rel="stylesheet"', 'rel="preload"'))
    assert check.run(ui) == 1


def test_main_strict_flag(ui, monkeypatch):
    monkeypatch.setattr(check, "LEGACY_ALLOW_LIST", {"chat.html": sha(OLD)})
    write(ui, "chat.html", OLD)
    assert check.main(["x", str(ui)]) == 0
    assert check.main(["x", "--strict", str(ui)]) == 1


def test_real_tree_passes():
    assert check.main(["x"]) == 0


def test_allow_list_is_empty_and_strict_passes():
    # Since 4.2 (chat.html archived, run.html rewritten) no old page is exempt, so
    # CI's --strict run (5.6) gives the same result as the default run.
    assert check.LEGACY_ALLOW_LIST == {}
    assert check.main(["x", "--strict"]) == 0
