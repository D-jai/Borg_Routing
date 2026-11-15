"""Tests for src/agents/executor.py.

TC-ANS-001 — execute(answerer) returns resolved InboundEnvelope when model responds correctly
TC-ANS-002 — governance is injected into system prompt
TC-ANS-003 — BRT-ANS-001 raised when model_endpoint is None
TC-ANS-004 — model returns unparseable JSON → failed envelope (not crash)
TC-ANS-005 — model response missing required output key → failed envelope (BRT-ANS-005)
TC-ANS-006 — scratch_cleanup directives applied after successful call
TC-ANS-007 — model_call event logged after successful call
TC-ANS-008 — HTTP error from model → BRT-ANS-002
TC-ANS-009 — finish_reason=length on first attempt → retry at doubled cap succeeds
TC-ANS-010 — finish_reason=length even after retry → BRT-ANS-009 failed envelope
TC-ANS-011 — envelope max_output_tokens acts as a floor over ANSWERER_MAX_TOKENS
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.db.schema import create_all
from src.db.state import get_config, set_config
from src.agents.executor import execute
from src.envelopes.schema import InboundEnvelope, WrappedTask


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "exec_test.db")
    await create_all(path)
    return path


def _task(run_id: uuid.UUID, endpoint: str = "http://localhost:1234", **kwargs) -> WrappedTask:
    return WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=run_id,
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        instruction="Classify this email into bug, feature, or question.",
        output_schema={"bucket": "bug|feature|question"},
        model_endpoint=endpoint,
        model_name="test-model",
        **kwargs,
    )


def _mock_http_response(content: str, tokens_in: int = 50, tokens_out: int = 20):
    """Build a mock httpx response that looks like an OpenAI chat-completions response."""
    body = {
        "choices": [{"message": {"content": content}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    }
    mock_resp = MagicMock()
    mock_resp.json.return_value = body
    mock_resp.raise_for_status = MagicMock()
    return mock_resp


@pytest.mark.asyncio
async def test_execute_answerer_resolved(db):
    """TC-ANS-001 — successful model call returns resolved InboundEnvelope."""
    run_id = uuid.uuid4()
    task = _task(run_id)
    model_output = json.dumps({
        "status": "resolved",
        "confidence": 0.9,
        "output": {"bucket": "bug"},
        "scratch_writes": [],
        "scratch_cleanup": [],
    })
    with patch("src.agents.executor._call_model", new=AsyncMock(return_value=(model_output, 50, 20, "stop"))):
        env = await execute(task, db_path=db)
    assert isinstance(env, InboundEnvelope)
    assert env.status == "resolved"
    assert env.output["bucket"] == "bug"
    assert env.tokens_used == {"input": 50, "output": 20}


@pytest.mark.asyncio
async def test_governance_injected(db):
    """TC-ANS-002 — global_rules from config appear in system prompt."""
    run_id = uuid.uuid4()
    task = _task(run_id)
    await set_config("global_rules", "Never output PII.", db_path=db)

    captured_prompt: list[str] = []

    async def mock_call(endpoint, system_prompt, user_msg, requested_max_tokens=None):
        captured_prompt.append(system_prompt)
        return json.dumps({"status": "resolved", "output": {"bucket": "bug"}}), 50, 20, "stop"

    with patch("src.agents.executor._call_model", new=AsyncMock(side_effect=mock_call)):
        await execute(task, db_path=db)

    assert "Never output PII." in captured_prompt[0]


@pytest.mark.asyncio
async def test_no_endpoint_raises():
    """TC-ANS-003 — BRT-ANS-001 raised when model_endpoint is None."""
    run_id = uuid.uuid4()
    task = _task(run_id, endpoint=None)
    with pytest.raises(ValueError, match="BRT-ANS-001"):
        await execute(task, db_path=":memory:")


@pytest.mark.asyncio
async def test_unparseable_json_returns_failed_envelope(db):
    """TC-ANS-004 — model returning unparseable text gives failed envelope."""
    run_id = uuid.uuid4()
    task = _task(run_id)
    with patch("src.agents.executor._call_model", new=AsyncMock(return_value=("not json at all", 10, 5, "stop"))):
        env = await execute(task, db_path=db)
    assert env.status == "failed"
    assert "BRT-ANS-004" in env.failure_reason


@pytest.mark.asyncio
async def test_missing_output_key_returns_failed_envelope(db):
    """TC-ANS-005 — output missing required key → failed envelope via BRT-ANS-005."""
    run_id = uuid.uuid4()
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=run_id,
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=1,
        max_depth=5,
        level_path="1a",
        instruction="Classify this email.",
        output_schema={"bucket": "bug|feature", "confidence": "float"},
        model_endpoint="http://localhost:1234",
        model_name="test-model",
    )
    # model returns output without 'confidence'
    model_output = json.dumps({"status": "resolved", "output": {"bucket": "bug"}})
    with patch("src.agents.executor._call_model", new=AsyncMock(return_value=(model_output, 30, 10, "stop"))):
        env = await execute(task, db_path=db)
    assert env.status == "failed"
    assert "BRT-ANS-005" in env.failure_reason


@pytest.mark.asyncio
async def test_scratch_cleanup_applied(db):
    """TC-ANS-006 — scratch_cleanup directives are executed after successful call."""
    from src.db.scratch import write_scratch, count_scratch_for_level
    run_id = uuid.uuid4()
    task = _task(run_id, side_effects=["deferrals"])

    # pre-write a scratch row the model will claim to clean up
    row_id = await write_scratch(run_id, "1a", task.envelope_id, "deferrals", {"x": 1}, db_path=db)

    model_output = json.dumps({
        "status": "resolved",
        "output": {"bucket": "bug"},
        "scratch_writes": [],
        "scratch_cleanup": [{"table": "deferrals", "action": "delete", "ids": [str(row_id)]}],
    })
    with patch("src.agents.executor._call_model", new=AsyncMock(return_value=(model_output, 30, 10, "stop"))):
        env = await execute(task, db_path=db)

    assert env.status == "resolved"
    count = await count_scratch_for_level(run_id, "1a", db_path=db)
    assert count == 0


@pytest.mark.asyncio
async def test_model_call_event_logged(db):
    """TC-ANS-007 — model_call event is logged after successful call."""
    import asyncio
    import aiosqlite
    run_id = uuid.uuid4()
    task = _task(run_id)
    model_output = json.dumps({"status": "resolved", "output": {"bucket": "bug"}})
    with patch("src.agents.executor._call_model", new=AsyncMock(return_value=(model_output, 40, 15, "stop"))):
        await execute(task, db_path=db)
    await asyncio.sleep(0.15)  # drain async tasks
    async with aiosqlite.connect(db) as conn:
        async with conn.execute("SELECT event_type, model_id FROM logs") as cur:
            rows = await cur.fetchall()
    assert any(r[0] == "model_call" and r[1] == "test-model" for r in rows)


@pytest.mark.asyncio
async def test_http_error_raises_brt_ans_002(db):
    """TC-ANS-008 — HTTP error from model → BRT-ANS-002."""
    import httpx
    run_id = uuid.uuid4()
    task = _task(run_id)

    async def raise_http(*args, **kwargs):
        request = httpx.Request("POST", "http://localhost:1234/v1/chat/completions")
        response = httpx.Response(500, request=request, text="Internal Server Error")
        raise httpx.HTTPStatusError("500", request=request, response=response)

    with patch("src.agents.executor._call_model", new=AsyncMock(side_effect=ValueError("BRT-ANS-002: HTTP 500"))):
        with pytest.raises(ValueError, match="BRT-ANS-002"):
            await execute(task, db_path=db)


@pytest.mark.asyncio
async def test_truncation_retry_succeeds(db):
    """TC-ANS-009 — _call_model auto-retries once on finish_reason=='length'.

    Simulates the OpenAI/OpenRouter wire format: first POST cuts off mid-JSON
    (finish_reason='length'); the executor's retry doubles max_tokens and
    the second POST returns clean JSON. Final envelope must be 'resolved'
    and reflect both calls' token usage.
    """
    import httpx
    run_id = uuid.uuid4()
    task = _task(run_id)

    truncated_payload = {
        "choices": [{
            "message": {"content": '{"status": "resolved", "output": {"bucket": "bu'},
            "finish_reason": "length",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 4000},
    }
    full_payload = {
        "choices": [{
            "message": {"content": json.dumps({"status": "resolved", "output": {"bucket": "bug"}})},
            "finish_reason": "stop",
        }],
        "usage": {"prompt_tokens": 100, "completion_tokens": 60},
    }

    call_count = {"n": 0}

    class _MockClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            call_count["n"] += 1
            body = truncated_payload if call_count["n"] == 1 else full_payload
            req = httpx.Request("POST", url)
            return httpx.Response(200, json=body, request=req)

    with patch("src.agents.executor.httpx.AsyncClient", _MockClient):
        env = await execute(task, db_path=db)

    assert call_count["n"] == 2, "executor must retry once on finish_reason=length"
    assert env.status == "resolved"
    assert env.output["bucket"] == "bug"
    # Tokens accumulate across both calls
    assert env.tokens_used["input"] == 200
    assert env.tokens_used["output"] == 4060


@pytest.mark.asyncio
async def test_truncation_retry_exhausted_returns_brt_ans_009(db):
    """TC-ANS-010 — both attempts truncate → BRT-ANS-009 failed envelope.

    When even the doubled-cap retry returns finish_reason='length' the
    executor must surface BRT-ANS-009 (distinct from BRT-ANS-004) so
    operators see "the cap is too small" instead of the misleading
    "unparseable JSON" symptom.
    """
    run_id = uuid.uuid4()
    task = _task(run_id)
    truncated_content = '{"status": "resolved", "output": {"bucket": "bu'
    with patch(
        "src.agents.executor._call_model",
        new=AsyncMock(return_value=(truncated_content, 200, 8000, "length")),
    ):
        env = await execute(task, db_path=db)
    assert env.status == "failed"
    assert "BRT-ANS-009" in env.failure_reason
    # Tokens-used still recorded so cost accounting works.
    assert env.tokens_used == {"input": 200, "output": 8000}


@pytest.mark.asyncio
async def test_envelope_max_output_tokens_acts_as_floor(db, monkeypatch):
    """TC-ANS-011 — task.max_output_tokens overrides ANSWERER_MAX_TOKENS up.

    When the orchestrator stamps a higher per-node cap on a WrappedTask,
    _call_model must pass that value (not the smaller env default) into
    the wire payload so the model has the headroom the planner asked for.
    """
    import httpx
    monkeypatch.setenv("ANSWERER_MAX_TOKENS", "2000")

    run_id = uuid.uuid4()
    # Plan node declared 9000 tokens for a Go-impl-plus-tests answer.
    task = _task(run_id)
    task = task.model_copy(update={"max_output_tokens": 9000})

    captured: dict = {}

    class _MockClient:
        def __init__(self, *a, **kw):
            pass
        async def __aenter__(self):
            return self
        async def __aexit__(self, *a):
            return False
        async def post(self, url, json=None, headers=None):
            captured["payload"] = json
            req = httpx.Request("POST", url)
            return httpx.Response(200, json={
                "choices": [{
                    "message": {"content": '{"status":"resolved","output":{"bucket":"bug"}}'},
                    "finish_reason": "stop",
                }],
                "usage": {"prompt_tokens": 50, "completion_tokens": 30},
            }, request=req)

    with patch("src.agents.executor.httpx.AsyncClient", _MockClient):
        env = await execute(task, db_path=db)

    assert env.status == "resolved"
    assert captured["payload"]["max_tokens"] == 9000, (
        "envelope max_output_tokens (9000) must beat ANSWERER_MAX_TOKENS (2000)"
    )
