"""TC-EXE-009 — executor._call_model handles OpenRouter rows: openrouter URL,
bearer auth header, stripped body model id, max_tokens in payload.
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.agents.executor import _call_model
from src.registry.models import ModelInfo, Pricing


@pytest.mark.asyncio
async def test_call_model_openrouter_wiring(monkeypatch):
    """TC-EXE-009 — answerer call to openrouter row → correct URL + headers + body."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-key")
    monkeypatch.setenv("ANSWERER_MAX_TOKENS", "321")

    model = ModelInfo(
        model_id="openrouter/anthropic/claude-sonnet-4",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=200000,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )

    post_calls = {}

    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    mock_resp.raise_for_status = MagicMock()

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

        content, tokens_in, tokens_out = await _call_model(model, "sys", "usr")

    assert content == "ok"
    assert tokens_in == 5
    assert tokens_out == 2
    assert post_calls["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert post_calls["headers"]["Authorization"] == "Bearer sk-or-key"
    assert post_calls["json"]["model"] == "anthropic/claude-sonnet-4"
    assert post_calls["json"]["max_tokens"] == 321
