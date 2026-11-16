import importlib.util
from pathlib import Path

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


def test_good_page_passes_and_missing_pages_are_skipped(tmp_path, capsys):
    (tmp_path / "ask.html").write_text(GOOD, encoding="utf-8")
    assert check.run(tmp_path) == 0
    assert "skipped" in capsys.readouterr().out


def test_bad_page_fails(tmp_path, capsys):
    (tmp_path / "ask.html").write_text(GOOD, encoding="utf-8")
    (tmp_path / "keys.html").write_text(BAD, encoding="utf-8")
    assert check.run(tmp_path) == 1
    out = capsys.readouterr().out
    assert "keys.html" in out and "app.css" in out and "cdn." in out and "alert(" in out


def test_long_style_block_fails(tmp_path):
    style = "<style>\n" + "a{}\n" * 16 + "</style>"
    (tmp_path / "logs.html").write_text(GOOD.replace("</head>", style + "</head>"), encoding="utf-8")
    assert check.run(tmp_path) == 1


def test_main_exit_code(tmp_path):
    (tmp_path / "ask.html").write_text(BAD, encoding="utf-8")
    assert check.main(["x", str(tmp_path)]) == 1


def test_legacy_page_skipped_unless_strict(tmp_path):
    (tmp_path / "keys.html").write_text(BAD.replace("assets/logo.svg", "x.svg"), encoding="utf-8")
    assert check.run(tmp_path) == 0
    assert check.run(tmp_path, strict=True) == 1
