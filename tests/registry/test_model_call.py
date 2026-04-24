"""Tests for src/registry/model_call.py.

TC-REG-057 — resolve_model_call: openrouter row + key → URL + bearer + stripped id
TC-REG-058 — resolve_model_call: openrouter row + no key → BRT-ORC-013
TC-REG-059 — resolve_model_call: lmstudio row → local URL, no auth, tail body model
TC-REG-060 — resolve_model_call: empty endpoint → BRT-ORC-014
TC-REG-061 — discover_openrouter fills endpoint on every returned row
"""
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.registry.discovery import discover_openrouter
from src.registry.model_call import resolve_model_call
from src.registry.models import ModelInfo, Pricing


# ---------------------------------------------------------------------------
# resolve_model_call
# ---------------------------------------------------------------------------

def _or_row() -> ModelInfo:
    return ModelInfo(
        model_id="openrouter/x-ai/grok-4",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=200000,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )


def _lm_row() -> ModelInfo:
    return ModelInfo(
        model_id="lmstudio/google/gemma-4-26b-a4b",
        source="lmstudio",
        endpoint="http://localhost:1234",
        context_length=128000,
    )


def test_resolve_openrouter_row_with_key(monkeypatch):
    """TC-REG-057 — openrouter row + OPENROUTER_API_KEY → correct shape."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-123")
    monkeypatch.delenv("OPENROUTER_APP_URL", raising=False)
    monkeypatch.delenv("OPENROUTER_APP_NAME", raising=False)

    url, headers, body_model = resolve_model_call(_or_row())

    assert url == "https://openrouter.ai/api/v1/chat/completions"
    assert headers["Authorization"] == "Bearer sk-or-test-123"
    assert headers["HTTP-Referer"]  # default present
    assert headers["X-Title"] == "BORG_ROUTE"
    # body model id has the "openrouter/" prefix stripped, publisher kept.
    assert body_model == "x-ai/grok-4"


def test_resolve_openrouter_row_without_key_raises(monkeypatch):
    """TC-REG-058 — openrouter row + no key → raises BRT-ORC-013."""
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    with pytest.raises(ValueError) as exc:
        resolve_model_call(_or_row())
    assert "BRT-ORC-013" in str(exc.value)


def test_resolve_lmstudio_row_local_url_no_auth():
    """TC-REG-059 — lmstudio row → local URL, no auth, tail body model."""
    url, headers, body_model = resolve_model_call(_lm_row())
    assert url == "http://localhost:1234/v1/chat/completions"
    assert headers == {}
    # strip leading "lmstudio/" and keep the rest verbatim (publisher/model-id)
    assert body_model == "google/gemma-4-26b-a4b"


def test_resolve_empty_endpoint_raises():
    """TC-REG-060 — row with blank endpoint → BRT-ORC-014."""
    row = ModelInfo(
        model_id="openrouter/orphan/model",
        source="openrouter",
        endpoint=None,
        context_length=4096,
    )
    with pytest.raises(ValueError) as exc:
        resolve_model_call(row)
    assert "BRT-ORC-014" in str(exc.value)


# ---------------------------------------------------------------------------
# discover_openrouter endpoint backfill
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_openrouter_fills_endpoint(tmp_path):
    """TC-REG-061 — every OpenRouter row returned from discovery has an endpoint."""
    curated = tmp_path / "curated.yaml"
    curated.write_text(
        "- id: anthropic/claude-sonnet-4\n- id: google/gemini-2.5-flash\n",
        encoding="utf-8",
    )

    payload = {
        "data": [
            {
                "id": "anthropic/claude-sonnet-4",
                "context_length": 200000,
                "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            },
            {
                "id": "google/gemini-2.5-flash",
                "context_length": 1000000,
                "pricing": {"prompt": "0.0000003", "completion": "0.0000025"},
            },
        ]
    }
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = payload
    mock_resp.raise_for_status = MagicMock()

    with patch("httpx.AsyncClient") as mock_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_cls.return_value = mock_client

        models = await discover_openrouter(curated_path=str(curated))

    assert len(models) == 2
    assert all(m.endpoint == "https://openrouter.ai/api" for m in models)
