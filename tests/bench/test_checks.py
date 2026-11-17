"""bench/checks.py (job B): good and bad generated code; Docker mocked or skipped; nothing runs outside a container."""
from __future__ import annotations

import subprocess
from pathlib import Path

from bench import checks

SQL = "CREATE TABLE notes (id INTEGER PRIMARY KEY, title TEXT NOT NULL);\n"
APP = "from fastapi import FastAPI\napp = FastAPI()\n"
OPENAPI = "openapi: 3.0.0\ninfo: {title: t, version: '1'}\npaths:\n  /notes: {}\n"
TEST = "def test_ok():\n    assert True\n"


def block(name: str, lang: str, body: str) -> str:
    return f"=== FILE: {name} ===\n```{lang}\n{body.rstrip()}\n```\n"


def answer(sql=SQL, app=APP, yml=OPENAPI, test=TEST) -> str:
    return (block("schema.sql", "sql", sql) + block("app.py", "python", app)
            + block("openapi.yaml", "yaml", yml) + block("test_app.py", "python", test))


def cp(rc=0, out=""):
    return subprocess.CompletedProcess(args=[], returncode=rc, stdout=out, stderr="")


def test_extract_files_and_refuses_paths_that_leave_the_folder():
    text = answer() + block("../evil.py", "python", "x=1") + block("/abs.py", "python", "x=1")
    files = checks.extract_files(text)
    assert set(files) == {"schema.sql", "app.py", "openapi.yaml", "test_app.py"}


def test_basic_checks_pass_and_docker_off_makes_the_result_void():
    res = checks.run_checks(answer(), with_docker=False)
    st = {k: v["status"] for k, v in res["checks"].items()}
    for k in ("files_present", "sql_executes", "python_parses", "openapi_loads"):
        assert st[k] == "pass"
    assert st["tests_pass_in_container"] == "skipped"
    assert all(v == "skipped" for k, v in st.items() if k.startswith("ref_"))
    assert res["void"] is True and "turned off" in res["void_reason"]


def test_bad_sql_bad_python_and_bad_yaml_each_fail():
    res = checks.run_checks(answer(sql="CREATE TABEL x (", app="def broken(:\n", yml="just: a mapping\n"),
                            with_docker=False)
    st = {k: v["status"] for k, v in res["checks"].items()}
    assert st["sql_executes"] == st["python_parses"] == st["openapi_loads"] == "fail"
    assert "app.py" in res["checks"]["python_parses"]["detail"]
    assert "openapi" in res["checks"]["openapi_loads"]["detail"]
    assert res["machine_checks_passed"] == 1  # only files_present


def test_missing_files_fail_the_presence_check():
    res = checks.run_checks(block("schema.sql", "sql", SQL), with_docker=False)
    assert res["checks"]["files_present"]["status"] == "fail"
    assert "app.py" in res["checks"]["files_present"]["detail"]
    assert res["checks"]["python_parses"]["status"] == "fail"


def test_generated_code_is_parsed_never_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    evil = "open('pwned.txt', 'w').write('x')\nraise SystemExit(1)\n"
    res = checks.run_checks(answer(app=evil, test=evil), with_docker=False)
    assert res["checks"]["python_parses"]["status"] == "pass"
    assert not Path("pwned.txt").exists()


def test_docker_not_running_skips_the_container_step():
    def runner(cmd, **kw):
        return cp(1)  # docker info fails
    res = checks.run_checks(answer(), with_docker=True, runner=runner)
    c = res["checks"]["tests_pass_in_container"]
    assert c["status"] == "skipped" and "not running" in c["detail"]


def test_docker_missing_binary_skips():
    def runner(cmd, **kw):
        raise FileNotFoundError("docker")
    assert checks.docker_available(runner) is False
    res = checks.run_checks(answer(), with_docker=True, runner=runner)
    assert res["checks"]["tests_pass_in_container"]["status"] == "skipped"


def test_missing_image_skips():
    def runner(cmd, **kw):
        return cp(0) if cmd[1] == "info" else cp(1)  # image inspect fails
    res = checks.run_checks(answer(), with_docker=True, runner=runner)
    assert "not built" in res["checks"]["tests_pass_in_container"]["detail"]


def test_tests_run_only_inside_a_no_network_container():
    seen = []

    def runner(cmd, **kw):
        seen.append((cmd, kw))
        return cp(0, "1 passed in 0.1s") if cmd[1] == "run" else cp(0)

    res = checks.run_checks(answer(), with_docker=True, runner=runner)
    assert res["checks"]["tests_pass_in_container"]["status"] == "pass"
    run_cmd, kw = next((c, k) for c, k in seen if c[1] == "run")
    assert ["--network", "none"] == run_cmd[run_cmd.index("--network"):run_cmd.index("--network") + 2]
    assert "--read-only" in run_cmd and "--memory" in run_cmd and kw["timeout"] > 0
    assert any(a.endswith(":/work:ro") for a in run_cmd)
    assert res["machine_checks_passed"] == 5


def test_failing_tests_and_timeouts_in_the_container():
    def failing(cmd, **kw):
        return cp(1, "1 failed") if cmd[1] == "run" else cp(0)

    assert checks.run_checks(answer(), runner=failing)["checks"]["tests_pass_in_container"]["status"] == "fail"

    calls = []

    def slow(cmd, **kw):
        calls.append(cmd[1])
        if cmd[1] == "run":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        return cp(0)

    res = checks.run_checks(answer(), runner=slow)
    assert res["checks"]["tests_pass_in_container"]["status"] == "fail"
    assert "timed out" in res["checks"]["tests_pass_in_container"]["detail"]
    assert "rm" in calls, "the timed-out container is removed"


# -- review fixes: spec checks, reference suite, void without Docker -------------------------------

import re  # noqa: E402
import subprocess as _sp  # noqa: E402
import sys  # noqa: E402

MINIMAL = (Path(__file__).parent / "data" / "job_b_minimal_answer.md").read_text(encoding="utf-8")
DO_NOTHING = answer(sql="SELECT 1;", app="app = None\n", yml="openapi: 3.0.0\npaths: {}\n", test=TEST)
REF_NAMES = checks.reference_test_names()


def test_spec_requirements_are_read_from_the_spec_file():
    req = checks.load_spec_requirements()
    assert req["tables"] == ["notes", "tags", "note_tags"]
    assert sorted(req["endpoints"]) == [("DELETE", "/notes/{id}"), ("GET", "/notes"), ("GET", "/notes/{id}"),
                                        ("POST", "/notes")]
    assert len(REF_NAMES) >= 8 and all(n.startswith("test_") for n in REF_NAMES)


def docker_ok(ref_passes=True, generated_rc=0):
    """A mocked Docker: engine up, image present; the reference run reports every test PASSED (or FAILED)."""
    seen = []

    def runner(cmd, **kw):
        seen.append(cmd)
        if cmd[1] != "run":
            return cp(0)
        if "/ref" in cmd:
            word = "PASSED" if ref_passes else "FAILED"
            out = "\n".join(f"{word} ../ref/test_reference.py::{n}" for n in REF_NAMES)
            return cp(0 if ref_passes else 1, out)
        return cp(generated_rc, "1 passed")

    runner.seen = seen
    return runner


def test_the_do_nothing_answer_fails_every_spec_check():
    res = checks.run_checks(DO_NOTHING, runner=docker_ok(ref_passes=False))
    st = {k: v["status"] for k, v in res["checks"].items()}
    assert st["spec_tables"] == "fail" and "notes" in res["checks"]["spec_tables"]["detail"]
    assert st["spec_openapi_paths"] == "fail" and "/notes" in res["checks"]["spec_openapi_paths"]["detail"]
    assert st["spec_handlers"] == "fail" and "FastAPI" in res["checks"]["spec_handlers"]["detail"]
    assert all(st[f"ref_{n}"] == "fail" for n in REF_NAMES)
    assert res["void"] is False
    good = checks.run_checks(MINIMAL, runner=docker_ok())
    assert res["machine_checks_passed"] < good["machine_checks_passed"] - len(REF_NAMES) - 2


def test_a_correct_minimal_service_passes_every_check_with_docker_mocked():
    runner = docker_ok()
    res = checks.run_checks(MINIMAL, runner=runner)
    failed = {k: v for k, v in res["checks"].items() if v["status"] != "pass"}
    assert failed == {}, failed
    assert res["machine_checks_passed"] == len(res["checks"]) == 8 + len(REF_NAMES)
    assert res["void"] is False


def test_handlers_need_a_fastapi_app_and_every_route():
    files = checks.extract_files(MINIMAL)
    req = checks.load_spec_requirements()
    assert checks.check_spec_handlers(files, req)[0] is True
    no_delete = dict(files, **{"app.py": files["app.py"].replace('@app.delete("/notes/{note_id}", status_code=204)', "@staticmethod")})
    ok, detail = checks.check_spec_handlers(no_delete, req)
    assert not ok and "DELETE /notes/{id}" in detail
    renamed = dict(files, **{"app.py": files["app.py"].replace("app = FastAPI()", "app = None\nother = FastAPI()")})
    assert checks.check_spec_handlers(renamed, req)[0] is False


def test_openapi_paths_need_each_method_and_accept_any_parameter_name():
    req = checks.load_spec_requirements()
    yml = MINIMAL.split("=== FILE: openapi.yaml ===")[1].split("```yaml\n")[1].split("```")[0]
    assert checks.check_spec_paths({"openapi.yaml": yml.replace("/notes/{id}", "/notes/{note_id}")}, req)[0]
    ok, detail = checks.check_spec_paths({"openapi.yaml": yml.replace("    delete:", "    put:")}, req)
    assert not ok and "DELETE /notes/{id}" in detail


def test_reference_suite_runs_only_in_the_no_network_container_with_the_suite_read_only():
    runner = docker_ok()
    checks.run_checks(MINIMAL, runner=runner)
    ref_cmd = next(c for c in runner.seen if c[1] == "run" and "/ref" in c)
    assert ref_cmd[ref_cmd.index("--network") + 1] == "none"
    assert "--read-only" in ref_cmd and "--memory" in ref_cmd and "--pids-limit" in ref_cmd
    mounts = [ref_cmd[i + 1] for i, a in enumerate(ref_cmd) if a == "-v"]
    assert any(m.endswith(":/work:ro") for m in mounts) and any(m.endswith(":/ref:ro") for m in mounts)


def test_a_reference_test_missing_from_the_output_fails():
    def runner(cmd, **kw):
        if cmd[1] != "run":
            return cp(0)
        return cp(1, "PASSED ../ref/test_reference.py::" + REF_NAMES[0]) if "/ref" in cmd else cp(0, "ok")

    res = checks.run_checks(MINIMAL, runner=runner)
    assert res["checks"][f"ref_{REF_NAMES[0]}"]["status"] == "pass"
    assert all(res["checks"][f"ref_{n}"]["status"] == "fail" for n in REF_NAMES[1:])


def test_docker_or_image_unavailable_makes_the_result_void():
    down = checks.run_checks(MINIMAL, runner=lambda cmd, **kw: cp(1))
    assert down["void"] is True and "not running" in down["void_reason"]
    no_image = checks.run_checks(MINIMAL, runner=lambda cmd, **kw: cp(0) if cmd[1] == "info" else cp(1))
    assert no_image["void"] is True and "not built" in no_image["void_reason"]


def test_an_answer_without_a_test_file_fails_the_generated_tests_check():
    files = MINIMAL.split("=== FILE: test_app.py ===")[0]
    res = checks.run_checks(files, runner=docker_ok())
    assert res["checks"]["tests_pass_in_container"]["status"] == "fail"
    assert res["void"] is False


def test_the_reference_suite_itself_passes_a_correct_service_and_fails_a_broken_one(tmp_path):
    """Our own suite against our own minimal service, run locally (no generated code is run here)."""
    import os

    def run(answer_text, select):
        work = tmp_path / f"w{len(list(tmp_path.iterdir()))}"
        work.mkdir()
        for name, text in checks.extract_files(answer_text).items():
            (work / name).write_text(text, encoding="utf-8")
        env = {**os.environ, "PYTHONPATH": str(work), "PYTHONDONTWRITEBYTECODE": "1"}
        return _sp.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-k", select,
                        str(checks.REFERENCE_DIR)], cwd=work, env=env, capture_output=True, text=True, timeout=120)

    assert run(MINIMAL, "not newest").returncode == 0
    broken = MINIMAL.replace("raise HTTPException(422, \"title must be 1 to 120 characters\")", "pass")
    assert broken != MINIMAL
    r = run(broken, "title")
    assert r.returncode == 1 and re.search(r"2 failed", r.stdout), r.stdout[-500:]
