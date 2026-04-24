"""TC-ORC-014 — engine._invoke_orchestrator_model honours resolve_model_call
for OpenRouter rows (correct URL, bearer header, stripped body model id,
max_tokens present).
"""
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.orchestrator.engine import _invoke_orchestrator_model
from src.registry.models import ModelInfo, Pricing


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all
    path = str(tmp_path / "orc_or.db")
    await create_all(path)
    return path


def _orch_response() -> dict:
    return {"direct_answer": "hello", "plan": None, "rationale": "simple"}


@pytest.mark.asyncio
async def test_invoke_orchestrator_uses_openrouter_correctly(monkeypatch, db):
    """TC-ORC-014 — openrouter orchestrator row → openrouter URL + bearer + stripped id."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "1234")

    orch_model = ModelInfo(
        model_id="openrouter/x-ai/grok-4",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=200000,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps(_orch_response())}}],
        "usage": {"prompt_tokens": 12, "completion_tokens": 3},
    }
    mock_resp.raise_for_status = MagicMock()

    post_calls = {}

    async def fake_post(url, json=None, headers=None):
        post_calls["url"] = url
        post_calls["json"] = json
        post_calls["headers"] = headers
        return mock_resp

    with patch("httpx.AsyncClient") as mock_cls:
        client = AsyncMock()
        client.post = AsyncMock(side_effect=fake_post)
        client.__aenter__ = AsyncMock(return_value=client)
        client.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = client

        parsed = await _invoke_orchestrator_model(
            mode="plan",
            run_id=uuid.uuid4(),
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
            orchestrator_model=orch_model,
            db_path=db,
        )

    assert parsed == _orch_response()
    assert post_calls["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert post_calls["headers"]["Authorization"] == "Bearer sk-or-test-xyz"
    assert post_calls["json"]["model"] == "x-ai/grok-4"
    assert post_calls["json"]["max_tokens"] == 1234
