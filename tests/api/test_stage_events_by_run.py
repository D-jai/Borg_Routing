"""R3: the chat stream shows stage events of its own run only."""
import asyncio
import json
import time
import uuid

import pytest

from src.api.chat_completions import _poll_stage_events, _stream_orchestration_with_heartbeats
from src.db.schema import create_all
from src.db.state import create_run
from src.logging.writer import flush_pending, write_event


async def _two_runs(db):
    await create_all(db)
    mine = await create_run("mine", db_path=db)
    other = await create_run("someone else", db_path=db)
    await write_event("orchestrator_decision", run_id=mine,
                      decision={"mode": "plan", "msg": "my plan"}, db_path=db)
    await write_event("orchestrator_decision", run_id=other,
                      decision={"mode": "plan", "msg": "their plan"}, db_path=db)
    await flush_pending()
    await asyncio.sleep(0.15)
    return mine, other


@pytest.mark.asyncio
async def test_poll_returns_only_the_requested_runs_events(tmp_path):
    db = str(tmp_path / "r3.db")
    started = time.time() - 5
    mine, _other = await _two_runs(db)
    events, _ = await _poll_stage_events(started, 0, db, run_id=str(mine))
    assert [e["detail"] for e in events] == ["my plan"]


@pytest.mark.asyncio
async def test_poll_without_a_run_id_still_sees_every_run(tmp_path):
    db = str(tmp_path / "r3b.db")
    started = time.time() - 5
    await _two_runs(db)
    events, _ = await _poll_stage_events(started, 0, db)
    assert sorted(e["detail"] for e in events) == ["my plan", "their plan"]


@pytest.mark.asyncio
async def test_stream_passes_its_run_id_and_ignores_other_runs(tmp_path, monkeypatch):
    db = str(tmp_path / "r3c.db")
    await create_all(db)
    monkeypatch.setattr("src.api.chat_completions.DB_PATH", db)
    monkeypatch.setenv("BORG_STREAM_HEARTBEAT_SEC", "0.2")
    seen = {}

    async def pipeline(user_instruction, thread_window=None, run_id=None, **kw):
        seen["run_id"] = run_id
        other = await create_run("other", db_path=db)
        await write_event("orchestrator_decision", run_id=other,
                          decision={"mode": "plan", "msg": "FOREIGN"}, db_path=db)
        await write_event("orchestrator_decision", run_id=run_id,
                          decision={"mode": "plan", "msg": "OWN"}, db_path=db)
        await flush_pending()
        await asyncio.sleep(0.8)
        return {"run_id": str(run_id), "response": "done", "status": "completed",
                "latency_ms": 1, "envelope_count": 0, "error": None}

    monkeypatch.setattr("src.api.chat_completions.run_pipeline", pipeline)
    out = ""
    async for chunk in _stream_orchestration_with_heartbeats("hi", [], "borg_route"):
        out += chunk
    assert isinstance(seen["run_id"], uuid.UUID)
    assert "OWN" in out
    assert "FOREIGN" not in out


@pytest.mark.asyncio
async def test_poll_includes_the_chats_child_runs_but_not_unrelated_runs(tmp_path):
    db = str(tmp_path / "r3d.db")
    await create_all(db)
    started = time.time() - 5
    root = await create_run("chat", db_path=db)
    child = await create_run("child", parent_run_id=root, db_path=db)
    grandchild = await create_run("grandchild", parent_run_id=child, db_path=db)
    stranger = await create_run("another chat", db_path=db)
    for run, msg in ((root, "root plan"), (child, "child plan"),
                     (grandchild, "grandchild plan"), (stranger, "stranger plan")):
        await write_event("orchestrator_decision", run_id=run,
                          decision={"mode": "plan", "msg": msg}, db_path=db)
    await flush_pending()
    await asyncio.sleep(0.15)
    events, _ = await _poll_stage_events(started, 0, db, run_id=str(root))
    assert sorted(e["detail"] for e in events) == ["child plan", "grandchild plan", "root plan"]
