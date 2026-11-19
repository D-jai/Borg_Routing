"""Work order 4.6: the pure parts of scripts/check_ui.py, and the CI job that runs it.

The browser part runs only in CI (planner ruling 38a). These tests check what can be checked
without a browser: the arguments, the per-page lists being well-formed and written from the
pages' own code, the expected refusals matching what replay really refuses, the failure
classifier, and the workflow job's shape.
"""
import importlib.util
import re
import tomllib
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from main import app
from src.api import replay

ROOT = Path(__file__).resolve().parents[2]
UI = ROOT / "ui"
_SPEC = importlib.util.spec_from_file_location("check_ui", ROOT / "scripts" / "check_ui.py")
check = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(check)

SHARED_JS = ("app.js", "admin.js", "workspace.js")


def _code(page: str) -> str:
    parts = [UI / f"{page}.html", UI / "assets" / "pages" / f"{page}.js"]
    parts += [UI / "assets" / n for n in SHARED_JS]
    return "\n".join(p.read_text(encoding="utf-8") for p in parts if p.exists())


# --- arguments ----------------------------------------------------------------------------------

def test_defaults():
    a = check.parse_args([])
    assert a.base_url == "http://127.0.0.1:8000" and a.speed is None and a.job == "1"
    assert a.pages == list(check.PAGES)


def test_speed_base_url_and_pages_are_parsed():
    a = check.parse_args(["--base-url", "http://h:9/", "--speed", "50", "--pages", "ask, run"])
    assert a.base_url == "http://h:9" and a.speed == 50.0 and a.pages == ["ask", "run"]


@pytest.mark.parametrize("argv", [["--speed", "0"], ["--speed", "-2"], ["--speed", "fast"], ["--pages", "nope"]])
def test_bad_arguments_are_refused(argv, capsys):
    with pytest.raises(SystemExit) as e:
        check.parse_args(argv)
    assert e.value.code == 2


# --- the per-page lists -------------------------------------------------------------------------

def test_one_list_per_page_in_ui():
    assert set(check.PAGES) == {p.stem for p in UI.glob("*.html")}
    assert len(check.PAGES) == 10


def test_the_lists_are_well_formed():
    assert check.spec_problems() == []


def test_a_malformed_list_is_caught():
    bad = {"x": {"required": [{"css": ""}, {"css": "#a", "min": 0}, {"css": "#b", "colour": 1}],
                 "api_ok": [("GET", "no-slash")], "expected_failures": [("GET", "/a", 200)]}}
    problems = check.spec_problems(bad)
    assert len(problems) == 5, problems


@pytest.mark.parametrize("page", sorted(check.PAGES))
def test_every_required_id_and_class_is_in_the_pages_code(page):
    """The lists are written from the pages: every #id and .class a selector names is in the
    page's HTML or the scripts that build it."""
    code = _code(page)
    for r in check.SHELL + check.PAGES[page]["required"]:
        for name in re.findall(r"[#.]([A-Za-z][\w-]*)", r["css"]):
            assert name in code, (page, r["css"], name)


@pytest.mark.parametrize("page", sorted(check.PAGES))
def test_every_listed_request_is_one_the_page_sends(page):
    js = (UI / "assets" / "pages" / f"{page}.js").read_text(encoding="utf-8") + \
        (UI / "assets" / "app.js").read_text(encoding="utf-8")
    spec = check.PAGES[page]
    for method, path, *_ in spec["api_ok"] + spec["expected_failures"]:
        assert method == "GET"
        literal = path.split("{")[0].rstrip("/") if "{" in path else path
        assert f"'{literal}" in js, (page, path)


# --- the expected refusals are replay's own -----------------------------------------------------

@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv("BORG_DEMO_MODE", "replay")
    monkeypatch.setenv("BORG_REPLAY_DIR", str(ROOT / "demo" / "runs"))
    replay.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    replay.reset()


def _run_id(client):
    runs = client.get("/replay/status").json()["runs"]
    return next(r["run_id"] for r in runs if str(r["job"]) == "1")


@pytest.mark.parametrize("page", sorted(check.PAGES))
def test_expected_refusals_are_refused_by_replay_and_the_rest_answers(client, page):
    run_id = _run_id(client)
    spec = check.PAGES[page]
    for method, path, status in spec["expected_failures"]:
        r = client.request(method, path)
        assert r.status_code == status == 403, (page, path, r.status_code)
        assert r.json()["detail"]["error"] == "BRT-RPL-001", (page, path)
    for method, path in spec["api_ok"]:
        r = client.request(method, path.format(run_id=run_id))
        assert r.status_code == 200, (page, path, r.status_code)
    assert client.get(check.page_url("", page, run_id).split("?")[0]).status_code == 200


def test_the_pages_that_need_one_show_the_refusal_notice(client):
    """A page whose refused read is not quiet shows the notice; its list must require it."""
    for page, spec in check.PAGES.items():
        loud = []
        js = (UI / "assets" / "pages" / f"{page}.js").read_text(encoding="utf-8")
        for _, path, _ in spec["expected_failures"]:
            call = re.search(r"api\('" + re.escape(path) + r"'(\s*,\s*\{[^}]*\})?\)", js)
            if call and "quiet: true" not in (call.group(1) or ""):
                loud.append(path)
            elif not call and page == "logs" and re.search(r"B\.api\(q\)\.then", js):
                loud.append(path)   # logs builds the path in `q`, then B.api(q) with no options
        assert (check.REFUSAL_NOTICE in spec["required"]) == bool(loud), (page, loud)


def test_the_run_page_has_three_steps_in_the_played_job(client):
    view = client.get(f"/runs/{_run_id(client)}").json()
    assert len(view["envelopes"]) >= 3


# --- the failure classifier ---------------------------------------------------------------------

B = "http://127.0.0.1:8000"
LOAD_403 = "Failed to load resource: the server responded with a status of 403 (Forbidden)"


def test_a_clean_page_has_no_problems():
    assert check.classify([("GET", B + "/ui/ask.html", 200)], [], [], [], []) == []


def test_an_expected_refusal_and_its_console_line_are_allowed():
    resp = [("GET", B + "/keys/status", 403)]
    assert check.classify(resp, [], [(LOAD_403, B + "/keys/status")], [], [("GET", "/keys/status", 403)]) == []
    assert check.classify(resp, [], [(LOAD_403, "")], [], [("GET", "/keys/status", 403)]) == []


def test_an_unexpected_refusal_fails_and_names_the_request():
    out = check.classify([("GET", B + "/skills", 403)], [], [(LOAD_403, B + "/skills")], [], [])
    assert out[0] == "failed request: GET /skills -> 403"
    assert out[1].startswith("console error: Failed to load resource")


def test_a_listed_refusal_that_did_not_happen_fails():
    out = check.classify([], [], [], [], [("GET", "/config", 403)])
    assert out == ["expected refusal did not happen: GET /config -> 403 (update PAGES)"]


def test_query_strings_count():
    out = check.classify([("GET", B + "/logs?limit=50", 403)], [], [], [], [("GET", "/logs?limit=200", 403)])
    assert "failed request: GET /logs?limit=50 -> 403" in out


def test_other_console_errors_script_errors_and_network_failures_fail():
    out = check.classify([], [("GET", B + "/runs/x/stream", "net::ERR_FAILED")],
                         [("Refused to execute inline event handler", B + "/ui/ask.html")],
                         ["TypeError: x is undefined"], [])
    assert out == ["request failed: GET /runs/x/stream (net::ERR_FAILED)",
                   "console error: Refused to execute inline event handler",
                   "uncaught script error: TypeError: x is undefined"]


def test_favicon_is_tolerated_not_required():
    assert check.classify([("GET", B + "/favicon.ico", 403)], [], [(LOAD_403, B + "/favicon.ico")], [], []) == []


# --- packaging and the CI job -------------------------------------------------------------------

def test_ui_test_extra_holds_playwright_only_as_an_extra():
    meta = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    assert any(d.startswith("playwright") for d in meta["optional-dependencies"]["ui_test"])
    assert not any("playwright" in d for d in meta["dependencies"])


def test_the_script_imports_playwright_only_when_it_runs():
    src = (ROOT / "scripts" / "check_ui.py").read_text(encoding="utf-8")
    top = src.split("\ndef main(")[0]
    assert "playwright" not in "\n".join(l for l in top.splitlines() if l.startswith(("import", "from")))


def test_the_ci_job_shape():
    wf = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8"))
    job = wf["jobs"]["ui_check"]
    assert job["needs"] == "test" and job["runs-on"] == "ubuntu-latest"
    assert 0 < job["timeout-minutes"] <= 30 and "strategy" not in job
    runs = "\n".join(s.get("run", "") for s in job["steps"])
    assert 'pip install -e ".[ui_test]"' in runs
    assert "python -m playwright install chromium --with-deps" in runs
    assert "BORG_DEMO_MODE=replay" in runs and "uvicorn main:app" in runs and "/health" in runs
    assert "python scripts/check_ui.py" in runs
    order = [i for i, s in enumerate(job["steps"]) for key in ("uvicorn main:app", "/health", "scripts/check_ui.py")
             if key in s.get("run", "")]
    assert order == sorted(order)
    up = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert up["if"] == "always()" and up["with"]["name"] == "ui-check" and "ui_check_out" in up["with"]["path"]
