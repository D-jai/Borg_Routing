"""Work order 1.6 review fixes: cost is recorded for every paid call.

Covers child runs, paid-then-failed calls, the log flush, the Cost Summary
and run receipt, bypass requests, background runs, retries and apply_to_each.
Every test uses a temp database and a patched HTTP post.
"""
import asyncio
import json
import sqlite3
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.agents.executor import execute
from src.db.state import compute_and_save_digest, create_run
from src.envelopes.schema import WrappedTask
from src.orchestrator import engine
from src.orchestrator.engine import _invoke_orchestrator_model
from src.pipeline.runner import run_pipeline
from src.registry.models import ModelInfo, Pricing
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

ROOT = "Review the client notes and say which contract is at risk."
PRICE_IN, PRICE_OUT = 1e-6, 3e-6   # stub/mid


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all

    path = str(tmp_path / "review.db")
    await create_all(path)
    return path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-review")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.delenv("BASELINE_MODEL_ID", raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return monkeypatch


def _q(db, sql, *args):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, args).fetchall()


def _log_sum(db, run_ids):
    marks = ",".join("?" for _ in run_ids)
    return _q(
        db,
        "SELECT COALESCE(SUM(dollars),0), COALESCE(SUM(baseline_dollars),0), "
        "COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0) "
        f"FROM logs WHERE event_type IN ('model_call','orchestrator_decision') "
        f"AND run_id IN ({marks})",
        *run_ids,
    )[0]


def _answerer_task(run_id, model="openrouter/stub/mid"):
    return WrappedTask(
        envelope_id=uuid.uuid4(), run_id=run_id, subtask_id=uuid.uuid4(),
        type="answerer", current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="https://openrouter.ai/api", model_name=model,
        instruction="Standalone step.", output_schema={"result": "string"},
    )


# --- 1. child runs ---------------------------------------------------------

@pytest.mark.asyncio
async def test_child_run_calls_are_in_the_parent_digest_and_envelope(db, env):
    sub_task = "Work out which contract is at risk."
    leaf = "Read the note and name the risk."

    def plan_for(req):
        if req.user == ROOT:
            return [{"type": "orchestrate", "id": "n1", "instruction": sub_task,
                     "output_schema": {}}]
        return [{"type": "answerer", "id": "c1", "instruction": leaf,
                 "tier": "light", "output_schema": {"result": "string"}}]

    StubModels(plan_nodes=[], plan_for=plan_for).install(env)
    result = await run_pipeline(ROOT, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    root = result["run_id"]
    (child,) = [r[0] for r in _q(db, "SELECT run_id FROM runs WHERE parent_run_id=?", root)]
    root_only = _log_sum(db, [root])
    child_total = _log_sum(db, [child])
    assert child_total[0] > 0 and root_only[0] > 0

    tree = _log_sum(db, [root, child])
    digest = _q(
        db,
        "SELECT cost_dollars, token_count, baseline_dollars, tokens_input, tokens_output "
        "FROM run_digests WHERE run_id=?", root,
    )[0]
    assert digest[0] == pytest.approx(tree[0]) and digest[0] > root_only[0]
    assert digest[1] == tree[2] + tree[3]
    assert digest[2] == pytest.approx(tree[1])          # baseline stored (defect 5)
    assert (digest[3], digest[4]) == (tree[2], tree[3])  # token split stored (defect 5)

    assert result["cost_dollars"] == pytest.approx(tree[0])
    assert result["usage"]["total_tokens"] == tree[2] + tree[3]

    env_dollars = _q(
        db, "SELECT dollars, tokens_input, tokens_output FROM envelopes "
        "WHERE run_id=? AND type='orchestrate'", root)[0]
    assert env_dollars[0] == pytest.approx(child_total[0])
    assert (env_dollars[1], env_dollars[2]) == (child_total[2], child_total[3])


# --- 2. paid then failed ---------------------------------------------------

def _length_then_error(first_usage):
    calls = []

    async def fake_post(client, url, json=None, headers=None, **kwargs):  # noqa: A002
        calls.append(json)
        if len(calls) == 1:
            resp = MagicMock()
            resp.json.return_value = {
                "choices": [{"message": {"content": '{"cut'}, "finish_reason": "length"}],
                "usage": first_usage,
            }
            resp.raise_for_status = MagicMock()
            return resp
        raise httpx.RequestError("boom")

    return fake_post


@pytest.mark.asyncio
async def test_answerer_first_attempt_is_logged_when_the_retry_fails(db, env, monkeypatch):
    monkeypatch.setattr(
        httpx.AsyncClient, "post",
        _length_then_error({"prompt_tokens": 1000, "completion_tokens": 2000}),
    )
    run_id = await create_run("x", db_path=db)
    with pytest.raises(ValueError, match="BRT-ANS-002"):
        await execute(_answerer_task(run_id), db_path=db)
    await drain_log_writes()

    rows = _q(db, "SELECT event_type, tokens_input, tokens_output, dollars FROM logs "
                  "WHERE run_id=?", str(run_id))
    assert [r[:3] for r in rows] == [("model_call", 1000, 2000)]
    assert rows[0][3] == pytest.approx(1000 * PRICE_IN + 2000 * PRICE_OUT)
    digest = await compute_and_save_digest(run_id, db_path=db)
    assert digest["cost_dollars"] == pytest.approx(1000 * PRICE_IN + 2000 * PRICE_OUT)


def _length_then_error_body(first_usage):
    """First response is cut by the cap; the retry is HTTP 200 with an error body."""
    calls = []

    async def fake_post(client, url, json=None, headers=None, **kwargs):  # noqa: A002
        calls.append(json)
        resp = MagicMock()
        if len(calls) == 1:
            resp.json.return_value = {
                "choices": [{"message": {"content": '{"cut'}, "finish_reason": "length"}],
                "usage": first_usage,
            }
        else:
            resp.json.return_value = {"error": {"message": "x"}}
        resp.raise_for_status = MagicMock()
        return resp

    return fake_post


@pytest.mark.asyncio
async def test_answerer_first_attempt_is_logged_when_the_retry_is_a_200_error_body(
    db, env, monkeypatch
):
    monkeypatch.setattr(
        httpx.AsyncClient, "post",
        _length_then_error_body({"prompt_tokens": 1000, "completion_tokens": 2000}),
    )
    run_id = await create_run("x", db_path=db)
    with pytest.raises(ValueError, match="BRT-ANS-002"):
        await execute(_answerer_task(run_id), db_path=db)
    await drain_log_writes()

    rows = _q(db, "SELECT event_type, tokens_input, tokens_output, dollars FROM logs "
                  "WHERE run_id=?", str(run_id))
    assert [r[:3] for r in rows] == [("model_call", 1000, 2000)]
    assert rows[0][3] == pytest.approx(1000 * PRICE_IN + 2000 * PRICE_OUT)


def _orc_model():
    return ModelInfo(
        model_id="openrouter/acme/plain-model", source="openrouter",
        endpoint="https://openrouter.ai/api", context_length=200000,
        pricing=Pricing(input_per_token=PRICE_IN, output_per_token=PRICE_OUT),
    )


def _orc_resp(content, finish, usage):
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish}],
        "usage": usage,
    }
    resp.raise_for_status = MagicMock()
    return resp


async def _invoke_plan(db, run_id, queue):
    """Plan-mode orchestrator call; ``queue`` items are responses or exceptions."""
    async def fake_post(url, json=None, headers=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = client
        return await _invoke_orchestrator_model(
            mode="plan", run_id=run_id, user_instruction="Hi", current_depth=0,
            max_depth=5, thread_window=[], rolling_summary="", stage_results={},
            child_scratch_digest={}, skills_catalogue="(none)",
            governance={"global_rules": "", "admin_rules_ledger": []},
            available_models_brief="(none)", orchestrator_model=_orc_model(), db_path=db,
        )


@pytest.mark.asyncio
async def test_orchestrator_first_attempt_is_logged_when_the_length_retry_fails(db, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-x")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1000")
    run_id = await create_run("x", db_path=db)
    queue = [
        _orc_resp('{"cut', "length", {"prompt_tokens": 1000, "completion_tokens": 2000}),
        httpx.RequestError("boom"),
    ]
    with pytest.raises(ValueError, match="BRT-ORC-001"):
        await _invoke_plan(db, run_id, queue)
    await drain_log_writes()

    rows = _q(db, "SELECT event_type, tokens_input, tokens_output, dollars FROM logs "
                  "WHERE run_id=?", str(run_id))
    assert [r[:3] for r in rows] == [("orchestrator_decision", 1000, 2000)]
    assert rows[0][3] == pytest.approx(1000 * PRICE_IN + 2000 * PRICE_OUT)
    digest = await compute_and_save_digest(run_id, db_path=db)
    assert digest["cost_dollars"] == pytest.approx(1000 * PRICE_IN + 2000 * PRICE_OUT)


@pytest.mark.asyncio
async def test_orchestrator_first_attempt_is_logged_when_the_retry_is_a_200_error_body(
    db, monkeypatch
):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-x")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1000")
    run_id = await create_run("x", db_path=db)
    err = MagicMock()
    err.json.return_value = {"error": {"message": "x"}}
    err.raise_for_status = MagicMock()
    queue = [
        _orc_resp('{"cut', "length", {"prompt_tokens": 1000, "completion_tokens": 2000}),
        err,
    ]
    with pytest.raises(ValueError, match="BRT-ORC-001"):
        await _invoke_plan(db, run_id, queue)
    await drain_log_writes()

    rows = _q(db, "SELECT event_type, tokens_input, tokens_output, dollars FROM logs "
                  "WHERE run_id=?", str(run_id))
    assert [r[:3] for r in rows] == [("orchestrator_decision", 1000, 2000)]
    assert rows[0][3] == pytest.approx(1000 * PRICE_IN + 2000 * PRICE_OUT)


# --- 3. flush_pending ------------------------------------------------------

@pytest.mark.asyncio
async def test_digest_waits_for_slow_log_writes(db, env, monkeypatch):
    """With a slow log writer the digest still equals the logged rows."""
    from src.logging import writer

    original = writer._write_event_task

    async def slow(*args, **kwargs):
        await asyncio.sleep(0.3)
        return await original(*args, **kwargs)

    monkeypatch.setattr(writer, "_write_event_task", slow)
    plan = [{"type": "answerer", "id": "n1", "instruction": "Pick the client.",
             "tier": "light", "output_schema": {"result": "string"}}]
    StubModels(plan_nodes=plan).install(env)
    result = await run_pipeline(ROOT, db_path=db)
    await drain_log_writes()

    total = _log_sum(db, [result["run_id"]])
    assert total[0] > 0
    assert result["cost_dollars"] == pytest.approx(total[0])
    assert result["usage"]["total_tokens"] == total[2] + total[3]


# --- 4. Cost Summary and GET /runs -----------------------------------------

@pytest.mark.asyncio
async def test_cost_summary_includes_priced_orchestrator_calls_once(db, monkeypatch):
    from src.api import logs as logs_api

    monkeypatch.setattr(logs_api, "DB_PATH", db)
    with sqlite3.connect(db) as conn:
        conn.executemany(
            "INSERT INTO logs(run_id, event_type, model_id, tokens_input, tokens_output, dollars) "
            "VALUES (?,?,?,?,?,?)",
            [
                ("r", "model_call", "m/a", 10, 5, 0.01),
                ("r", "orchestrator_decision", "m/top", 20, 8, 0.02),
                ("r", "orchestrator_decision", None, None, None, None),   # route event
            ],
        )
    body = json.loads((await logs_api.cost_summary()).body)
    assert body["totals"]["total_cost"] == pytest.approx(0.03)
    assert body["totals"]["total_calls"] == 2
    assert {r["model_id"] for r in body["by_model"]} == {"m/a", "m/top"}


@pytest.mark.asyncio
async def test_get_run_returns_the_digest(db, env, monkeypatch):
    from src.api import tasks as tasks_api

    plan = [{"type": "answerer", "id": "n1", "instruction": "Pick the client.",
             "tier": "light", "output_schema": {"result": "string"}}]
    StubModels(plan_nodes=plan).install(env)
    result = await run_pipeline(ROOT, db_path=db)
    await drain_log_writes()

    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    body = json.loads((await tasks_api.get_run_state(result["run_id"])).body)
    d = body["digest"]
    assert d["cost_dollars"] == pytest.approx(result["cost_dollars"]) and d["cost_dollars"] > 0
    assert d["baseline_dollars"] == pytest.approx(result["baseline_dollars"])
    assert d["tokens_input"] + d["tokens_output"] == result["usage"]["total_tokens"]


# --- 6. bypass -------------------------------------------------------------

def _bypass_matrix(monkeypatch):
    from main import matrix

    matrix.upsert(ModelInfo(
        model_id="openrouter/bypass/cheap", source="openrouter",
        endpoint="https://openrouter.ai/api", context_length=100000,
        pricing=Pricing(input_per_token=1e-6, output_per_token=2e-6), checked=False,
    ))
    matrix.upsert(ModelInfo(
        model_id="openrouter/bypass/base", source="openrouter",
        endpoint="https://openrouter.ai/api", context_length=100000,
        pricing=Pricing(input_per_token=1e-5, output_per_token=2e-5), checked=False,
    ))
    monkeypatch.setenv("BASELINE_MODEL_ID", "openrouter/bypass/base")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-bypass")
    return matrix


class _FakeRequest:
    def __init__(self, model, stream=False):
        self._body = {"model": model, "stream": stream,
                      "messages": [{"role": "user", "content": "hi there"}]}

    async def json(self):
        return self._body


def _post_returning(usage):
    async def fake_post(client, url, json=None, headers=None, **kwargs):  # noqa: A002
        resp = MagicMock()
        body = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
        if usage is not None:
            body["usage"] = usage
        resp.json.return_value = body
        resp.raise_for_status = MagicMock()
        return resp

    return fake_post


@pytest.mark.asyncio
async def test_bypass_reply_reports_provider_usage_and_logs_a_priced_row(db, monkeypatch):
    from src.api import chat_completions as cc

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc, "_db_initialised", True)
    monkeypatch.setattr(
        httpx.AsyncClient, "post",
        _post_returning({"prompt_tokens": 11, "completion_tokens": 7}),
    )
    resp = await cc.chat_completions(_FakeRequest("openrouter/bypass/cheap"))
    body = json.loads(resp.body)
    assert body["usage"] == {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18}

    run_id = body["id"].removeprefix("chatcmpl-")
    rows = _q(db, "SELECT event_type, model_id, tokens_input, tokens_output, dollars, "
                  "baseline_dollars FROM logs WHERE run_id=?", run_id)
    assert len(rows) == 1
    et, model, tin, tout, dollars, baseline = rows[0]
    assert (et, model, tin, tout) == ("model_call", "openrouter/bypass/cheap", 11, 7)
    assert dollars == pytest.approx(11 * 1e-6 + 7 * 2e-6)
    assert baseline == pytest.approx(11 * 1e-5 + 7 * 2e-5)
    digest = _q(db, "SELECT cost_dollars, tokens_input, tokens_output FROM run_digests "
                    "WHERE run_id=?", run_id)[0]
    assert digest == (pytest.approx(dollars), 11, 7)


@pytest.mark.asyncio
async def test_bypass_without_provider_usage_leaves_usage_out(db, monkeypatch):
    from src.api import chat_completions as cc

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc, "_db_initialised", True)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post_returning(None))
    body = json.loads((await cc.chat_completions(_FakeRequest("openrouter/bypass/cheap"))).body)
    assert "usage" not in body
    assert body["choices"][0]["message"]["content"] == "hello"


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, lines):
        self._lines = lines

    async def aiter_lines(self):
        for line in self._lines:
            yield line


class _FakeStreamClient:
    lines: list[str] = []

    def __init__(self, *a, **kw):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return None

    def stream(self, method, url, json=None, headers=None):
        outer = self

        class _Ctx:
            async def __aenter__(self_inner):
                return _FakeStreamResponse(outer.lines)

            async def __aexit__(self_inner, *exc):
                return None

        return _Ctx()


async def _relay(cc, lines):
    from main import matrix

    _FakeStreamClient.lines = lines
    run_id = str(uuid.uuid4())
    from src.api.chat_completions import ChatMessage

    out = [c async for c in cc._stream_bypass_relay(
        matrix, "openrouter/bypass/cheap", "openrouter/bypass/cheap",
        [ChatMessage(role="user", content="hi there")], run_id)]
    await drain_log_writes()
    return run_id, out


@pytest.mark.asyncio
async def test_streaming_bypass_logs_usage_from_the_final_chunk(db, monkeypatch):
    from src.api import chat_completions as cc

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc.httpx, "AsyncClient", _FakeStreamClient)
    lines = [
        'data: {"choices":[{"delta":{"content":"hel"}}]}',
        'data: {"choices":[{"delta":{"content":"lo"}}],"usage":{"prompt_tokens":4,"completion_tokens":2}}',
        "data: [DONE]",
    ]
    run_id, out = await _relay(cc, lines)
    assert out[-1].strip() == "data: [DONE]"
    rows = _q(db, "SELECT tokens_input, tokens_output, dollars FROM logs WHERE run_id=?", run_id)
    assert len(rows) == 1 and rows[0][:2] == (4, 2)
    assert rows[0][2] == pytest.approx(4 * 1e-6 + 2 * 2e-6)


@pytest.mark.asyncio
async def test_streaming_bypass_without_usage_logs_no_tokens(db, monkeypatch):
    from src.api import chat_completions as cc

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc.httpx, "AsyncClient", _FakeStreamClient)
    run_id, _out = await _relay(cc, ['data: {"choices":[{"delta":{"content":"hi"}}]}', "data: [DONE]"])
    rows = _q(db, "SELECT tokens_input, tokens_output, dollars FROM logs WHERE run_id=?", run_id)
    assert rows == [(None, None, None)]     # absent, not zero


class _FakeBrokenStreamResponse(_FakeStreamResponse):
    async def aiter_lines(self):
        yield 'data: {"choices":[{"delta":{"content":"par"}}]}'
        raise httpx.ReadError("connection dropped")


@pytest.mark.asyncio
async def test_streaming_bypass_logs_a_row_when_the_provider_errors_mid_stream(db, monkeypatch):
    from src.api import chat_completions as cc

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc.httpx, "AsyncClient", _FakeStreamClient)
    monkeypatch.setattr(
        _FakeStreamClient, "stream",
        lambda self, method, url, json=None, headers=None: _BrokenCtx(), raising=False,
    )
    run_id, out = await _relay(cc, [])
    assert any("Streaming relay failed" in c for c in out)
    rows = _q(db, "SELECT event_type, tokens_input, tokens_output, dollars FROM logs "
                  "WHERE run_id=?", run_id)
    assert rows == [("model_call", None, None, None)]


class _BrokenCtx:
    async def __aenter__(self):
        return _FakeBrokenStreamResponse([])

    async def __aexit__(self, *exc):
        return None


@pytest.mark.asyncio
async def test_streaming_bypass_logs_a_row_when_the_client_disconnects(db, monkeypatch):
    from main import matrix
    from src.api import chat_completions as cc
    from src.api.chat_completions import ChatMessage

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc.httpx, "AsyncClient", _FakeStreamClient)
    _FakeStreamClient.lines = [
        'data: {"choices":[{"delta":{"content":"a"}}]}',
        'data: {"choices":[{"delta":{"content":"b"}}]}',
        "data: [DONE]",
    ]
    run_id = str(uuid.uuid4())
    gen = cc._stream_bypass_relay(
        matrix, "openrouter/bypass/cheap", "openrouter/bypass/cheap",
        [ChatMessage(role="user", content="hi there")], run_id)
    await gen.__anext__()
    await gen.aclose()          # client went away: GeneratorExit inside the relay
    await drain_log_writes()
    rows = _q(db, "SELECT tokens_input, tokens_output FROM logs WHERE run_id=?", run_id)
    assert rows == [(None, None)]


@pytest.mark.asyncio
async def test_bypass_runs_are_tagged_and_left_out_of_the_run_list(db, monkeypatch):
    from src.api import chat_completions as cc
    from src.api import tasks as tasks_api

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc, "_db_initialised", True)
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post_returning({"prompt_tokens": 1, "completion_tokens": 1}))
    body = json.loads((await cc.chat_completions(_FakeRequest("openrouter/bypass/cheap"))).body)
    bypass_id = body["id"].removeprefix("chatcmpl-")
    normal_id = str(await create_run("a normal run", db_path=db))

    assert _q(db, "SELECT kind FROM runs WHERE run_id=?", bypass_id) == [("bypass",)]
    assert _q(db, "SELECT kind FROM runs WHERE run_id=?", normal_id) == [("orchestrated",)]

    default = json.loads((await tasks_api.list_runs()).body)["runs"]
    assert [r["run_id"] for r in default] == [normal_id]
    everything = json.loads((await tasks_api.list_runs(include_bypass=True)).body)["runs"]
    assert {r["run_id"] for r in everything} == {normal_id, bypass_id}


@pytest.mark.asyncio
async def test_a_bypass_run_is_still_returned_by_id(db, monkeypatch):
    from src.api import chat_completions as cc
    from src.api import tasks as tasks_api

    _bypass_matrix(monkeypatch)
    monkeypatch.setattr(cc, "DB_PATH", db)
    monkeypatch.setattr(cc, "_db_initialised", True)
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    monkeypatch.setattr(httpx.AsyncClient, "post", _post_returning({"prompt_tokens": 1, "completion_tokens": 1}))
    body = json.loads((await cc.chat_completions(_FakeRequest("openrouter/bypass/cheap"))).body)
    bypass_id = body["id"].removeprefix("chatcmpl-")
    got = json.loads((await tasks_api.get_run_state(bypass_id)).body)
    assert got["run"]["run_id"] == bypass_id and got["run"]["kind"] == "bypass"


# --- 7. POST /tasks background run -----------------------------------------

@pytest.mark.asyncio
async def test_background_task_run_gets_a_digest(db, env):
    from src.api.tasks import _run_in_background

    plan = [{"type": "answerer", "id": "n1", "instruction": "Pick the client.",
             "tier": "light", "output_schema": {"result": "string"}}]
    StubModels(plan_nodes=plan).install(env)
    run_id = await create_run(ROOT, db_path=db)
    await _run_in_background(ROOT, str(run_id), db)
    await drain_log_writes()

    total = _log_sum(db, [str(run_id)])
    assert total[0] > 0
    digest = _q(db, "SELECT cost_dollars, token_count FROM run_digests WHERE run_id=?",
                str(run_id))
    assert digest and digest[0][0] == pytest.approx(total[0])
    assert digest[0][1] == total[2] + total[3]


# --- 9. missing coverage ---------------------------------------------------

@pytest.mark.asyncio
async def test_json_retry_row_is_priced_separately(db, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-x")
    run_id = await create_run("x", db_path=db)
    good = {"direct_answer": "hello", "plan": None, "rationale": "simple"}
    queue = [
        _orc_resp("not json at all", "stop", {"prompt_tokens": 10, "completion_tokens": 5}),
        _orc_resp(json.dumps(good), "stop", {"prompt_tokens": 20, "completion_tokens": 8}),
    ]
    assert await _invoke_plan(db, run_id, queue) == good
    await drain_log_writes()
    # Each attempt's row is written by its own fire-and-forget task, so the
    # insert order (log_id) is not guaranteed; order by the attempt's tokens.
    rows = _q(db, "SELECT tokens_input, tokens_output, dollars FROM logs WHERE run_id=? "
                  "AND event_type='orchestrator_decision' ORDER BY tokens_input", str(run_id))
    assert [r[:2] for r in rows] == [(10, 5), (20, 8)]
    assert rows[0][2] == pytest.approx(10 * PRICE_IN + 5 * PRICE_OUT)
    assert rows[1][2] == pytest.approx(20 * PRICE_IN + 8 * PRICE_OUT)


@pytest.mark.asyncio
async def test_orchestrator_length_retry_sums_both_attempts(db, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-x")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1000")
    run_id = await create_run("x", db_path=db)
    good = {"direct_answer": "hello", "plan": None, "rationale": "simple"}
    queue = [
        _orc_resp('{"direct', "length", {"prompt_tokens": 100, "completion_tokens": 1000}),
        _orc_resp(json.dumps(good), "stop", {"prompt_tokens": 100, "completion_tokens": 40}),
    ]
    await _invoke_plan(db, run_id, queue)
    await drain_log_writes()
    rows = _q(db, "SELECT tokens_input, tokens_output, dollars FROM logs WHERE run_id=? "
                  "AND event_type='orchestrator_decision'", str(run_id))
    assert len(rows) == 1 and rows[0][:2] == (200, 1040)
    assert rows[0][2] == pytest.approx(200 * PRICE_IN + 1040 * PRICE_OUT)


@pytest.mark.asyncio
async def test_tool_rows_log_zero_dollars_not_null(db):
    from src.agents.executor import _execute_answerer
    from src.envelopes.schema import ToolRequest
    from src.tools.base import BaseTool, ToolInvocationResult
    from src.tools.registry import TOOL_REGISTRY, register_tool

    class _Tool(BaseTool):
        name = "tool_zero_dollars"

        async def run(self, args):
            return ToolInvocationResult(tool=self.name, args=args, status="ok",
                                        result={"hits": []}, provider="stub")

    original = dict(TOOL_REGISTRY)
    register_tool(_Tool())
    try:
        task = _answerer_task(uuid.uuid4(), model="lmstudio/test-model").model_copy(
            update={"tool_requests": [ToolRequest(tool="tool_zero_dollars", args={})],
                    "model_endpoint": "http://localhost:1234"})

        async def fake_call(model, system_prompt, user_msg, requested_max_tokens=None):
            return json.dumps({"status": "resolved", "output": {"result": "ok"}}), 10, 5, "stop"

        with patch("src.agents.executor._call_model", side_effect=fake_call):
            await _execute_answerer(task, db_path=db)
        await drain_log_writes()
    finally:
        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(original)

    rows = _q(db, "SELECT dollars FROM logs WHERE model_id='tool:tool_zero_dollars'")
    assert rows == [(0.0,)]


@pytest.mark.asyncio
async def test_apply_to_each_items_are_priced_and_the_envelope_is_their_sum(db, env):
    plan = [{"type": "apply_to_each", "id": "n1", "instruction": "Label this note.",
             "items": ["alpha", "beta", "gamma"], "tier": "light",
             "output_schema": {"result": "string"}}]
    StubModels(plan_nodes=plan).install(env)
    result = await run_pipeline(ROOT, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    items = _q(db, "SELECT tokens_input, tokens_output, dollars FROM logs "
                   "WHERE run_id=? AND event_type='model_call'", result["run_id"])
    assert len(items) == 3
    pin, pout = 1e-7, 2e-7   # stub/cheap
    for tin, tout, dollars in items:
        assert dollars == pytest.approx(tin * pin + tout * pout)
    parent = _q(db, "SELECT dollars, tokens_input, tokens_output, baseline_dollars "
                    "FROM envelopes WHERE run_id=? AND type='apply_to_each'", result["run_id"])[0]
    assert parent[0] == pytest.approx(sum(i[2] for i in items))
    assert (parent[1], parent[2]) == (sum(i[0] for i in items), sum(i[1] for i in items))
    assert parent[3] is not None
