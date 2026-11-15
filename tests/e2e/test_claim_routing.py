"""The headline claim, as tests.

"BORG_ROUTE breaks a job into steps and sends each step to the cheapest
model that can do it, and shows what each step cost."

The real engine, executor and router run against a scripted stand-in for
the model servers (``stub_model_server.py``). Each test states one part of
the claim. Nothing here needs a key or the network.
"""
import sqlite3

import pytest

from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

OPERATOR_INSTRUCTION = (
    "From the 12 product reviews below, extract product and sentiment for each, "
    "write a Python function that tallies them, and recommend which product to stock.\n"
    "1. Kettle: boils fast, love it.\n"
    "2. Toaster: burnt everything."
)

EXTRACT = "Extract product and sentiment from each review."
CODE = "Write a Python function that tallies sentiment per product."
RECOMMEND = "Recommend which product to stock and justify it."

PLAN = [
    {"type": "answerer", "id": "n1", "instruction": EXTRACT,
     "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}},
    {"type": "answerer", "id": "n2", "instruction": CODE,
     "task_type": "coding", "tier": "mid", "output_schema": {"result": "string"}},
    {"type": "answerer", "id": "n3", "instruction": RECOMMEND,
     "task_type": "reasoning", "tier": "heavy", "output_schema": {"result": "string"}},
]

COMPOSED = "## Stock the kettle\n\n- Kettle: 1 positive\n- Toaster: 1 negative\n\n```python\ndef tally(rows):\n    return rows\n```\n"


@pytest.fixture
async def finished_run(tmp_path, monkeypatch):
    """One whole run through the real pipeline. Returns (stub, result, db_path)."""
    from src.db.schema import create_all

    db_path = str(tmp_path / "claim.db")
    await create_all(db_path)

    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-claim")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    for name in ("LEAF_QUALITY_LIGHT", "LEAF_QUALITY_MID", "LEAF_QUALITY_HEAVY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())

    stub = StubModels(plan_nodes=PLAN, compose_text=COMPOSED).install(monkeypatch)
    result = await run_pipeline(OPERATOR_INSTRUCTION, db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    return stub, result, db_path


@pytest.mark.asyncio
async def test_steps_are_served_by_more_than_one_model(finished_run):
    stub, _result, _db = finished_run
    models = {r.model for r in stub.of_kind("answerer")}
    assert len(stub.of_kind("answerer")) == 3
    assert len(models) >= 2, f"every step went to the same model: {models}"


@pytest.mark.asyncio
async def test_each_step_goes_to_the_cheapest_model_that_clears_its_floor(finished_run):
    stub, _result, _db = finished_run
    assert stub.step_request(EXTRACT).model == "stub/cheap"    # light: floor 6.0
    assert stub.step_request(CODE).model == "stub/mid"         # mid: floor 7.5
    assert stub.step_request(RECOMMEND).model == "stub/top"    # heavy: floor 9.0


@pytest.mark.asyncio
async def test_planning_and_composing_stay_on_the_top_model(finished_run):
    stub, _result, _db = finished_run
    for kind in ("plan", "re_plan", "compose"):
        assert {r.model for r in stub.of_kind(kind)} == {"stub/top"}, kind


@pytest.mark.xfail(strict=True, reason="work order 1.4: steps are given their own text, not the operator's")
@pytest.mark.asyncio
async def test_every_step_is_told_the_operators_exact_instruction(finished_run):
    """Law 1: governance carries the operator's words verbatim to every call."""
    stub, _result, _db = finished_run
    for req in stub.of_kind("answerer"):
        assert req.governance_instruction == OPERATOR_INSTRUCTION.strip(), (
            f"step on {req.model} was given: {req.governance_instruction!r}"
        )


@pytest.mark.xfail(strict=True, reason="work order 1.6: cost per call is not recorded yet")
@pytest.mark.asyncio
async def test_every_model_call_is_priced_and_the_run_total_is_their_sum(finished_run):
    _stub, result, db_path = finished_run
    with sqlite3.connect(db_path) as conn:
        calls = conn.execute(
            "SELECT model_id, dollars FROM logs WHERE run_id=? AND tokens_input IS NOT NULL",
            (result["run_id"],),
        ).fetchall()
        digest = conn.execute(
            "SELECT cost_dollars FROM run_digests WHERE run_id=?", (result["run_id"],)
        ).fetchone()
    assert len(calls) == 6  # plan, three steps, re_plan, compose
    for model_id, dollars in calls:
        assert dollars is not None and dollars > 0, f"{model_id} was not priced"
    assert digest is not None
    assert digest[0] == pytest.approx(sum(d for _m, d in calls))


@pytest.mark.xfail(strict=True, reason="work order 1.7: the stream drops line breaks")
@pytest.mark.asyncio
async def test_the_streamed_answer_keeps_every_character(finished_run):
    import json

    from src.api.chat_completions import _stream_response

    _stub, result, _db = finished_run
    assert result["response"] == COMPOSED
    streamed = ""
    async for line in _stream_response(result["response"], "borg_route", result["run_id"]):
        payload = line.removeprefix("data: ").strip()
        if not payload or payload == "[DONE]":
            continue
        streamed += json.loads(payload)["choices"][0]["delta"].get("content", "")
    assert streamed == COMPOSED
