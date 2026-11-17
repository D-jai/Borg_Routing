"""4.2 review: locality is worked out by the server, never guessed by a page.

- every model row from GET /models and GET /models/matrix carries ``is_local``
  (src/registry/locality.py rules: endpoint host, -cloud tags, allow-list);
- GET /runs/{id} carries ``calls_left_machine`` {count, source}: the outbound
  ledger when there is one, else the locality of each logged call, child runs
  included; unknown is null, never 0;
- a run records the lane mode it ran under (``runs.lane_mode``).
"""
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from main import app
from src.api import tasks as tasks_api
from src.db.schema import create_all
from src.db.state import create_run, update_run_status
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo


def mi(model_id, source, endpoint, **kw):
    return ModelInfo(model_id=model_id, source=source, endpoint=endpoint, context_length=4096, **kw)


@pytest.fixture
def matrix(monkeypatch):
    monkeypatch.delenv("BORG_LOCAL_HOSTS", raising=False)
    monkeypatch.delenv("BORG_LANE_LOCAL_ALLOWLIST", raising=False)
    m = CapabilityMatrix()
    m.upsert(mi("lmstudio/here", "lmstudio", "http://localhost:1234"))
    m.upsert(mi("ollama/elsewhere", "ollama", "http://10.1.2.3:11434"))
    m.upsert(mi("ollama/big-model", "ollama", "http://localhost:11434"))
    m.upsert(mi("ollama/tagged:cloud", "ollama", "http://localhost:11434"))
    m.upsert(mi("openrouter/x/y", "openrouter", "https://openrouter.ai/api/v1"))
    monkeypatch.setattr(app.state, "matrix", m, raising=False)
    return m


@pytest.fixture
async def db(tmp_path, monkeypatch):
    path = str(tmp_path / "loc.db")
    await create_all(path)
    monkeypatch.setattr(tasks_api, "DB_PATH", path)
    monkeypatch.setattr("src.db.schema.DB_PATH", path)
    return path


def client():
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def rows(path):
    return {r["model_id"]: r for r in client().get(path).json()}


@pytest.mark.parametrize("path", ["/models", "/models/matrix"])
def test_every_model_row_carries_a_computed_is_local(matrix, db, path):
    got = rows(path)
    assert got["lmstudio/here"]["is_local"] is True
    assert got["openrouter/x/y"]["is_local"] is False
    # An Ollama row whose endpoint is not this machine is not local, whatever its label.
    assert got["ollama/elsewhere"]["is_local"] is False
    # A -cloud / :cloud tag is refused even on a loopback endpoint.
    assert got["ollama/tagged:cloud"]["is_local"] is False
    assert got["ollama/big-model"]["is_local"] is True


def test_ollama_on_a_host_listed_in_borg_local_hosts_is_local(matrix, db, monkeypatch):
    monkeypatch.setenv("BORG_LOCAL_HOSTS", "10.1.2.3")
    assert rows("/models")["ollama/elsewhere"]["is_local"] is True
    assert rows("/models/matrix")["ollama/elsewhere"]["is_local"] is True


def test_a_remote_row_and_the_allow_list_are_honoured(matrix, db, monkeypatch):
    matrix.upsert(mi("lmstudio/remote-flag", "lmstudio", "http://localhost:1234", remote=True))
    monkeypatch.setenv("BORG_LANE_LOCAL_ALLOWLIST", "lmstudio/here")
    got = rows("/models")
    assert got["lmstudio/remote-flag"]["is_local"] is False
    assert got["lmstudio/here"]["is_local"] is True
    assert got["ollama/big-model"]["is_local"] is False   # not on the allow-list


# ---- calls that left this machine -------------------------------------------------------------

def log_call(path, run_id, model_id):
    with sqlite3.connect(path) as c:
        c.execute(
            "INSERT INTO logs(run_id, event_type, model_id, tokens_input, tokens_output, dollars)"
            " VALUES (?, 'model_call', ?, 10, 5, 0.01)", (str(run_id), model_id))
        c.commit()


def left(run_id):
    return client().get(f"/runs/{run_id}").json()["calls_left_machine"]


@pytest.mark.asyncio
async def test_calls_left_machine_comes_from_the_ledger_when_the_lane_was_on(matrix, db):
    run = await create_run("q", db_path=db, lane_mode="all_local")
    log_call(db, run, "openrouter/x/y")           # ignored: the ledger is the record
    assert left(run) == {"count": 0, "source": "ledger"}   # a real, recorded zero
    with sqlite3.connect(db) as c:
        c.execute(
            "INSERT INTO outbound_requests(request_id, run_id, envelope_id, attempt, purpose, destination, url,"
            " left_machine, body, status, http_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), str(run), None, 1, "answerer", "openrouter/x/y", "https://openrouter.ai/x", 1, "{}", "sent", 200))
        c.commit()
    assert left(run) == {"count": 1, "source": "ledger"}


@pytest.mark.asyncio
async def test_calls_left_machine_without_a_ledger_judges_each_call_and_counts_child_runs(matrix, db):
    run = await create_run("q", db_path=db)
    child = await create_run("child", parent_run_id=run, db_path=db)
    log_call(db, run, "lmstudio/here")
    log_call(db, run, "openrouter/x/y")
    log_call(db, child, "ollama/elsewhere")       # not on this machine
    log_call(db, child, "lmstudio/here")
    assert left(run) == {"count": 2, "source": "locality"}


@pytest.mark.asyncio
async def test_unknown_locality_is_null_never_zero(matrix, db):
    run = await create_run("q", db_path=db)
    assert left(run) == {"count": None, "source": None}            # nothing logged
    log_call(db, run, "lmstudio/here")
    log_call(db, run, "gone/from/the-matrix")                      # model no longer in the matrix
    assert left(run) == {"count": None, "source": None}


# ---- the run records its lane mode ------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_run_record_carries_the_lane_mode_it_ran_under(matrix, db):
    a = await create_run("q", db_path=db, lane_mode="raw_local")
    b = await create_run("q", db_path=db)
    assert client().get(f"/runs/{a}").json()["run"]["lane_mode"] == "raw_local"
    assert client().get(f"/runs/{b}").json()["run"]["lane_mode"] is None   # not recorded: unknown


@pytest.mark.asyncio
async def test_an_older_database_gains_the_lane_mode_column(tmp_path):
    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as c:
        c.execute("CREATE TABLE runs (run_id TEXT PRIMARY KEY, status TEXT, user_instruction TEXT)")
        c.commit()
    await create_all(path)
    with sqlite3.connect(path) as c:
        cols = [r[1] for r in c.execute("PRAGMA table_info(runs)")]
    assert "lane_mode" in cols


@pytest.mark.asyncio
async def test_a_pipeline_run_records_off_when_there_is_no_lane(tmp_path, monkeypatch):
    from tests.e2e.test_cost_recording import _run  # noqa: PLC0415

    _stub, result, path = await _run(tmp_path, monkeypatch, plan=[
        {"type": "answerer", "id": "n1", "instruction": "Say hi.", "output_schema": {"result": "string"}}])
    monkeypatch.setattr(tasks_api, "DB_PATH", path)
    assert client().get(f"/runs/{result['run_id']}").json()["run"]["lane_mode"] == "off"
