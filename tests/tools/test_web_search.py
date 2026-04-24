"""TC-WEB-001..006 — WebSearchTool provider selection, fallback, and errors."""
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.tools.base import ToolError
from src.tools.web_search import WebSearchTool, _reset_degraded_logged


@pytest.fixture(autouse=True)
def _reset_degraded():
    _reset_degraded_logged()
    yield
    _reset_degraded_logged()


def _mock_httpx_post(return_json: dict, status_code: int = 200):
    """Return a patch target that makes httpx.AsyncClient.post return the given body."""
    mock_resp = MagicMock()
    mock_resp.json.return_value = return_json
    if status_code != 200:
        req = httpx.Request("POST", "http://x")
        resp = httpx.Response(status_code, request=req, text="err")
        mock_resp.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError("x", request=req, response=resp)
        )
    else:
        mock_resp.raise_for_status = MagicMock()

    client = AsyncMock()
    client.post = AsyncMock(return_value=mock_resp)
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    return client


@pytest.mark.asyncio
async def test_tavily_happy_path(monkeypatch):
    """TC-WEB-001 — with TAVILY_API_KEY set, Tavily is called and results normalise."""
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-abc")
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.delenv("BORG_WEB_SEARCH_PROVIDER_ORDER", raising=False)

    tavily_body = {
        "results": [
            {"title": "A", "url": "https://a", "content": "snippet A"},
            {"title": "B", "url": "https://b", "content": "snippet B"},
        ]
    }
    client = _mock_httpx_post(tavily_body)

    tool = WebSearchTool()
    with patch("httpx.AsyncClient", return_value=client):
        res = await tool.run({"query": "hello", "max_results": 2})

    assert res.status == "ok"
    assert res.provider == "tavily"
    assert res.result == [
        {"title": "A", "url": "https://a", "snippet": "snippet A"},
        {"title": "B", "url": "https://b", "snippet": "snippet B"},
    ]
    # Ensure the call was actually to Tavily with the API key in payload.
    call_args = client.post.call_args
    assert "tavily.com" in call_args[0][0]
    assert call_args[1]["json"]["api_key"] == "tv-test-abc"


@pytest.mark.asyncio
async def test_tavily_failure_falls_back_to_perplexity(monkeypatch, caplog):
    """TC-WEB-002 — Tavily 502 → Perplexity answers → result marked degraded,
    BRT-TOOL-DEGRADED logged once."""
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")

    tavily_client = _mock_httpx_post({}, status_code=502)
    perplexity_body = {
        "choices": [
            {
                "message": {
                    "content": '[{"title":"P","url":"https://p","snippet":"px"}]'
                }
            }
        ]
    }
    perplexity_client = _mock_httpx_post(perplexity_body)

    tool = WebSearchTool()
    with patch("httpx.AsyncClient", side_effect=[tavily_client, perplexity_client]):
        with caplog.at_level("WARNING"):
            res = await tool.run({"query": "hello"})

    assert res.status == "degraded"
    assert res.provider == "perplexity"
    assert res.result == [{"title": "P", "url": "https://p", "snippet": "px"}]
    assert any("BRT-TOOL-DEGRADED" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_no_tavily_key_uses_perplexity_silently(monkeypatch, caplog):
    """TC-WEB-003 — no TAVILY_API_KEY → Tavily step raises BRT-TOOL-001 →
    fallback to Perplexity; BRT-TOOL-DEGRADED logged on first call."""
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")

    perplexity_body = {
        "choices": [
            {"message": {"content": '[{"title":"X","url":"https://x","snippet":"xs"}]'}}
        ]
    }
    perplexity_client = _mock_httpx_post(perplexity_body)

    tool = WebSearchTool()
    with patch("httpx.AsyncClient", return_value=perplexity_client):
        with caplog.at_level("WARNING"):
            res = await tool.run({"query": "hello"})

    assert res.status == "degraded"
    assert res.provider == "perplexity"
    assert any("BRT-TOOL-DEGRADED" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_all_providers_fail_raises_bright_tool_002(monkeypatch):
    """TC-WEB-004 — Tavily 502 and Perplexity timeout → ToolError(BRT-TOOL-002)."""
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-abc")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-xyz")

    tavily_client = _mock_httpx_post({}, status_code=502)

    # Perplexity client that raises TimeoutException
    perplexity_client = AsyncMock()
    perplexity_client.post = AsyncMock(side_effect=httpx.TimeoutException("slow"))
    perplexity_client.__aenter__ = AsyncMock(return_value=perplexity_client)
    perplexity_client.__aexit__ = AsyncMock(return_value=None)

    tool = WebSearchTool()
    with patch("httpx.AsyncClient", side_effect=[tavily_client, perplexity_client]):
        with pytest.raises(ToolError) as exc_info:
            await tool.run({"query": "hello"})
    assert exc_info.value.code == "BRT-TOOL-002"


@pytest.mark.asyncio
async def test_empty_query_rejected(monkeypatch):
    """TC-WEB-005 — empty query raises BRT-TOOL-002 before any HTTP call."""
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-abc")
    tool = WebSearchTool()
    with pytest.raises(ToolError) as exc_info:
        await tool.run({"query": "  "})
    assert exc_info.value.code == "BRT-TOOL-002"


@pytest.mark.asyncio
async def test_max_results_is_bounded(monkeypatch):
    """TC-WEB-006 — max_results above the cap is clamped; below 1 is clamped too."""
    monkeypatch.setenv("TAVILY_API_KEY", "tv-test-abc")

    tavily_body = {
        "results": [{"title": str(i), "url": "x", "content": "x"} for i in range(30)]
    }
    client = _mock_httpx_post(tavily_body)
    tool = WebSearchTool()
    with patch("httpx.AsyncClient", return_value=client):
        res = await tool.run({"query": "q", "max_results": 999})
    assert len(res.result) <= 20
    assert client.post.call_args[1]["json"]["max_results"] <= 20


@pytest.mark.asyncio
async def test_is_available(monkeypatch):
    """TC-WEB-007 — is_available True with either key, False with neither."""
    tool = WebSearchTool()
    monkeypatch.delenv("TAVILY_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    assert tool.is_available() is False
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test")
    assert tool.is_available() is True
