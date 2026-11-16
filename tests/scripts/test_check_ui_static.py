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


def write(tmp_path, name, text):
    p = tmp_path / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_good_page_passes(tmp_path):
    write(tmp_path, "ask.html", GOOD)
    assert check.run(tmp_path) == 0


def test_bad_page_fails(tmp_path, capsys):
    write(tmp_path, "ask.html", GOOD)
    write(tmp_path, "keys_new.html", BAD)
    assert check.run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "keys_new.html" in out and "app.css" in out


def test_long_style_block_fails(tmp_path):
    style = "<style>\n" + "a{}\n" * 16 + "</style>"
    write(tmp_path, "logs_new.html", GOOD.replace("</head>", style + "</head>"))
    assert check.run(tmp_path) == 1


def test_main_exit_code(tmp_path):
    write(tmp_path, "ask.html", BAD)
    assert check.main(["x", str(tmp_path)]) == 1


def test_every_page_is_scanned_not_a_fixed_list(tmp_path):
    write(tmp_path, "brand_new_page.html", OLD)
    assert check.run(tmp_path) == 1


def test_comment_only_links_fail(tmp_path):
    page = GOOD.replace("<link", "<!-- <link").replace('app.css">', 'app.css"> -->')
    page = page.replace("<script", "<!-- <script").replace("</script>", "</script> -->")
    write(tmp_path, "ask.html", page)
    assert check.run(tmp_path) == 1


def test_rel_preload_fails(tmp_path):
    write(tmp_path, "ask.html", GOOD.replace('rel="stylesheet"', 'rel="preload"'))
    assert check.run(tmp_path) == 1


def test_attribute_order_is_free(tmp_path):
    page = GOOD.replace('<link rel="stylesheet" href="assets/app.css">', '<link href="assets/app.css" rel="stylesheet">')
    page = page.replace('<script src="assets/app.js">', '<script defer src="assets/app.js">')
    write(tmp_path, "ask.html", page)
    assert check.run(tmp_path) == 0


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
    ],
)
def test_forbidden_content_fails(tmp_path, extra):
    write(tmp_path, "ask.html", GOOD.replace("</body>", extra + "</body>"))
    assert check.run(tmp_path) == 1


def test_alert_word_without_call_is_fine(tmp_path):
    write(tmp_path, "ask.html", GOOD.replace("</body>", "<p>alerts are inline</p></body>"))
    assert check.run(tmp_path) == 0


def test_external_import_in_css_fails(tmp_path):
    write(tmp_path, "ask.html", GOOD)
    write(tmp_path, "assets/app.css", '@import "https://fonts.example.com/a.css";\nbody{}')
    assert check.run(tmp_path) == 1


def test_external_url_in_css_fails(tmp_path):
    write(tmp_path, "ask.html", GOOD)
    write(tmp_path, "assets/app.css", "body{background:url(//x.example.com/a.png)}")
    assert check.run(tmp_path) == 1


def test_alert_in_js_fails_and_vendor_is_skipped(tmp_path):
    write(tmp_path, "ask.html", GOOD)
    write(tmp_path, "assets/app.js", "window.alert (1);")
    assert check.run(tmp_path) == 1
    (tmp_path / "assets" / "app.js").write_text("var a = 1;", encoding="utf-8")
    write(tmp_path, "assets/vendor/lib.min.js", "alert(1); https://cdn.example.com")
    assert check.run(tmp_path) == 0


def test_allow_listed_old_page_passes_default_fails_strict(tmp_path, capsys):
    write(tmp_path, "chat.html", OLD)
    assert check.run(tmp_path) == 0
    assert "allowed legacy: chat.html" in capsys.readouterr().out
    assert check.run(tmp_path, strict=True) == 1


def test_allow_listed_name_that_links_app_js_is_checked_fully(tmp_path):
    write(tmp_path, "run.html", GOOD.replace('rel="stylesheet"', 'rel="preload"'))
    assert check.run(tmp_path) == 1
    write(tmp_path, "run.html", GOOD)
    assert check.run(tmp_path) == 0


def test_main_strict_flag(tmp_path):
    write(tmp_path, "chat.html", OLD)
    assert check.main(["x", str(tmp_path)]) == 0
    assert check.main(["x", "--strict", str(tmp_path)]) == 1


def test_real_tree_passes():
    assert check.main(["x"]) == 0
