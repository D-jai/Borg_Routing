"""Work order 1.5: GET /runs/{id} says why each step went to its model.

Same stand-in model servers as the cost test. The run is read back through
the real route function against a temp database.
"""
import json
import sqlite3
import uuid

import pytest
from fastapi import HTTPException

from src.api import tasks as tasks_api
from tests.e2e.test_cost_recording import _run

TOP_PRICES = (5e-6, 2.5e-5)  # stub/top: the baseline


async def _read(db_path, run_id, monkeypatch):
    monkeypatch.setattr(tasks_api, "DB_PATH", db_path)
    resp = await tasks_api.get_run_state(run_id)
    return json.loads(resp.body)


@pytest.mark.asyncio
async def test_every_routed_step_has_a_decision_with_candidates_and_baseline(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    body = await _read(db, result["run_id"], monkeypatch)
    envs = body["envelopes"]
    assert len(envs) == 3
    for env in envs:
        routing = env["routing"]
        assert routing is not None
        assert routing["mode"] == "route"
        assert len(routing["candidates"]) >= 1
        assert routing["chosen"] and routing["reason"]
        assert routing["baseline"] == "openrouter/stub/top"
        pin, pout = TOP_PRICES
        expected = (
            routing["est_input_tokens"] * pin + routing["est_output_tokens"] * pout
        )
        assert routing["baseline_est_cost"] == pytest.approx(expected, rel=1e-12)
        assert env["dollars"] > 0
        assert env["latency_ms"] is not None and env["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_a_configured_baseline_overrides_the_top_model(tmp_path, monkeypatch):
    monkeypatch.setenv("BASELINE_MODEL_ID", "openrouter/stub/mid")
    _stub, result, db = await _run(tmp_path, monkeypatch)
    # _run clears the env var; set it again for the run that matters.
    monkeypatch.setenv("BASELINE_MODEL_ID", "openrouter/stub/mid")
    from src.orchestrator import engine
    from tests.e2e.stub_model_server import StubModels, drain_log_writes
    from tests.e2e.test_claim_routing import COMPOSED, OPERATOR_INSTRUCTION, PLAN
    from src.pipeline.runner import run_pipeline

    StubModels(plan_nodes=PLAN, compose_text=COMPOSED).install(monkeypatch)
    result = await run_pipeline(OPERATOR_INSTRUCTION, db_path=db)
    await drain_log_writes()
    body = await _read(db, result["run_id"], monkeypatch)
    assert body["envelopes"]
    for env in body["envelopes"]:
        assert env["routing"]["baseline"] == "openrouter/stub/mid"


@pytest.mark.asyncio
async def test_envelope_without_a_route_event_gets_routing_null(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    run_id = result["run_id"]
    with sqlite3.connect(db) as conn:
        # A non-routed envelope: a copy of a real one with no route event.
        cols = [r[1] for r in conn.execute("PRAGMA table_info(envelopes)")]
        sel = ", ".join("?" if c == "envelope_id" else c for c in cols)
        conn.execute(
            f"INSERT INTO envelopes({', '.join(cols)}) "
            f"SELECT {sel} FROM envelopes WHERE run_id=? LIMIT 1",
            (str(uuid.uuid4()), run_id),
        )
        # And one routed envelope whose route event is lost.
        victim = conn.execute(
            "SELECT envelope_id FROM envelopes WHERE run_id=? ORDER BY created_at LIMIT 1",
            (run_id,),
        ).fetchone()[0]
        conn.execute(
            "DELETE FROM logs WHERE envelope_id=? AND event_type='orchestrator_decision'",
            (victim,),
        )
        conn.commit()
    body = await _read(db, run_id, monkeypatch)
    assert len(body["envelopes"]) == 4
    nulls = [e for e in body["envelopes"] if e["routing"] is None]
    assert len(nulls) == 2
    assert any(e["envelope_id"] == victim for e in nulls)
    assert sum(1 for e in body["envelopes"] if e["routing"] is not None) == 2


@pytest.mark.asyncio
async def test_unknown_run_is_still_404(tmp_path, monkeypatch):
    from src.db.schema import create_all

    db = str(tmp_path / "empty.db")
    await create_all(db)
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    with pytest.raises(HTTPException) as exc:
        await tasks_api.get_run_state(str(uuid.uuid4()))
    assert exc.value.status_code == 404
