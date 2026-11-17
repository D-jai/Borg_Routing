"""4.2 round-2 review (7): a run records the lane mode it ran under on EVERY path
that creates a run, and a re-run never rewrites it.

Paths: POST /v1/task, the chat/pipeline path (run_pipeline), MCP submit_task, a child
run, a direct-to-model bypass (non-streaming and streaming), and POST /runs/{id}/resume
(keeps the original mode). Nothing calls a live API.
"""
import asyncio
import sqlite3
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

import src.api.chat_completions as cc
import src.api.tasks as tasks_api
import src.pipeline.runner as runner
from main import app
from src.db.schema import create_all
from src.db.state import create_run, update_run_status


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / "lm.db")
    asyncio.run(create_all(path))
    for mod in (tasks_api, runner, cc):
        monkeypatch.setattr(mod, "DB_PATH", path, raising=False)
    monkeypatch.setattr("src.db.schema.DB_PATH", path)
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    return path


def modes(path):
    with sqlite3.connect(path) as c:
        return c.execute("SELECT kind, lane_mode FROM runs ORDER BY created_at").fetchall()


def client():
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def test_v1_task_records_raw_local(db):
    with patch("src.api.tasks._run_in_background", new=MagicMock()):
        resp = client().post("/v1/task", json={"instruction": "Say hi."})
    assert resp.status_code == 202
    assert modes(db) == [("orchestrated", "raw_local")]


@pytest.mark.asyncio
async def test_the_pipeline_chat_path_records_raw_local(db):
    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(return_value="ok")):
        result = await runner.run_pipeline(user_instruction="Say hi.", db_path=db)
    assert result["status"] == "completed"
    assert modes(db) == [("orchestrated", "raw_local")]


@pytest.mark.asyncio
async def test_mcp_submit_task_records_raw_local(db):
    from src.mcp import server as mcp_server

    with patch("src.api.tasks._run_in_background", new=AsyncMock()):
        await mcp_server._tool_submit_task({"instruction": "Say hi."})
        await asyncio.gather(*list(mcp_server._BACKGROUND_RUNS), return_exceptions=True)
    assert modes(db) == [("orchestrated", "raw_local")]


@pytest.mark.asyncio
async def test_a_child_run_records_the_lane_mode_of_its_lane(db):
    from src.agents.executor import execute
    from src.lane.context import lane_scope, prepare_lane
    from tests.orchestrator.test_slice3 import _make_task

    parent = await create_run("parent", db_path=db, lane_mode="raw_local")
    task = _make_task(parent, current_depth=0, max_depth=5, task_type="orchestrate")
    lane = await prepare_lane(None, db)
    with lane_scope(lane), \
            patch("src.orchestrator.engine.orchestrate_run", new=AsyncMock(return_value="child")), \
            patch("src.agents.executor.write_event", new=AsyncMock()):
        await execute(task, db_path=db)
    with sqlite3.connect(db) as c:
        row = c.execute("SELECT lane_mode FROM runs WHERE parent_run_id=?", (str(parent),)).fetchone()
    assert row == ("raw_local",)


def _bypass_model(monkeypatch):
    seen = {}

    async def fake(matrix, model_id, messages):
        from src.lane.context import current_lane
        seen["lane"] = current_lane()
        return "hi", {"prompt_tokens": 1, "completion_tokens": 1}

    monkeypatch.setattr(cc, "_bypass_to_model", fake)
    from main import matrix as m
    return m.all()[0].model_id, seen


def test_a_non_streaming_bypass_records_raw_local(db, monkeypatch):
    mid, seen = _bypass_model(monkeypatch)
    resp = client().post("/v1/chat/completions", json={
        "model": mid, "messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 200, resp.text
    assert seen["lane"] is not None
    assert modes(db) == [("bypass", "raw_local")]


def test_a_streaming_bypass_records_raw_local(db, monkeypatch):
    mid, _seen = _bypass_model(monkeypatch)
    resp = client().post("/v1/chat/completions", json={
        "model": mid, "stream": True, "messages": [{"role": "user", "content": "q"}]})
    assert resp.status_code == 200, resp.text
    assert modes(db) == [("bypass", "raw_local")]


def test_a_bypass_with_the_lane_off_records_off(db, monkeypatch):
    monkeypatch.setenv("BORG_LANE_MODE", "off")
    mid, _seen = _bypass_model(monkeypatch)
    client().post("/v1/chat/completions", json={
        "model": mid, "messages": [{"role": "user", "content": "q"}]})
    assert modes(db) == [("bypass", "off")]


@pytest.mark.asyncio
async def test_resume_keeps_the_runs_original_lane_mode(db, monkeypatch):
    """A run made with the lane off, resumed while the lane is on, still says off."""
    run = await create_run("q", db_path=db, lane_mode="off")
    await update_run_status(run, "failed", db_path=db)
    with patch("src.api.tasks._run_in_background", new=MagicMock()):
        resp = await asyncio.to_thread(client().post, f"/runs/{run}/resume")
    assert resp.status_code == 202, resp.text
    assert resp.json()["status"] == "resuming"
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT lane_mode FROM runs WHERE run_id=?", (str(run),)).fetchone() == ("off",)
