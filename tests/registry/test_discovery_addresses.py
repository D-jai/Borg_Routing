"""Work order 1.8 — local model servers by address.

Env vars: LMSTUDIO_BASE_URLS, OLLAMA_BASE_URL, LOCAL_OPENAI_BASE_URLS.
"""
import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.discovery import (
    discover_all,
    discover_lmstudio,
    discover_local_openai,
    discover_ollama,
    local_openai_base_urls,
    lmstudio_base_urls,
    ollama_base_url,
)

_ENV = ("LMSTUDIO_BASE_URLS", "OLLAMA_BASE_URL", "LOCAL_OPENAI_BASE_URLS")

OPENAI_LIST = {"data": [{"id": "my-model", "context_length": 16000}]}
OLLAMA_LIST = {"models": [{"name": "llama3:8b", "details": {}}]}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _resp(data):
    r = MagicMock()
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def _client(get):
    c = AsyncMock()
    c.get = get
    c.__aenter__ = AsyncMock(return_value=c)
    c.__aexit__ = AsyncMock(return_value=None)
    return c


# --- defaults unchanged ----------------------------------------------------

def test_defaults_reproduce_today(monkeypatch):
    assert lmstudio_base_urls() == [
        "http://localhost:1234",
        "http://localhost:1235",
        "http://localhost:1236",
        "http://localhost:1237",
    ]
    assert ollama_base_url() == "http://localhost:11434"
    assert local_openai_base_urls() == []


@pytest.mark.asyncio
async def test_default_probe_set_is_unchanged():
    seen = []

    async def get(url, **kw):
        seen.append(url)
        raise httpx.ConnectError("refused")

    with patch("httpx.AsyncClient", return_value=_client(get)):
        await discover_lmstudio()
        await discover_ollama()
        await discover_local_openai()

    assert seen == [
        "http://localhost:1234/v1/models",
        "http://localhost:1235/v1/models",
        "http://localhost:1236/v1/models",
        "http://localhost:1237/v1/models",
        "http://localhost:11434/api/tags",
    ]


# --- non-default addresses are probed --------------------------------------

@pytest.mark.asyncio
async def test_non_default_addresses_are_probed(monkeypatch):
    monkeypatch.setenv("LMSTUDIO_BASE_URLS", "http://192.168.1.5:1234, http://box:9000/")
    monkeypatch.setenv("OLLAMA_BASE_URL", "http://ollama-host:11500")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URLS", "http://vllm-host:8001")
    seen = []

    async def get(url, **kw):
        seen.append(url)
        if url.endswith("/api/tags"):
            return _resp(OLLAMA_LIST)
        return _resp(OPENAI_LIST)

    with patch("httpx.AsyncClient", return_value=_client(get)):
        lm = await discover_lmstudio()
        ol = await discover_ollama()
        lo = await discover_local_openai()

    assert seen == [
        "http://192.168.1.5:1234/v1/models",
        "http://box:9000/v1/models",
        "http://ollama-host:11500/api/tags",
        "http://vllm-host:8001/v1/models",
    ]
    assert {m.endpoint for m in lm} == {"http://192.168.1.5:1234", "http://box:9000"}
    assert ol[0].endpoint == "http://ollama-host:11500"
    assert len(lo) == 1
    assert lo[0].source == "custom_key"
    assert lo[0].endpoint == "http://vllm-host:8001"
    assert lo[0].model_id.endswith("/my-model")


# --- failing cases ---------------------------------------------------------

def test_malformed_entries_are_skipped(monkeypatch):
    monkeypatch.setenv("LMSTUDIO_BASE_URLS", "not a url,,  ,ftp://x:1,http://ok:1234")
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URLS", "garbage, http://good:8000")
    monkeypatch.setenv("OLLAMA_BASE_URL", "junk")
    assert lmstudio_base_urls() == ["http://ok:1234"]
    assert local_openai_base_urls() == ["http://good:8000"]
    assert ollama_base_url() == "http://localhost:11434"


@pytest.mark.asyncio
async def test_unreachable_custom_address_does_not_crash_discover_all(monkeypatch):
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URLS", "http://dead-host:8000")
    cm = CapabilityMatrix()

    async def get(url, **kw):
        raise httpx.ConnectError("refused")

    with patch("httpx.AsyncClient", return_value=_client(get)), patch(
        "src.registry.discovery.discover_openrouter", new_callable=AsyncMock
    ) as mock_or:
        mock_or.return_value = []
        count = await discover_all(cm)

    assert count == 0


@pytest.mark.asyncio
async def test_discover_all_includes_local_openai_rows(monkeypatch):
    monkeypatch.setenv("LOCAL_OPENAI_BASE_URLS", "http://vllm-host:8001")
    cm = CapabilityMatrix()

    async def get(url, **kw):
        if "vllm-host" in url:
            return _resp(OPENAI_LIST)
        raise httpx.ConnectError("refused")

    with patch("httpx.AsyncClient", return_value=_client(get)), patch(
        "src.registry.discovery.discover_openrouter", new_callable=AsyncMock
    ) as mock_or:
        mock_or.return_value = []
        count = await discover_all(cm)

    assert count == 1
    row = next(m for m in cm.all() if m.source == "custom_key")
    assert row.endpoint == "http://vllm-host:8001"
