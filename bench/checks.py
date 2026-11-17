"""Machine checks for job B (generated Python service): work order 3.2.

Generated code is never run outside a container. The checks here only
*parse* and *load* it (SQL is executed by SQLite in memory, which cannot reach
the machine; Python is parsed with ``ast`` and never imported; YAML is loaded
with ``safe_load``). The generated tests run only inside
``docker run --network none`` with a timeout, and only when Docker is up and
the bench image exists; otherwise that check is reported "skipped", for both
arms alike.

Answer format asked of the model (see bench/jobs/job_b_spec.md): each file as

    === FILE: name.ext ===
    ```lang
    ...
    ```
"""
from __future__ import annotations

import ast
import re
import shutil
import sqlite3
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Callable, Optional

import yaml

BENCH_IMAGE = "borg_bench_py"
DOCKER_TIMEOUT_S = 180
EXPECTED_FILES = ("schema.sql", "app.py", "openapi.yaml", "test_app.py")

_FILE_RE = re.compile(
    r"===\s*FILE:\s*(?P<name>[\w./-]+)\s*===\s*\n```[^\n]*\n(?P<body>.*?)\n```",
    re.DOTALL,
)


def extract_files(answer: str) -> dict[str, str]:
    """``{file name: text}`` from the model's answer; the last block wins for a repeated name."""
    files: dict[str, str] = {}
    for m in _FILE_RE.finditer(answer or ""):
        name = m.group("name").strip().replace("\\", "/")
        # never let a name leave the work folder
        if name.startswith("/") or ".." in name.split("/"):
            continue
        files[name] = m.group("body")
    return files


# -- the individual checks --------------------------------------------------

def check_sql(files: dict[str, str]) -> tuple[bool, str]:
    sql = {n: t for n, t in files.items() if n.endswith(".sql")}
    if not sql:
        return False, "no .sql file"
    for name, text in sql.items():
        conn = sqlite3.connect(":memory:")
        try:
            conn.executescript(text)
        except sqlite3.Error as exc:
            return False, f"{name}: {exc}"
        finally:
            conn.close()
    return True, f"{len(sql)} sql file(s) executed in SQLite"


def check_python(files: dict[str, str]) -> tuple[bool, str]:
    py = {n: t for n, t in files.items() if n.endswith(".py")}
    if not py:
        return False, "no .py file"
    for name, text in py.items():
        try:
            ast.parse(text, filename=name)
        except SyntaxError as exc:
            return False, f"{name}: line {exc.lineno}: {exc.msg}"
    return True, f"{len(py)} python file(s) parse"


def check_yaml(files: dict[str, str]) -> tuple[bool, str]:
    ys = {n: t for n, t in files.items() if n.endswith((".yaml", ".yml"))}
    if not ys:
        return False, "no .yaml file"
    for name, text in ys.items():
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as exc:
            return False, f"{name}: {str(exc)[:120]}"
        if not isinstance(doc, dict) or "openapi" not in doc or "paths" not in doc:
            return False, f"{name}: needs top-level 'openapi' and 'paths'"
    return True, f"{len(ys)} yaml file(s) load with openapi and paths"


def docker_available(runner: Callable = subprocess.run) -> bool:
    """True when ``docker info`` succeeds (the engine is running)."""
    try:
        r = runner(["docker", "info"], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def image_present(image: str = BENCH_IMAGE, runner: Callable = subprocess.run) -> bool:
    try:
        r = runner(["docker", "image", "inspect", image], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def run_tests_in_container(
    files: dict[str, str],
    image: str = BENCH_IMAGE,
    timeout: int = DOCKER_TIMEOUT_S,
    runner: Callable = subprocess.run,
) -> tuple[Optional[bool], str]:
    """Run the generated tests inside ``docker run --network none``.

    Returns ``(True|False, detail)``, or ``(None, reason)`` when skipped
    (Docker down, image missing, or no test file). The files are copied into a
    fresh temp folder mounted read-only; the container has no network, a
    memory cap, a process cap and is removed afterwards.
    """
    if not any(n.endswith(".py") and "test" in Path(n).name for n in files):
        return None, "skipped: no test file in the answer"
    if not docker_available(runner):
        return None, "skipped: Docker is not running"
    if not image_present(image, runner):
        return None, f"skipped: image '{image}' not built (docker build -t {image} bench/docker)"
    work = Path(tempfile.mkdtemp(prefix="borg_bench_b_"))
    name = f"borg_bench_{uuid.uuid4().hex[:10]}"
    try:
        for rel, text in files.items():
            dest = work / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(text, encoding="utf-8")
        cmd = [
            "docker", "run", "--rm", "--name", name, "--network", "none",
            "--memory", "1g", "--pids-limit", "256", "--read-only", "--tmpfs", "/tmp",
            "-v", f"{work}:/work:ro", "-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1",
            image, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider", "-x",
        ]
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            runner(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=60)
            return False, f"tests timed out after {timeout}s"
        tail = ((r.stdout or "") + (r.stderr or ""))[-300:].strip().replace("\n", " | ")
        return r.returncode == 0, tail or f"exit {r.returncode}"
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_checks(answer: str, with_docker: bool = True, runner: Callable = subprocess.run) -> dict:
    """All job B checks for one answer.

    Returns ``{"files": [...], "checks": {name: {"status": "pass"|"fail"|"skipped", "detail": str}},
    "passed": int, "failed": int, "skipped": int, "machine_checks_passed": int}``.
    Each of the four expected files missing is a failed ``files_present`` check.
    """
    files = extract_files(answer)
    checks: dict[str, dict] = {}

    missing = [f for f in EXPECTED_FILES if f not in files]
    checks["files_present"] = {
        "status": "pass" if not missing else "fail",
        "detail": "all four files" if not missing else "missing: " + ", ".join(missing),
    }
    for name, fn in (("sql_executes", check_sql), ("python_parses", check_python), ("openapi_loads", check_yaml)):
        ok, detail = fn(files)
        checks[name] = {"status": "pass" if ok else "fail", "detail": detail}

    if not with_docker:
        checks["tests_pass_in_container"] = {"status": "skipped", "detail": "skipped: Docker step turned off"}
    else:
        ok, detail = run_tests_in_container(files, runner=runner)
        checks["tests_pass_in_container"] = {
            "status": "skipped" if ok is None else ("pass" if ok else "fail"),
            "detail": detail,
        }
    counts = {k: sum(1 for c in checks.values() if c["status"] == k) for k in ("pass", "fail", "skipped")}
    return {
        "files": sorted(files),
        "checks": checks,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "skipped": counts["skipped"],
        "machine_checks_passed": counts["pass"],
    }
