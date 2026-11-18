"""Planner reading (1) under ruling 35: a plan or re_plan reply that is valid
JSON but not an object (a list, a bare string) is treated exactly like an
unparseable reply: the JSON-only retry, then the BRT-ORC-002 failure path.
It used to crash the run at ``plan_result.get`` / ``replan_result.get``.
"""
import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

ANSWERER = {"type": "answerer", "id": "n1", "instruction": "Say hi.", "tier": "light",
            "output_schema": {"result": "string"}}
GOOD_PLAN = {"plan": {"nodes": [ANSWERER]}, "rationale": "scripted plan"}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "non_object.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-non-object")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _in_turn(*replies):
    left = list(replies)
    return lambda req: left.pop(0) if len(left) > 1 else left[0]


@pytest.mark.asyncio
async def test_a_plan_reply_that_is_a_json_list_retries_then_fails_with_002(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[], plan_content_for=lambda r: [ANSWERER]).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("plan")) == 2                       # the plan, then the JSON-only retry
    assert "CRITICAL: Your entire reply must be valid JSON only." in stub.of_kind("plan")[1].system
    assert result["status"] == "failed"
    assert "BRT-ORC-002" in (result.get("error") or ""), result.get("error")
    assert "AttributeError" not in (result.get("error") or "")


@pytest.mark.asyncio
async def test_a_plan_reply_that_is_a_json_list_recovers_on_the_retry(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[], plan_content_for=_in_turn([ANSWERER], GOOD_PLAN),
                      answer_for=lambda r: {"result": "hi"}).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("plan")) == 2 and stub.of_kind("answerer")


@pytest.mark.asyncio
async def test_a_re_plan_reply_that_is_a_bare_string_retries_then_fails_with_002(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[ANSWERER], replan_for=lambda r: '"compose now"',
                      answer_for=lambda r: {"result": "hi"}).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("re_plan")) == 2
    assert "CRITICAL: Your entire reply must be valid JSON only." in stub.of_kind("re_plan")[1].system
    assert result["status"] == "failed"
    assert "BRT-ORC-002" in (result.get("error") or ""), result.get("error")
    assert "AttributeError" not in (result.get("error") or "")


@pytest.mark.asyncio
async def test_a_re_plan_reply_that_is_a_bare_string_recovers_on_the_retry(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[ANSWERER],
                      replan_for=_in_turn('"compose now"', {"action": "compose", "rationale": "done"}),
                      answer_for=lambda r: {"result": "hi"}).install(mp)
    result = await run_pipeline("Say hi.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("re_plan")) == 2 and stub.of_kind("compose")
