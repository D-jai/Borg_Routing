"""TC-LANE-050..: the on-premise lane's API surface.

POST /v1/task and POST /v1/chat/completions take ``documents``; GET /runs/{id}
lists what left the machine; resume is refused for a run that read
documents; bypass and admin outbound actions follow the mode; lane settings
are validated at POST /config. Every test uses a temp database.
"""
import asyncio
import json
import sqlite3
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from main import app

_ENV = ("BORG_LANE_MODE", "BORG_LANE_MANIFEST", "BORG_LANE_LOCAL_ALLOWLIST", "BORG_LOCAL_HOSTS")


@pytest.fixture
def lane_api(tmp_path, monkeypatch):
    from src.api import admin, chat_completions, tasks
    from src.db import schema
    from src.db.schema import create_all
    from src.pipeline import runner

    db = str(tmp_path / "api_lane.db")
    asyncio.run(create_all(db))
    for mod in (tasks, admin, runner, schema, chat_completions):
        monkeypatch.setattr(mod, "DB_PATH", db)
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "note.txt").write_text("A client note about Acme.", encoding="utf-8")
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    return {"db": db, "client": TestClient(app, raise_server_exceptions=False),
            "mp": monkeypatch, "tmp": tmp_path}


async def _noop(*args, **kwargs):
    return None


def _error(resp) -> str:
    return resp.json()["detail"]["error"]


# Happy path ---------------------------------------------------------------------

def test_task_with_documents_creates_a_run_holding_the_manifest(lane_api):
    lane_api["mp"].setenv("BORG_LANE_MODE", "raw_local")
    with patch("src.api.tasks._run_in_background", new=_noop):
        resp = lane_api["client"].post(
            "/v1/task", json={"instruction": "Summarise the notes.",
                              "documents": {"files": ["note.txt"]}})
    assert resp.status_code == 202
    with sqlite3.connect(lane_api["db"]) as conn:
        (manifest,) = conn.execute(
            "SELECT documents FROM runs WHERE run_id=?", (resp.json()["run_id"],)
        ).fetchone()
    assert json.loads(manifest)[0]["id"] == "doc_001"
    assert "Acme" not in manifest


def test_run_detail_lists_what_left_the_machine_for_the_run_tree(lane_api):
    db = lane_api["db"]
    parent, child = str(uuid4()), str(uuid4())
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO runs(run_id, status, user_instruction) VALUES (?, 'completed', 'p')",
                     (parent,))
        conn.execute("INSERT INTO runs(run_id, parent_run_id, status, user_instruction) "
                     "VALUES (?, ?, 'completed', 'c')", (child, parent))
        for rid, left in ((parent, 1), (child, 1), (parent, 0)):
            conn.execute(
                "INSERT INTO outbound_requests(request_id, run_id, attempt, purpose, url, "
                "left_machine, body, status) VALUES (?,?,1,'model','https://x.test',?,?,'sent')",
                (str(uuid4()), rid, left, json.dumps({"model": "m"})),
            )
    body = lane_api["client"].get(f"/runs/{parent}").json()
    assert {r["run_id"] for r in body["outbound"]} == {parent, child}
    assert all(r["left_machine"] == 1 for r in body["outbound"])
    assert json.loads(body["outbound"][0]["body"]) == {"model": "m"}


def test_a_valid_lane_mode_is_saved_and_read_by_the_next_run(lane_api):
    from src.lane.settings import load_lane_settings

    resp = lane_api["client"].post("/config", json={"key": "lane_mode", "value": "all_local"})
    assert resp.status_code == 200
    assert asyncio.run(load_lane_settings(lane_api["db"])).mode == "all_local"
    effective = lane_api["client"].get("/config/effective").json()["settings"]
    assert {"key": "lane_mode", "value": "all_local"}.items() <= next(
        s for s in effective if s["key"] == "lane_mode").items()


def test_raw_local_bypass_is_recorded_before_it_is_sent(lane_api):
    import main
    from tests.e2e.stub_model_server import StubModels, stub_model

    lane_api["mp"].setenv("BORG_LANE_MODE", "raw_local")
    lane_api["mp"].setenv("OPENROUTER_API_KEY", "sk-or-test")
    main.matrix.upsert(stub_model("bypassed", 8.0, 1e-6, 1e-6))
    stub = StubModels(plan_nodes=[]).install(lane_api["mp"])
    resp = lane_api["client"].post("/v1/chat/completions", json={
        "model": "openrouter/stub/bypassed",
        "messages": [{"role": "user", "content": "hello"}]})
    assert resp.status_code == 200
    with sqlite3.connect(lane_api["db"]) as conn:
        rows = conn.execute("SELECT purpose, left_machine, body FROM outbound_requests").fetchall()
    assert len(rows) == 1 and rows[0][0] == "bypass" and rows[0][1] == 1
    assert json.loads(rows[0][2]) == stub.requests[0].body


# Unhappy path -------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["../secret.txt", "C:\\Windows\\win.ini", "\\\\server\\share\\x.txt"])
def test_task_with_a_path_outside_the_root_is_refused(lane_api, bad):
    lane_api["mp"].setenv("BORG_LANE_MODE", "raw_local")
    with patch("src.api.tasks._run_in_background", new=_noop):
        resp = lane_api["client"].post(
            "/v1/task", json={"instruction": "x", "documents": {"files": [bad]}})
    assert resp.status_code == 400
    assert _error(resp) == "BRT-LANE-001"
    assert str(lane_api["tmp"]) not in resp.text
    with sqlite3.connect(lane_api["db"]) as conn:
        assert conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0] == 0


def test_documents_with_the_lane_off_are_refused(lane_api):
    resp = lane_api["client"].post(
        "/v1/task", json={"instruction": "x", "documents": {"files": ["note.txt"]}})
    assert resp.status_code == 400 and _error(resp) == "BRT-LANE-005"


def test_resume_is_refused_for_a_run_that_read_documents(lane_api):
    run_id = str(uuid4())
    with sqlite3.connect(lane_api["db"]) as conn:
        conn.execute("INSERT INTO runs(run_id, status, user_instruction, documents) "
                     "VALUES (?, 'failed', 'x', ?)", (run_id, json.dumps([{"id": "doc_001"}])))
    resp = lane_api["client"].post(f"/runs/{run_id}/resume")
    assert resp.status_code == 409 and _error(resp) == "BRT-LANE-008"


def test_bypass_is_refused_in_all_local(lane_api):
    lane_api["mp"].setenv("BORG_LANE_MODE", "all_local")
    resp = lane_api["client"].post("/v1/chat/completions", json={
        "model": "openrouter/some/model", "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400 and _error(resp) == "BRT-LANE-005"


def test_documents_with_a_bypass_model_are_refused(lane_api):
    lane_api["mp"].setenv("BORG_LANE_MODE", "raw_local")
    resp = lane_api["client"].post("/v1/chat/completions", json={
        "model": "openrouter/some/model", "messages": [{"role": "user", "content": "hi"}],
        "documents": {"files": ["note.txt"]}})
    assert resp.status_code == 400 and _error(resp) == "BRT-LANE-005"


def test_chat_documents_outside_the_root_are_refused(lane_api):
    lane_api["mp"].setenv("BORG_LANE_MODE", "raw_local")
    resp = lane_api["client"].post("/v1/chat/completions", json={
        "model": "borg_route", "messages": [{"role": "user", "content": "hi"}],
        "documents": {"folder": ".."}})
    assert resp.status_code == 400 and _error(resp) == "BRT-LANE-001"


@pytest.mark.parametrize("path,body", [
    ("/models/discover", None),
    ("/models/research", {"model_ids": ["x"]}),
])
def test_admin_outbound_actions_are_refused_in_all_local(lane_api, path, body):
    lane_api["mp"].setenv("BORG_LANE_MODE", "all_local")
    resp = lane_api["client"].post(path, json=body)
    assert resp.status_code == 403 and _error(resp) == "BRT-LANE-005"


@pytest.mark.parametrize("key,value", [
    ("lane_mode", "mostly_local"), ("lane_manifest", "titles"),
    ("lane_max_output_chars", "-3"), ("lane_max_ngram_share", "1.5"),
])
def test_an_invalid_lane_setting_is_refused(lane_api, key, value):
    resp = lane_api["client"].post("/config", json={"key": key, "value": value})
    assert resp.status_code == 400 and _error(resp) == "BRT-LANE-005"
