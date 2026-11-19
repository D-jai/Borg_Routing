"""Planner ruling 37: the live chat stream and the run view redact through one rule.

(2) With the lane on, a non-admin sees the same text for a stage in the chat stream's stage
events as in GET /runs/{id}'s events (both through ``tasks.redact_event``, decided by
``tasks.viewer_redacts``); with the lane off, both are unchanged.
(1) A raw step whose output FAILED the lane's output guard shows its code and document ids
only, in both views, for a non-admin.
"""
from __future__ import annotations

import asyncio
import json
import re
import time

import pytest

from src.api import tasks
from src.api.chat_completions import _poll_stage_events, _stream_orchestration_with_heartbeats
from src.db.schema import create_all
from src.db.state import create_run
from src.logging.writer import flush_pending, write_event
from tests.e2e.stub_model_server import lane_matrix
from tests.e2e.test_privacy_lane import (  # noqa: F401  (lane is a fixture)
    ACME_TEXT, BIRCH_TEXT, CANARIES, _copying_local, _run, lane,
)

SECRET = "the client said orchid fourteen"
STAGES = [
    {"mode": "plan", "msg": f"planning: {SECRET}", "parsed": {"plan": {"nodes": []}}},
    {"mode": "route", "node_id": "n1", "msg": "n1 -> ollama/x (routed)", "chosen": "ollama/x"},
    {"mode": "re_plan", "rationale": f"n1 found that {SECRET}"},
    {"code": "BRT-ORC-028", "msg": f"compose quoted: {SECRET}"},
]


class _Req:
    """A request as ``get_run_state`` reads it: the app's matrix (none here)."""

    class app:  # noqa: N801
        class state:  # noqa: N801
            matrix = None


async def _stage_rows(db, run_id, redact):
    out, cursor = [], 0
    while True:
        batch, cursor2 = await _poll_stage_events(0, cursor, db, run_id=str(run_id), redact=redact)
        out += batch
        if cursor2 == cursor:
            return out
        cursor = cursor2


async def _view(db, run_id, monkeypatch, lane_on: bool):
    async def lane_is_on():
        return lane_on

    monkeypatch.setattr(tasks, "DB_PATH", db)
    monkeypatch.setattr(tasks, "_lane_is_on", lane_is_on)
    monkeypatch.setattr(tasks, "lane_admin_allowed", lambda request=None: False)
    resp = await tasks.get_run_state(str(run_id), request=_Req())
    return json.loads(resp.body)


def _view_detail(ev) -> str:
    """The text the stream would show for this run-view event (the stream's own rule)."""
    d = json.loads(ev["decision_json"]) if ev.get("decision_json") else {}
    return str(d.get("msg") or d.get("rationale") or ev.get("model_id") or "")[:200]


async def _seeded(tmp_path):
    db = str(tmp_path / "r37.db")
    await create_all(db)
    run = await create_run("ask", db_path=db)
    for d in STAGES:
        await write_event("orchestrator_decision", run_id=run, model_id="cloud/planner",
                          decision=d, db_path=db)
    await flush_pending()
    await asyncio.sleep(0.15)
    return db, run


# --- (2) one rule for the stream and the run view ------------------------------------------

@pytest.mark.parametrize("lane_on", [True, False])
async def test_stream_and_run_view_show_the_same_text_for_each_stage(tmp_path, monkeypatch, lane_on):
    db, run = await _seeded(tmp_path)
    view = await _view(db, run, monkeypatch, lane_on)
    redact = await tasks.viewer_redacts(None)          # the same question the stream asks
    assert redact is lane_on
    stream = await _stage_rows(db, run, redact)
    shown = [_view_detail(ev) for ev in view["events"] if ev["event_type"] == "orchestrator_decision"]
    assert [s["detail"] for s in stream] == shown
    text = json.dumps([s["detail"] for s in stream])
    if lane_on:
        assert SECRET not in text and SECRET not in json.dumps(view["events"])
        assert "n1 -> ollama/x (routed)" in text          # a route decision is kept whole, in both
    else:
        assert text.count(SECRET) == 3                    # unchanged: msg, rationale, msg


async def test_the_live_stream_redacts_while_the_lane_is_on_and_not_when_off(tmp_path, monkeypatch):
    db = str(tmp_path / "r37s.db")
    await create_all(db)
    monkeypatch.setattr("src.api.chat_completions.DB_PATH", db)
    monkeypatch.setenv("BORG_STREAM_HEARTBEAT_SEC", "0.2")
    monkeypatch.setattr(tasks, "lane_admin_allowed", lambda request=None: False)

    async def pipeline(user_instruction, thread_window=None, run_id=None, **kw):
        await write_event("orchestrator_decision", run_id=run_id, model_id="cloud/planner",
                          decision={"mode": "plan", "rationale": SECRET}, db_path=db)
        await flush_pending()
        await asyncio.sleep(0.8)
        return {"run_id": str(run_id), "response": "done", "status": "completed",
                "latency_ms": 1, "envelope_count": 0, "error": None}

    monkeypatch.setattr("src.api.chat_completions.run_pipeline", pipeline)
    for lane_on in (True, False):
        async def lane_is_on(on=lane_on):
            return on

        monkeypatch.setattr(tasks, "_lane_is_on", lane_is_on)
        out = ""
        async for chunk in _stream_orchestration_with_heartbeats("hi", [], "borg_route"):
            out += chunk
        stages = [json.loads(line[5:]) for line in out.splitlines()
                  if line.startswith("data:") and '"event": "stage"' in line]
        assert stages, out
        assert (SECRET in out) is (not lane_on)
        if lane_on:
            assert stages[0]["stage"] == "orchestrator" and stages[0]["detail"] == "cloud/planner"


# --- (1) a raw step that failed its guard: code and document ids only -------------------------

def _probes() -> list:
    """Every sentence-sized piece of the two documents, and the canaries."""
    pieces = []
    for text in (ACME_TEXT, BIRCH_TEXT):
        pieces += [p.strip() for p in re.split(r"[.\n]", text) if len(p.strip()) >= 20]
    return pieces + list(CANARIES)


async def test_a_raw_step_that_failed_its_guard_shows_code_and_ids_only(lane, monkeypatch):
    stub, result = await _run(lane, "raw_local", lane_matrix(local_score=8.0), answer_for=_copying_local)
    run_id = result["run_id"]
    db = lane["db"]
    # Check the checker: the database itself holds the copied text (the local trace).
    import sqlite3

    with sqlite3.connect(db) as conn:
        dump = json.dumps([list(r) for t in ("logs", "log_payloads", "outbound_requests")
                           for r in conn.execute(f"SELECT * FROM {t}")])
    assert any(p in dump for p in _probes())
    view = await _view(db, run_id, monkeypatch, lane_on=True)
    stream = await _stage_rows(db, run_id, redact=True)
    for name, shown in (("run view", json.dumps(view)), ("stream", json.dumps(stream))):
        leaked = [p for p in _probes() if p in shown]
        assert leaked == [], f"{name} shows document text: {leaked[:2]}"
    # The step did fail its guard, and what is shown of it is a code and document ids.
    agg = [json.loads(e["inbound_payload"]) for e in view["envelopes"]
           if e["type"] == "apply_to_each" and e.get("inbound_payload")]
    assert agg, "no apply_to_each step in the run"
    failures = [f for a in agg for f in (a.get("output") or {}).get("failures") or []]
    assert failures, "the guard did not refuse the copying step"
    for f in failures:
        assert f["reason"] == {"code": "BRT-LANE-004", "doc": f["reason"]["doc"]}
        assert re.fullmatch(r"doc_\d{3,}(#\d+)?(,doc_\d{3,}(#\d+)?)*", f["reason"]["doc"])
    for ev in view["events"]:
        d = json.loads(ev["decision_json"]) if ev.get("decision_json") else None
        if ev["event_type"] == "error" and d is not None:
            assert set(d) <= {"code", "node_id", "severity", "model_id", "action", "failed_steps",
                              "step_codes"}, d
