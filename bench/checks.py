"""Machine checks for job B (generated Python service): work order 3.2.

Generated code is never run outside a container. The checks here only
*parse* and *load* it (SQL is executed by SQLite in memory, which cannot reach
the machine; Python is parsed with ``ast`` and never imported; YAML is loaded
with ``safe_load``).

Spec checks, read from bench/jobs/job_b_spec.md: the SQL creates every table
the spec names; the OpenAPI YAML has every endpoint (path and method; path
parameter names may differ). There is no source-reading handler check (round-2
review (8), 17-Nov-2026): whether app.py serves every endpoint is decided by
the reference suite, which calls the endpoints.

Two test runs, both only inside ``docker run --network none`` with a memory
cap, a process cap, a read-only file system and a timeout: the answer's own
tests (one check), and our reference suite bench/jobs/job_b_reference_tests/
against the answer's app.py (one check per reference test). Job B requires
Docker: if Docker is down, the image is missing or the step is turned off, the
container checks are "skipped" and the result is marked ``void`` (and the README
voids all of job B when any run is void).

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
SPEC_FILE = Path(__file__).resolve().parent / "jobs" / "job_b_spec.md"
REFERENCE_DIR = Path(__file__).resolve().parent / "jobs" / "job_b_reference_tests"
HTTP_METHODS = ("get", "post", "put", "patch", "delete")

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


# -- spec checks (read the spec; parse, never import) -------------------------

def load_spec_requirements(path: Path = SPEC_FILE) -> dict:
    """``{"tables": [...], "endpoints": [(METHOD, path)]}`` as the spec states them."""
    text = Path(path).read_text(encoding="utf-8")
    m = re.search(r"tables?\s+((?:`\w+`(?:\s*,\s*|\s+and\s+)?)+)", text)
    tables = re.findall(r"`(\w+)`", m.group(1)) if m else []
    endpoints: list[tuple[str, str]] = []
    for method, path_ in re.findall(r"`(GET|POST|PUT|PATCH|DELETE)\s+(/[^`?\s]*)", text):
        if (method, path_) not in endpoints:
            endpoints.append((method, path_))
    return {"tables": tables, "endpoints": endpoints}


def norm_path(path: str) -> str:
    """``/notes/{note_id}/`` -> ``/notes/{}``: parameter names do not matter."""
    p = re.sub(r"\{[^}]*\}", "{}", str(path).strip())
    return p.rstrip("/") or "/"


def check_spec_tables(files: dict[str, str], req: dict) -> tuple[bool, str]:
    sql = [t for n, t in files.items() if n.endswith(".sql")]
    if not sql:
        return False, "no .sql file"
    conn = sqlite3.connect(":memory:")
    try:
        for text in sql:
            conn.executescript(text)
        have = {r[0].lower() for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    except sqlite3.Error as exc:
        return False, f"sql does not execute: {exc}"
    finally:
        conn.close()
    missing = [t for t in req["tables"] if t.lower() not in have]
    return (not missing), ("all required tables" if not missing else "missing tables: " + ", ".join(missing))


def check_spec_paths(files: dict[str, str], req: dict) -> tuple[bool, str]:
    ys = [t for n, t in files.items() if n.endswith((".yaml", ".yml"))]
    if not ys:
        return False, "no .yaml file"
    have: set[tuple[str, str]] = set()
    for text in ys:
        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError:
            continue
        paths = doc.get("paths") if isinstance(doc, dict) else None
        for path_, ops in (paths or {}).items() if isinstance(paths, dict) else []:
            if isinstance(ops, dict):
                for method in ops:
                    if str(method).lower() in HTTP_METHODS:
                        have.add((str(method).upper(), norm_path(path_)))
    missing = [f"{m} {p}" for m, p in req["endpoints"] if (m, norm_path(p)) not in have]
    return (not missing), ("every required path and method" if not missing else "missing: " + ", ".join(missing))


def reference_test_names(folder: Path = REFERENCE_DIR) -> list[str]:
    """The test functions of our reference suite (our own files; parsed, not imported)."""
    names: list[str] = []
    for f in sorted(Path(folder).glob("test_*.py")):
        for node in ast.parse(f.read_text(encoding="utf-8")).body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                names.append(node.name)
    return names


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


def _docker_pytest(files: dict[str, str], image: str, timeout: int, runner: Callable,
                   reference: bool) -> tuple[Optional[int], str]:
    """Run pytest in ``docker run --network none`` on the answer's files (read-only at /work).
    ``reference``: run our suite (read-only at /ref) against them instead of the answer's tests.
    Returns ``(exit code, output)``, or ``(None, detail)`` on a timeout."""
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
            "-v", f"{work}:/work:ro",
        ]
        if reference:
            cmd += ["-v", f"{REFERENCE_DIR}:/ref:ro"]
        cmd += ["-w", "/work", "-e", "PYTHONDONTWRITEBYTECODE=1", "-e", "PYTHONPATH=/work",
                image, "python", "-m", "pytest", "-q", "-p", "no:cacheprovider"]
        cmd += ["-rA", "/ref"] if reference else ["-x"]
        try:
            r = runner(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            runner(["docker", "rm", "-f", name], capture_output=True, text=True, timeout=60)
            return None, f"tests timed out after {timeout}s"
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    finally:
        shutil.rmtree(work, ignore_errors=True)


_RESULT_RE = re.compile(r"^(PASSED|FAILED|ERROR)\s+\S*?::(\w+)", re.MULTILINE)


def run_reference_tests(files: dict[str, str], image: str = BENCH_IMAGE, timeout: int = DOCKER_TIMEOUT_S,
                        runner: Callable = subprocess.run) -> dict[str, dict]:
    """One check per reference test; a test the run does not report as PASSED fails."""
    names = reference_test_names()
    rc, out = _docker_pytest(files, image, timeout, runner, reference=True)
    seen: dict[str, str] = {}
    for word, test in _RESULT_RE.findall(out or ""):
        seen.setdefault(test, word)
    tail = (out or "")[-200:].strip().replace("\n", " | ")
    result = {}
    for n in names:
        if rc is None:
            result[n] = {"status": "fail", "detail": out}
        elif seen.get(n) == "PASSED":
            result[n] = {"status": "pass", "detail": "passed"}
        else:
            result[n] = {"status": "fail", "detail": (seen.get(n) or "not reported").lower() + (f": {tail}" if tail else "")}
    return result


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


def run_checks(answer: str, with_docker: bool = True, runner: Callable = subprocess.run,
               spec: Optional[dict] = None) -> dict:
    """All job B checks for one answer.

    Returns ``{"files": [...], "checks": {name: {"status": "pass"|"fail"|"skipped", "detail": str}},
    "passed": int, "failed": int, "skipped": int, "machine_checks_passed": int, "void": bool,
    "void_reason": str|None}``. Each of the four expected files missing is a failed
    ``files_present`` check. Reference tests are the checks ``ref_<test name>``.
    """
    files = extract_files(answer)
    req = spec or load_spec_requirements()
    checks: dict[str, dict] = {}

    missing = [f for f in EXPECTED_FILES if f not in files]
    checks["files_present"] = {
        "status": "pass" if not missing else "fail",
        "detail": "all four files" if not missing else "missing: " + ", ".join(missing),
    }
    for name, fn in (("sql_executes", check_sql), ("python_parses", check_python), ("openapi_loads", check_yaml)):
        ok, detail = fn(files)
        checks[name] = {"status": "pass" if ok else "fail", "detail": detail}
    for name, fn in (("spec_tables", check_spec_tables), ("spec_openapi_paths", check_spec_paths)):
        ok, detail = fn(files, req)
        checks[name] = {"status": "pass" if ok else "fail", "detail": detail}

    if not with_docker:
        void_reason = "Docker step turned off (--no-docker)"
    elif not docker_available(runner):
        void_reason = "Docker is not running"
    elif not image_present(BENCH_IMAGE, runner):
        void_reason = f"image '{BENCH_IMAGE}' not built (docker build -t {BENCH_IMAGE} bench/docker)"
    else:
        void_reason = None

    ref_names = reference_test_names()
    if void_reason:
        checks["tests_pass_in_container"] = {"status": "skipped", "detail": f"skipped: {void_reason}"}
        for n in ref_names:
            checks[f"ref_{n}"] = {"status": "skipped", "detail": f"skipped: {void_reason}"}
    else:
        if not any(n.endswith(".py") and "test" in Path(n).name for n in files):
            checks["tests_pass_in_container"] = {"status": "fail", "detail": "no test file in the answer"}
        else:
            rc, out = _docker_pytest(files, BENCH_IMAGE, DOCKER_TIMEOUT_S, runner, reference=False)
            tail = (out or "")[-300:].strip().replace("\n", " | ")
            checks["tests_pass_in_container"] = {
                "status": "pass" if rc == 0 else "fail",
                "detail": out if rc is None else (tail or f"exit {rc}"),
            }
        for n, c in run_reference_tests(files, runner=runner).items():
            checks[f"ref_{n}"] = c

    counts = {k: sum(1 for c in checks.values() if c["status"] == k) for k in ("pass", "fail", "skipped")}
    return {
        "files": sorted(files),
        "checks": checks,
        "passed": counts["pass"],
        "failed": counts["fail"],
        "skipped": counts["skipped"],
        "machine_checks_passed": counts["pass"],
        "void": void_reason is not None,
        "void_reason": void_reason,
    }
