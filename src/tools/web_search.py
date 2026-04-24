"""web_search tool — Tavily primary, Perplexity fallback (BRT-WEB-01).

Providers in order:
1. Tavily (https://api.tavily.com/search) — fast, raw results, cheap.
   Requires ``TAVILY_API_KEY``.
2. Perplexity Sonar via OpenRouter — degrades silently to this when Tavily
   fails or its key is missing. Requires ``OPENROUTER_API_KEY``.

Config knobs (``.env``):
- ``TAVILY_API_KEY``            — optional. If absent, Perplexity becomes primary.
- ``OPENROUTER_API_KEY``        — optional but required for the Perplexity
                                  fallback.
- ``BORG_WEB_SEARCH_MAX_RESULTS`` — default cap (1-20, default 5).
- ``BORG_WEB_SEARCH_TIMEOUT_SEC`` — per-provider HTTP timeout (default 8).
- ``BORG_WEB_SEARCH_PROVIDER_ORDER`` — comma-separated override, e.g.
                                  ``perplexity,tavily``. Default is
                                  ``tavily,perplexity``.

Returns a normalised list of ``{title, url, snippet}`` dicts regardless of
which provider answered — answerers never need to know where the results
came from.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Optional

import httpx

from src.tools.base import BaseTool, ToolError, ToolInvocationResult

logger = logging.getLogger(__name__)

_TAVILY_URL = "https://api.tavily.com/search"
_PERPLEXITY_URL = "https://openrouter.ai/api/v1/chat/completions"
_PERPLEXITY_MODEL = "perplexity/sonar"
_DEFAULT_PROVIDER_ORDER = ("tavily", "perplexity")
_MAX_RESULTS_CAP = 20

# Module-level flag so we only log BRT-TOOL-DEGRADED once per process
# (same pattern as BRT-ORC-007-DEGRADED in engine.py).
_degraded_logged = False


def _bounded_max_results(raw: Any) -> int:
    try:
        n = int(raw)
    except (TypeError, ValueError):
        n = int(os.getenv("BORG_WEB_SEARCH_MAX_RESULTS", "5"))
    return max(1, min(_MAX_RESULTS_CAP, n))


def _provider_order() -> tuple[str, ...]:
    raw = os.getenv("BORG_WEB_SEARCH_PROVIDER_ORDER", "")
    if not raw.strip():
        return _DEFAULT_PROVIDER_ORDER
    parts = tuple(p.strip().lower() for p in raw.split(",") if p.strip())
    return parts or _DEFAULT_PROVIDER_ORDER


def _timeout_sec() -> float:
    try:
        return float(os.getenv("BORG_WEB_SEARCH_TIMEOUT_SEC", "8"))
    except ValueError:
        return 8.0


class WebSearchTool(BaseTool):
    name = "web_search"

    def is_available(self) -> bool:
        """True if at least one provider has credentials wired."""
        return bool(os.getenv("TAVILY_API_KEY") or os.getenv("OPENROUTER_API_KEY"))

    async def run(self, args: dict) -> ToolInvocationResult:
        query = (args.get("query") or "").strip()
        if not query:
            raise ToolError(
                "BRT-TOOL-002",
                "web_search requires a non-empty 'query' arg",
            )

        max_results = _bounded_max_results(
            args.get("max_results", os.getenv("BORG_WEB_SEARCH_MAX_RESULTS", "5"))
        )
        order = _provider_order()

        last_error: Optional[str] = None
        t0 = time.monotonic()
        for idx, provider in enumerate(order):
            try:
                if provider == "tavily":
                    results = await self._call_tavily(query, max_results)
                elif provider == "perplexity":
                    results = await self._call_perplexity(query, max_results)
                else:
                    logger.debug("web_search: unknown provider '%s' in order", provider)
                    continue
            except ToolError as exc:
                last_error = str(exc)
                logger.debug("web_search %s failed: %s", provider, exc)
                continue

            latency_ms = int((time.monotonic() - t0) * 1000)
            is_fallback = idx > 0
            if is_fallback:
                self._note_degraded(provider, order)

            return ToolInvocationResult(
                tool=self.name,
                args={"query": query, "max_results": max_results},
                status="degraded" if is_fallback else "ok",
                result=results,
                provider=provider,
                latency_ms=latency_ms,
                extras={"provider_order": list(order)},
            )

        # Every provider failed.
        raise ToolError(
            "BRT-TOOL-002",
            f"web_search exhausted providers {list(order)}. last_error={last_error}",
        )

    # ------------------------------------------------------------------
    # Providers
    # ------------------------------------------------------------------

    async def _call_tavily(self, query: str, max_results: int) -> list[dict]:
        api_key = os.getenv("TAVILY_API_KEY")
        if not api_key:
            raise ToolError("BRT-TOOL-001", "TAVILY_API_KEY not set")

        payload = {
            "api_key": api_key,
            "query": query,
            "max_results": max_results,
            "search_depth": "basic",
        }
        try:
            async with httpx.AsyncClient(timeout=_timeout_sec()) as client:
                resp = await client.post(_TAVILY_URL, json=payload)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ToolError("BRT-TOOL-002", f"Tavily timeout: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise ToolError(
                "BRT-TOOL-002",
                f"Tavily HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            ) from exc
        except httpx.RequestError as exc:
            raise ToolError("BRT-TOOL-002", f"Tavily request error: {exc}") from exc

        data = resp.json()
        normalised: list[dict] = []
        for item in (data.get("results") or [])[:max_results]:
            normalised.append(
                {
                    "title": item.get("title", "") or "",
                    "url": item.get("url", "") or "",
                    "snippet": item.get("content", "") or item.get("snippet", "") or "",
                }
            )
        return normalised

    async def _call_perplexity(self, query: str, max_results: int) -> list[dict]:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            raise ToolError(
                "BRT-TOOL-001",
                "OPENROUTER_API_KEY not set (needed for Perplexity fallback)",
            )

        system = (
            "You are a web search assistant. Return up to "
            f"{max_results} results as a strict JSON array of objects with "
            "keys 'title', 'url', 'snippet'. No prose, no fences — pure JSON."
        )
        payload = {
            "model": _PERPLEXITY_MODEL,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": query},
            ],
            "temperature": 0.0,
            "max_tokens": 1000,
        }
        headers = {"Authorization": f"Bearer {api_key}"}
        try:
            async with httpx.AsyncClient(timeout=_timeout_sec()) as client:
                resp = await client.post(_PERPLEXITY_URL, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ToolError("BRT-TOOL-002", f"Perplexity timeout: {exc}") from exc
        except httpx.HTTPStatusError as exc:
            raise ToolError(
                "BRT-TOOL-002",
                f"Perplexity HTTP {exc.response.status_code}: {exc.response.text[:200]}",
            ) from exc
        except httpx.RequestError as exc:
            raise ToolError("BRT-TOOL-002", f"Perplexity request error: {exc}") from exc

        body = resp.json()
        try:
            content = body["choices"][0]["message"]["content"]
        except (KeyError, IndexError) as exc:
            raise ToolError(
                "BRT-TOOL-002",
                f"Perplexity response missing choices[0].message.content: {body!r:.200s}",
            ) from exc

        import json as _json

        # The model answers in JSON by instruction but may still wrap in
        # a fence or include a citations preamble — accept either shape.
        content = content.strip()
        if content.startswith("```"):
            # strip optional ```json ... ```
            content = content.strip("`").lstrip("json").strip()
        try:
            parsed = _json.loads(content)
        except _json.JSONDecodeError as exc:
            raise ToolError(
                "BRT-TOOL-002",
                f"Perplexity returned unparseable JSON: {content[:200]}",
            ) from exc

        if not isinstance(parsed, list):
            raise ToolError(
                "BRT-TOOL-002",
                f"Perplexity returned {type(parsed).__name__}, expected list",
            )

        normalised: list[dict] = []
        for item in parsed[:max_results]:
            if not isinstance(item, dict):
                continue
            normalised.append(
                {
                    "title": str(item.get("title", "")),
                    "url": str(item.get("url", "")),
                    "snippet": str(item.get("snippet", "") or item.get("content", "")),
                }
            )
        return normalised

    # ------------------------------------------------------------------
    # Degraded-mode logging (once per process)
    # ------------------------------------------------------------------

    @staticmethod
    def _note_degraded(provider: str, order: tuple[str, ...]) -> None:
        global _degraded_logged
        if _degraded_logged:
            return
        _degraded_logged = True
        primary = order[0] if order else "tavily"
        logger.warning(
            "BRT-TOOL-DEGRADED: web_search primary provider '%s' unavailable; "
            "falling back to '%s'. This message is emitted once per process.",
            primary,
            provider,
        )


# Reset helper for tests that want to see the DEGRADED log on a fresh run.
def _reset_degraded_logged() -> None:  # pragma: no cover
    global _degraded_logged
    _degraded_logged = False
