"""Work order 1.6: every call's dollars are recorded and add up.

Same stand-in model servers as the claim test. Prices are chosen so the
expected dollars can be computed here from the tokens the stand-in reported.
"""
import json
import sqlite3

import pytest

from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from src.registry.models import ModelInfo
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix
from tests.e2e.test_claim_routing import COMPOSED, OPERATOR_INSTRUCTION, PLAN

# Per-token prices of the three stand-in models (see three_tier_matrix).
PRICES = {
    "stub/top": (5e-6, 2.5e-5),
    "stub/mid": (1e-6, 3e-6),
    "stub/cheap": (1e-7, 2e-7),
}


async def _run(tmp_path, monkeypatch, matrix=None, plan=PLAN):
    from src.db.schema import create_all

    db_path = str(tmp_path / "cost.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-cost")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.delenv("BASELINE_MODEL_ID", raising=False)
    for name in ("LEAF_QUALITY_LIGHT", "LEAF_QUALITY_MID", "LEAF_QUALITY_HEAVY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", matrix or three_tier_matrix())
    stub = StubModels(plan_nodes=plan, compose_text=COMPOSED).install(monkeypatch)
    result = await run_pipeline(OPERATOR_INSTRUCTION, db_path=db_path)
    await drain_log_writes()
    return stub, result, db_path


def _rows(db_path, run_id, where=""):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT event_type, model_id, tokens_input, tokens_output, dollars, baseline_dollars "
            f"FROM logs WHERE run_id=? AND tokens_input IS NOT NULL {where} ORDER BY log_id",
            (run_id,),
        ).fetchall()


@pytest.mark.asyncio
async def test_priced_answerer_calls_log_the_exact_product(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    rows = _rows(db, result["run_id"], "AND event_type='model_call'")
    assert len(rows) == 3
    for _et, model_id, tin, tout, dollars, _b in rows:
        pin, pout = PRICES[model_id.removeprefix("openrouter/")]
        assert dollars == pytest.approx(tin * pin + tout * pout, rel=1e-12)


@pytest.mark.asyncio
async def test_orchestrator_calls_plan_replan_compose_are_priced(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    rows = _rows(db, result["run_id"], "AND event_type='orchestrator_decision'")
    assert len(rows) == 3  # plan, re_plan, compose
    pin, pout = PRICES["stub/top"]
    for _et, model_id, tin, tout, dollars, _b in rows:
        assert model_id == "openrouter/stub/top"
        assert dollars == pytest.approx(tin * pin + tout * pout, rel=1e-12)
        assert dollars > 0


@pytest.mark.asyncio
async def test_baseline_dollars_are_the_same_tokens_at_the_top_models_prices(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    pin, pout = PRICES["stub/top"]
    rows = _rows(db, result["run_id"])
    assert len(rows) == 6
    for _et, _m, tin, tout, dollars, baseline in rows:
        assert baseline == pytest.approx(tin * pin + tout * pout, rel=1e-12)
        assert baseline >= dollars
    # The cheap step really did cost less than the baseline.
    cheap = [r for r in rows if r[1] == "openrouter/stub/cheap"]
    assert len(cheap) == 1 and cheap[0][5] > cheap[0][4]


@pytest.mark.asyncio
async def test_local_model_call_logs_zero_dollars_not_null(tmp_path, monkeypatch):
    """A model with no pricing (local) costs 0.0; the field is not left empty."""
    matrix = three_tier_matrix()
    local = ModelInfo(
        model_id="ollama/stub_local", source="ollama", endpoint="http://127.0.0.1:11434",
        context_length=8192, task_scores={t: 6.5 for t in engine_task_types()},
        throughput_tps=1e6, checked=True,
    )
    matrix.upsert(local)
    # Make the local row the only model clearing the light floor cheaply: the
    # router sorts by effective cost, and a fast measured local model with no
    # price beats the cheap one. (An unmeasured one is never free: ruling 28.)
    _stub, result, db = await _run(tmp_path, monkeypatch, matrix=matrix)
    rows = _rows(db, result["run_id"], "AND event_type='model_call'")
    local_rows = [r for r in rows if r[1] == "ollama/stub_local"]
    assert local_rows, f"no step ran on the local model: {[r[1] for r in rows]}"
    for _et, _m, tin, tout, dollars, _b in local_rows:
        assert dollars == 0.0 and dollars is not None


def engine_task_types():
    from tests.e2e.stub_model_server import TASK_TYPES

    return TASK_TYPES


@pytest.mark.asyncio
async def test_run_digest_total_comes_from_the_logged_rows(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    rows = _rows(db, result["run_id"])
    with sqlite3.connect(db) as conn:
        cost, tokens = conn.execute(
            "SELECT cost_dollars, token_count FROM run_digests WHERE run_id=?",
            (result["run_id"],),
        ).fetchone()
    assert cost == pytest.approx(sum(r[4] for r in rows))
    assert tokens == sum(r[2] + r[3] for r in rows)
    assert result["cost_dollars"] == pytest.approx(cost)


@pytest.mark.asyncio
async def test_envelope_rows_carry_token_counts_and_dollars(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch)
    with sqlite3.connect(db) as conn:
        envs = conn.execute(
            "SELECT model_id, tokens_input, tokens_output, dollars FROM envelopes WHERE run_id=?",
            (result["run_id"],),
        ).fetchall()
    assert len(envs) == 3
    for model_id, tin, tout, dollars in envs:
        pin, pout = PRICES[model_id.removeprefix("openrouter/")]
        assert tin and tout
        assert dollars == pytest.approx(tin * pin + tout * pout, rel=1e-12)


@pytest.mark.asyncio
async def test_reply_carries_usage_from_the_run(tmp_path, monkeypatch):
    """The OpenAI-format reply reports the run's real token totals."""
    from src.api import chat_completions as cc

    stub_holder = {}

    async def run_with_tmp_db(user_instruction, thread_window=None):
        stub, result, _db = await _run(tmp_path, monkeypatch)
        stub_holder["db"] = _db
        return result

    monkeypatch.setattr(cc, "run_pipeline", run_with_tmp_db)
    monkeypatch.setattr(cc, "_db_initialised", True)

    class FakeRequest:
        async def json(self):
            return {"model": "borg_route", "messages": [{"role": "user", "content": OPERATOR_INSTRUCTION}]}

    resp = await cc.chat_completions(FakeRequest())
    body = json.loads(resp.body)
    usage = body["usage"]
    rows = _rows(stub_holder["db"], body["id"].removeprefix("chatcmpl-"))
    assert usage["prompt_tokens"] == sum(r[2] for r in rows) > 0
    assert usage["completion_tokens"] == sum(r[3] for r in rows) > 0
    assert usage["total_tokens"] == usage["prompt_tokens"] + usage["completion_tokens"]
    assert body["choices"][0]["message"]["content"] == COMPOSED
