"""Work order 4.3 — Admin status endpoints.

TC-ADM-030 — GET /keys/status says saved or not saved per provider and never
             returns a key value (not from the environment, not from .env).
TC-ADM-031 — GET /status/local reports which local servers answer.
TC-ADM-032 — GET /logs/outlier-runs ranks by cost or duration, never by prompt
             length (R10: the old dashboard sorted "Outlier Runs" by prompt length).
"""
import sqlite3

import pytest
from fastapi.testclient import TestClient

from main import app
from src.api import logs as logs_mod
from src.api import status as status_mod
from src.db.schema import create_all

FAKE_ENV_KEY = "sk-or-FAKE-ENV-KEY-9f3a1c77"
FAKE_FILE_KEY = "tvly-FAKE-FILE-KEY-52be08d1"


@pytest.fixture
def client():
    return TestClient(app)


def test_keys_status_saved_never_a_value(client, tmp_path, monkeypatch):
    """A key in the environment and a key only in .env both show as saved, with no value."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(f"# comment\nTAVILY_API_KEY={FAKE_FILE_KEY}\n", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_ENV_KEY)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    res = client.get("/keys/status")

    assert res.status_code == 200
    by_id = {p["id"]: p for p in res.json()["providers"]}
    assert by_id["openrouter"]["saved"] is True
    assert by_id["tavily"]["saved"] is True
    for secret in (FAKE_ENV_KEY, FAKE_FILE_KEY):
        assert secret not in res.text
    assert set(by_id["openrouter"]) == {"id", "label", "env_var", "saved"}


def test_keys_status_not_saved(client, tmp_path, monkeypatch):
    """No variable and an empty .env line: not saved."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("OPENROUTER_API_KEY=\n", encoding="utf-8")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)

    by_id = {p["id"]: p for p in client.get("/keys/status").json()["providers"]}

    assert by_id["openrouter"]["saved"] is False
    assert by_id["tavily"]["saved"] is False


def test_keys_status_does_not_log_a_key(client, tmp_path, monkeypatch, caplog):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", FAKE_ENV_KEY)
    with caplog.at_level("DEBUG"):
        client.get("/keys/status")
    assert FAKE_ENV_KEY not in caplog.text


def test_local_status_reports_each_server(client, monkeypatch):
    async def fake_probe(_client, name, url, path):
        return {"name": name, "url": url, "reachable": name == "Ollama"}

    monkeypatch.setattr(status_mod, "_probe", fake_probe)
    body = client.get("/status/local").json()
    assert body["reachable"] is True
    assert {s["name"] for s in body["servers"]} >= {"Ollama", "LM Studio"}


def test_local_status_none_reachable(client, monkeypatch):
    async def fake_probe(_client, name, url, path):
        return {"name": name, "url": url, "reachable": False}

    monkeypatch.setattr(status_mod, "_probe", fake_probe)
    assert client.get("/status/local").json()["reachable"] is False


@pytest.fixture
async def outlier_db(tmp_path, monkeypatch):
    path = str(tmp_path / "outliers.db")
    await create_all(path)
    monkeypatch.setattr(logs_mod, "DB_PATH", path)
    db = sqlite3.connect(path)
    # run_id, prompt, created, completed, paid dollars
    runs = [
        # a very long prompt that is cheap and quick: the old sort put this first
        ("r-long", "x" * 4000, "2026-11-16 10:00:00", "2026-11-16 10:00:02", 0.001),
        # a one-word prompt that cost the most
        ("r-costly", "hi", "2026-11-16 10:05:00", "2026-11-16 10:05:10", 0.90),
        # a short prompt that took the longest
        ("r-slow", "go", "2026-11-16 10:10:00", "2026-11-16 10:12:00", 0.02),
        # still running: no duration
        ("r-open", "wait", "2026-11-16 10:20:00", None, 0.5),
    ]
    for run_id, prompt, created, completed, dollars in runs:
        db.execute(
            "INSERT INTO runs (run_id, status, user_instruction, created_at, completed_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (run_id, "completed" if completed else "running", prompt, created, completed),
        )
        db.execute(
            "INSERT INTO logs (run_id, event_type, model_id, tokens_input, tokens_output, dollars) "
            "VALUES (?, 'model_call', 'm', 10, 5, ?)",
            (run_id, dollars),
        )
    db.commit()
    db.close()
    return path


@pytest.mark.asyncio
async def test_outliers_sort_by_cost_not_prompt_length(client, outlier_db):
    res = client.get("/logs/outlier-runs?sort=cost&limit=3")
    ids = [r["run_id"] for r in res.json()["runs"]]
    assert ids == ["r-costly", "r-open", "r-slow"]
    assert ids[0] != "r-long"  # the longest prompt is not the outlier


@pytest.mark.asyncio
async def test_outliers_sort_by_duration(client, outlier_db):
    res = client.get("/logs/outlier-runs?sort=duration&limit=5")
    runs = res.json()["runs"]
    assert [r["run_id"] for r in runs] == ["r-slow", "r-costly", "r-long"]
    assert runs[0]["duration_s"] == pytest.approx(120.0, abs=1)
    assert "r-open" not in [r["run_id"] for r in runs]  # not finished: no duration


@pytest.mark.asyncio
async def test_outliers_refuse_a_sort_that_is_not_cost_or_duration(client, outlier_db):
    assert client.get("/logs/outlier-runs?sort=prompt_length").status_code == 422
