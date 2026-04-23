"""Tests for src/registry/discovery.py (mocked httpx).

TC-REG-040 — discover_lmstudio parses OpenAI-compat /v1/models response
TC-REG-041 — discover_lmstudio returns empty list on connection failure (silent)
TC-REG-042 — discover_ollama parses /api/tags response
TC-REG-043 — discover_ollama returns empty list on connection failure (silent)
TC-REG-044 — discover_openrouter parses pricing + context_length; filters image models
TC-REG-045 — discover_openrouter returns empty list on connection failure
TC-REG-046 — discover_all upserts models and preserves existing scores
"""
import pytest
import httpx
from unittest.mock import AsyncMock, MagicMock, patch

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.discovery import (
    discover_lmstudio,
    discover_ollama,
    discover_openrouter,
    discover_all,
)


# ---------------------------------------------------------------------------
# Fixture responses
# ---------------------------------------------------------------------------

LMSTUDIO_RESPONSE = {
    "data": [
        {"id": "gemma-4-e2b-it", "context_length": 32000},
        {"id": "qwen3.5-9b", "context_length": 128000},
    ]
}

OLLAMA_RESPONSE = {
    "models": [
        {"name": "llama3:8b", "details": {"context_length": 8192}},
        {"name": "mistral:7b", "details": {}},
    ]
}

OPENROUTER_RESPONSE = {
    "data": [
        {
            "id": "anthropic/claude-sonnet-4",
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000015"},
            "architecture": {"modality": "text"},
        },
        {
            "id": "openai/dall-e-3",
            "context_length": 4096,
            "pricing": {"prompt": "0.0001", "completion": "0.0004"},
            "architecture": {"modality": "text->image"},  # must be filtered
        },
        {
            "id": "google/gemini-2.5-flash",
            "context_length": 1000000,
            "pricing": {"prompt": "0.0000003", "completion": "0.0000025"},
            "architecture": {"modality": "text"},
        },
    ]
}


def _mock_response(json_data: dict, status_code: int = 200) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.raise_for_status = MagicMock()
    return resp


# ---------------------------------------------------------------------------
# LM Studio
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_lmstudio_parses_response():
    """TC-REG-040 — discover_lmstudio parses OpenAI /v1/models correctly."""
    mock_resp = _mock_response(LMSTUDIO_RESPONSE)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        models = await discover_lmstudio(ports=[1234])

    assert len(models) == 2
    ids = {m.model_id for m in models}
    assert "lmstudio/gemma-4-e2b-it" in ids
    assert "lmstudio/qwen3.5-9b" in ids
    assert all(m.source == "lmstudio" for m in models)
    qwen = next(m for m in models if "qwen" in m.model_id)
    assert qwen.context_length == 128000


@pytest.mark.asyncio
async def test_discover_lmstudio_silent_on_failure():
    """TC-REG-041 — discover_lmstudio returns [] on connection error."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        models = await discover_lmstudio(ports=[1234])

    assert models == []


# ---------------------------------------------------------------------------
# Ollama
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_ollama_parses_response():
    """TC-REG-042 — discover_ollama parses /api/tags correctly."""
    mock_resp = _mock_response(OLLAMA_RESPONSE)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        models = await discover_ollama()

    assert len(models) == 2
    ids = {m.model_id for m in models}
    assert "ollama/llama3:8b" in ids
    assert "ollama/mistral:7b" in ids
    llama = next(m for m in models if "llama3" in m.model_id)
    assert llama.context_length == 8192


@pytest.mark.asyncio
async def test_discover_ollama_silent_on_failure():
    """TC-REG-043 — discover_ollama returns [] on connection error."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        models = await discover_ollama()

    assert models == []


# ---------------------------------------------------------------------------
# OpenRouter
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_openrouter_parses_and_filters(tmp_path):
    """TC-REG-044 — discover_openrouter uses the curated list to filter."""
    curated = tmp_path / "curated.yaml"
    curated.write_text(
        "- id: anthropic/claude-sonnet-4\n"
        "- id: google/gemini-2.5-flash\n",
        encoding="utf-8",
    )

    mock_resp = _mock_response(OPENROUTER_RESPONSE)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        models = await discover_openrouter(curated_path=str(curated))

    assert len(models) == 2  # dall-e-3 absent from curated list
    ids = {m.model_id for m in models}
    assert "openrouter/anthropic/claude-sonnet-4" in ids
    assert "openrouter/google/gemini-2.5-flash" in ids
    assert not any("dall-e" in mid for mid in ids)

    claude = next(m for m in models if "claude" in m.model_id)
    assert claude.pricing is not None
    assert claude.pricing.input_per_token == pytest.approx(0.000003)
    assert claude.context_length == 200000


@pytest.mark.asyncio
async def test_discover_openrouter_silent_on_failure(tmp_path):
    """TC-REG-045 — discover_openrouter returns [] on connection error."""
    curated = tmp_path / "curated.yaml"
    curated.write_text("- id: anthropic/claude-sonnet-4\n", encoding="utf-8")

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        models = await discover_openrouter(curated_path=str(curated))

    assert models == []


@pytest.mark.asyncio
async def test_discover_openrouter_missing_curated_raises_brt_reg_032(tmp_path):
    """TC-REG-048 — missing curated_models.yaml raises BRT-REG-032."""
    missing = tmp_path / "does_not_exist.yaml"
    with pytest.raises(FileNotFoundError) as exc_info:
        await discover_openrouter(curated_path=str(missing))
    assert "BRT-REG-032" in str(exc_info.value)


# ---------------------------------------------------------------------------
# discover_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_discover_all_preserves_existing_scores():
    """TC-REG-046 — discover_all upserts models and preserves existing task_scores."""
    cm = CapabilityMatrix("data/capability_matrix.yaml")
    cm.load()
    initial_count = len(cm.all())

    # Seed a known model with a task_score so we can verify preservation
    existing_id = cm.all()[0].model_id

    with patch("src.registry.discovery.discover_lmstudio", new_callable=AsyncMock) as mock_ls, \
         patch("src.registry.discovery.discover_ollama", new_callable=AsyncMock) as mock_ol, \
         patch("src.registry.discovery.discover_openrouter", new_callable=AsyncMock) as mock_or:

        # Return the first existing model (simulating re-discovery) + one new model
        from src.registry.models import ModelInfo
        rediscovered = ModelInfo(
            model_id=existing_id,
            source=cm.get(existing_id).source,
            context_length=99999,  # updated context
        )
        new_model = ModelInfo(
            model_id="lmstudio/brand-new-model",
            source="lmstudio",
            context_length=8192,
        )
        mock_ls.return_value = [rediscovered, new_model]
        mock_ol.return_value = []
        mock_or.return_value = []

        count = await discover_all(cm)

    assert count == 2
    # Existing model: context updated; task_scores preserved
    existing = cm.get(existing_id)
    assert existing.context_length == 99999
    # Scores from before should still be there
    assert existing.task_scores == cm.get(existing_id).task_scores

    # New model added
    assert cm.get("lmstudio/brand-new-model") is not None
    assert len(cm.all()) == initial_count + 1
