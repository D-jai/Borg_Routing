"""Slice BRT-OR-01 API-layer tests.

TC-API-024 — /v1/chat/completions (bypass mode) to openrouter row:
              openrouter URL + bearer + stripped body model id + max_tokens.
TC-API-025 — /v1/chat/completions bypass to openrouter row without
              OPENROUTER_API_KEY → BRT-ORC-013 surfaced to client.
TC-API-026 — POST /keys with name='OpenRouter' writes OPENROUTER_API_KEY=
              to .env (not KEY_OPENROUTER=).
"""
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app, matrix
from src.registry.models import ModelInfo, Pricing


def _install_openrouter_row(model_id: str = "openrouter/x-ai/grok-4") -> str:
    """Install a test OpenRouter row into the shared matrix; return model_id."""
    matrix.upsert(
        ModelInfo(
            model_id=model_id,
            source="openrouter",
            endpoint="https://openrouter.ai/api",
            context_length=200000,
            pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
        )
    )
    return model_id


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


# ---------------------------------------------------------------------------
# TC-API-024 — bypass to openrouter uses full wiring
# ---------------------------------------------------------------------------

def test_bypass_openrouter_wiring(client, monkeypatch):
    """TC-API-024 — bypass to openrouter row sends correct URL/headers/body."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-abc")
    monkeypatch.setenv("ANSWERER_MAX_TOKENS", "777")

    model_id = _install_openrouter_row()

    post_calls = {}

    async def fake_post(url, json=None, headers=None):
        post_calls["url"] = url
        post_calls["json"] = json
        post_calls["headers"] = headers
        resp = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "ok-bypass"}}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 1},
        }
        resp.raise_for_status = MagicMock()
        return resp

    with patch("httpx.AsyncClient") as mock_cls:
        cli = AsyncMock()
        cli.post = AsyncMock(side_effect=fake_post)
        cli.__aenter__ = AsyncMock(return_value=cli)
        cli.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = cli

        resp = client.post(
            "/v1/chat/completions",
            json={
                "model": model_id,
                "messages": [{"role": "user", "content": "hi"}],
            },
        )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"] == "ok-bypass"
    assert post_calls["url"] == "https://openrouter.ai/api/v1/chat/completions"
    assert post_calls["headers"]["Authorization"] == "Bearer sk-or-test-abc"
    assert post_calls["json"]["model"] == "x-ai/grok-4"
    assert post_calls["json"]["max_tokens"] == 777


# ---------------------------------------------------------------------------
# TC-API-025 — missing OPENROUTER_API_KEY surfaces BRT-ORC-013
# ---------------------------------------------------------------------------

def test_bypass_openrouter_missing_key(client, monkeypatch):
    """TC-API-025 — bypass to openrouter without key → BRT-ORC-013 via 4xx."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    model_id = _install_openrouter_row("openrouter/x-ai/grok-4-no-key")

    resp = client.post(
        "/v1/chat/completions",
        json={
            "model": model_id,
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    assert resp.status_code == 422
    assert "BRT-ORC-013" in resp.text


# ---------------------------------------------------------------------------
# TC-API-026 — POST /keys writes canonical OPENROUTER_API_KEY=
# ---------------------------------------------------------------------------

def test_post_keys_canonical_openrouter(client, tmp_path, monkeypatch):
    """TC-API-026 — name='OpenRouter' writes OPENROUTER_API_KEY= to .env."""
    env_path = tmp_path / ".env"
    env_path.write_text("KEY_OPENROUTER=stale-legacy-value\n", encoding="utf-8")

    # Run the endpoint with the tmp .env as CWD so relative ".env" points at it.
    monkeypatch.chdir(tmp_path)
    # Make sure the current env doesn't leak into the test.
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("KEY_OPENROUTER", raising=False)

    resp = client.post(
        "/keys",
        json={
            "name": "OpenRouter",
            "base_url": "https://openrouter.ai/api/v1",
            "api_key": "sk-or-fresh-value",
        },
    )
    assert resp.status_code == 200

    written = env_path.read_text(encoding="utf-8")
    assert "OPENROUTER_API_KEY=sk-or-fresh-value" in written
    # Legacy name is gone.
    assert "KEY_OPENROUTER=" not in written
    # Endpoint var still stored.
    assert "ENDPOINT_OPENROUTER=https://openrouter.ai/api/v1" in written
    # Process env now carries canonical key.
    assert os.environ.get("OPENROUTER_API_KEY") == "sk-or-fresh-value"
