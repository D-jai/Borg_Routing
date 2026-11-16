"""BRT-ORC-018 — the orchestrator call is repeated once at double the cap
when the reply was cut off (finish_reason == "length"), and BRT-ANS-009's
node-level cap: a plan node may ask for its own ``max_output_tokens``.
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.engine import _build_task_from_node, _invoke_orchestrator_model
from src.registry.models import ModelInfo, Pricing


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all
    path = str(tmp_path / "orc_trunc.db")
    await create_all(path)
    return path


def _resp(content: str, finish_reason: str) -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": finish_reason}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    resp.raise_for_status = MagicMock()
    return resp


def _plain_model() -> ModelInfo:
    # An id that matches none of the reasoning-family patterns, so the cap
    # comes straight from ORCHESTRATOR_MAX_TOKENS.
    return ModelInfo(
        model_id="openrouter/acme/plain-model",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=200000,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )


async def _invoke(
    db: str, responses: list[MagicMock], run_id: uuid.UUID | None = None
) -> tuple[dict, list[int]]:
    """Run one plan-mode call; return (parsed, max_tokens seen on each POST)."""
    caps: list[int] = []
    queue = list(responses)

    async def fake_post(url, json=None, headers=None):
        caps.append(json["max_tokens"])  # copy now: the payload dict is reused
        return queue.pop(0)

    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = client

        parsed = await _invoke_orchestrator_model(
            mode="plan",
            run_id=run_id or uuid.uuid4(),
            user_instruction="Hi",
            current_depth=0,
            max_depth=5,
            thread_window=[],
            rolling_summary="",
            stage_results={},
            child_scratch_digest={},
            skills_catalogue="(none)",
            governance={"global_rules": "", "admin_rules_ledger": []},
            available_models_brief="(none)",
            orchestrator_model=_plain_model(),
            db_path=db,
        )
    return parsed, caps


@pytest.mark.asyncio
async def test_orchestrator_retries_once_at_double_cap_when_cut_off(monkeypatch, db):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1000")
    full = {"direct_answer": "hello", "plan": None, "rationale": "simple"}

    parsed, caps = await _invoke(
        db,
        [_resp('{"direct_answer": "hel', "length"), _resp(json.dumps(full), "stop")],
    )

    assert parsed == full
    assert caps == [1000, 2000]


@pytest.mark.asyncio
async def test_orchestrator_does_not_retry_at_the_ceiling(monkeypatch, db):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "16000")
    full = {"direct_answer": "hello", "plan": None, "rationale": "simple"}

    parsed, caps = await _invoke(db, [_resp(json.dumps(full), "length")])

    assert parsed == full
    assert caps == [16000]


async def _decision_rows(db: str, run_id: uuid.UUID) -> list[tuple[int, int, dict]]:
    import aiosqlite
    from tests.e2e.stub_model_server import drain_log_writes
    await drain_log_writes()
    async with aiosqlite.connect(db) as conn:
        cur = await conn.execute(
            "SELECT tokens_input, tokens_output, decision_json FROM logs "
            "WHERE run_id = ? AND event_type = 'orchestrator_decision'",
            (str(run_id),),
        )
        rows = await cur.fetchall()
    return [(r[0], r[1], json.loads(r[2])) for r in rows]


@pytest.mark.asyncio
async def test_orchestrator_retry_is_recorded_on_the_decision_event(monkeypatch, db):
    """BRT-ORC-018 — the retry shows up on the orchestrator_decision row,
    with the tokens of both attempts summed."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1000")
    full = {"direct_answer": "hello", "plan": None, "rationale": "simple"}
    run_id = uuid.uuid4()

    await _invoke(
        db,
        [_resp('{"direct_answer": "hel', "length"), _resp(json.dumps(full), "stop")],
        run_id=run_id,
    )

    rows = await _decision_rows(db, run_id)
    assert len(rows) == 1
    tokens_in, tokens_out, decision = rows[0]
    assert decision["length_retry"] is True
    assert decision["code"] == "BRT-ORC-018"
    assert decision["retry_max_tokens"] == 2000
    assert (tokens_in, tokens_out) == (20, 10)  # 10+10 in, 5+5 out


@pytest.mark.asyncio
async def test_no_retry_flag_when_the_reply_was_not_cut_off(monkeypatch, db):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1000")
    full = {"direct_answer": "hello", "plan": None, "rationale": "simple"}
    run_id = uuid.uuid4()

    await _invoke(db, [_resp(json.dumps(full), "stop")], run_id=run_id)

    rows = await _decision_rows(db, run_id)
    assert len(rows) == 1
    tokens_in, tokens_out, decision = rows[0]
    assert "length_retry" not in decision
    assert "code" not in decision
    assert (tokens_in, tokens_out) == (10, 5)


def _answerer_node(**extra) -> dict:
    return {"type": "answerer", "id": "n1", "instruction": "Do it.", "output_schema": {}, **extra}


def _build(node: dict):
    return _build_task_from_node(
        node=node,
        run_id=uuid.uuid4(),
        current_depth=1,
        max_depth=5,
        level_path="1a",
        model_endpoint="http://localhost:1234",
        model_name="some-model",
        user_instruction="The user's request.",
    )


def test_plan_node_can_ask_for_its_own_token_cap():
    assert _build(_answerer_node(max_output_tokens=9000)).max_output_tokens == 9000


def test_unusable_node_token_cap_falls_back_to_the_schema_default():
    assert _build(_answerer_node(max_output_tokens="lots")).max_output_tokens == 4000
    assert _build(_answerer_node()).max_output_tokens == 4000
