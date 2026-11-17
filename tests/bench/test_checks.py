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


def test_good_answer_passes_every_machine_check_when_docker_is_off():
    res = checks.run_checks(answer(), with_docker=False)
    st = {k: v["status"] for k, v in res["checks"].items()}
    assert st == {"files_present": "pass", "sql_executes": "pass", "python_parses": "pass",
                  "openapi_loads": "pass", "tests_pass_in_container": "skipped"}
    assert res["machine_checks_passed"] == 4 and res["failed"] == 0 and res["skipped"] == 1


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
