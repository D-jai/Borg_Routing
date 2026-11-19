"""Per-call model timeouts are configurable (bench round-2 review, ruling (2), 17-Nov-2026).

``BORG_ANSWERER_CALL_TIMEOUT`` (default 120 s) and ``BORG_ORCHESTRATOR_CALL_TIMEOUT`` (default 180 s) are
read when each call is made. An invalid value (not a number, zero or negative) falls back to the default
with a warning. No network: ``send_outbound`` is replaced by a fake that records the timeout it was given.
"""
from __future__ import annotations

import logging
import uuid
from unittest.mock import MagicMock

import pytest

from src.agents import executor
from src.orchestrator import engine
from src.registry.models import ModelInfo, Pricing


def _resp(content: str = "ok") -> MagicMock:
    resp = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 5},
    }
    resp.raise_for_status = MagicMock()
    return resp


def _recorder(content: str = "ok"):
    seen: list[float] = []

    async def fake_send_outbound(url, payload, headers, timeout=None, **kw):
        seen.append(timeout)
        return _resp(content)

    return seen, fake_send_outbound


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BORG_ANSWERER_CALL_TIMEOUT", raising=False)
    monkeypatch.delenv("BORG_ORCHESTRATOR_CALL_TIMEOUT", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-timeouts")   # never sent: send_outbound is faked


# -- answerer ---------------------------------------------------------------------

async def _answerer_call(monkeypatch) -> list[float]:
    seen, fake = _recorder()
    monkeypatch.setattr(executor, "send_outbound", fake)
    await executor._post_chat_completion("http://stub/v1/chat/completions", {}, {"model": "m"})
    return seen


async def test_answerer_timeout_default_is_unchanged(monkeypatch):
    assert executor._MODEL_CALL_TIMEOUT_SEC == 120
    assert await _answerer_call(monkeypatch) == [120]


async def test_answerer_timeout_env_override_is_read_at_call_time(monkeypatch):
    monkeypatch.setenv("BORG_ANSWERER_CALL_TIMEOUT", "7200")
    assert await _answerer_call(monkeypatch) == [7200.0]
    monkeypatch.setenv("BORG_ANSWERER_CALL_TIMEOUT", "45.5")       # changed after import: still honoured
    assert await _answerer_call(monkeypatch) == [45.5]


@pytest.mark.parametrize("bad", ["abc", "0", "-5", "nan", "inf"])
async def test_answerer_timeout_invalid_value_falls_back_with_a_warning(monkeypatch, caplog, bad):
    monkeypatch.setenv("BORG_ANSWERER_CALL_TIMEOUT", bad)
    with caplog.at_level(logging.WARNING):
        assert await _answerer_call(monkeypatch) == [120]
    assert any("BORG_ANSWERER_CALL_TIMEOUT" in r.getMessage() for r in caplog.records)


# -- orchestrator ------------------------------------------------------------------

def _plain_model() -> ModelInfo:
    return ModelInfo(model_id="openrouter/acme/plain-model", source="openrouter",
                     endpoint="https://openrouter.ai/api", context_length=200000,
                     pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6))


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all
    path = str(tmp_path / "timeouts.db")
    await create_all(path)
    return path


async def _orchestrator_call(monkeypatch, db) -> list[float]:
    seen, fake = _recorder('{"action": "compose", "rationale": "x"}')
    monkeypatch.setattr(engine, "send_outbound", fake)
    await engine._invoke_orchestrator_model(
        mode="plan", run_id=uuid.uuid4(), user_instruction="Hi", current_depth=0, max_depth=5,
        thread_window=[], rolling_summary="", stage_results={}, child_scratch_digest={},
        skills_catalogue="(none)", governance={"global_rules": "", "admin_rules_ledger": []},
        available_models_brief="(none)", orchestrator_model=_plain_model(), db_path=db,
    )
    return seen


async def test_orchestrator_timeout_default_is_unchanged(monkeypatch, db):
    assert engine._MODEL_CALL_TIMEOUT == 180
    assert await _orchestrator_call(monkeypatch, db) == [180]


async def test_orchestrator_timeout_env_override_is_honoured(monkeypatch, db):
    monkeypatch.setenv("BORG_ORCHESTRATOR_CALL_TIMEOUT", "7200")
    assert await _orchestrator_call(monkeypatch, db) == [7200.0]


async def test_orchestrator_timeout_invalid_value_falls_back_with_a_warning(monkeypatch, db, caplog):
    monkeypatch.setenv("BORG_ORCHESTRATOR_CALL_TIMEOUT", "two hours")
    with caplog.at_level(logging.WARNING):
        assert await _orchestrator_call(monkeypatch, db) == [180]
    assert any("BORG_ORCHESTRATOR_CALL_TIMEOUT" in r.getMessage() for r in caplog.records)


def test_both_new_variables_are_documented():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2]
    for f in (".env.example", "docs/user-guide.md"):
        text = (root / f).read_text(encoding="utf-8")
        assert "BORG_ANSWERER_CALL_TIMEOUT" in text and "BORG_ORCHESTRATOR_CALL_TIMEOUT" in text, f
