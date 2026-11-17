"""One plain chat-completion call, priced from the matrix row.

Used by the direct arm and the judge. Both are outside BORG_ROUTE's own
pipeline, so this module builds the request the same way the app does
(``resolve_model_call``) and sends it with ``httpx.AsyncClient.post``; it
prices the call from the provider's reported usage and the row's prices.
"""
from __future__ import annotations

import httpx

from src.registry.cost import call_dollars
from src.registry.model_call import resolve_model_call

DEFAULT_TIMEOUT_S = 900.0


class ChatError(RuntimeError):
    """A model call failed; ``dollars`` is what was spent before the failure (0 if unknown)."""

    def __init__(self, message: str, dollars: float = 0.0):
        super().__init__(message)
        self.dollars = dollars


async def chat(model, system: str, user: str, max_tokens: int, timeout: float = DEFAULT_TIMEOUT_S) -> dict:
    """Return ``{content, tokens_in, tokens_out, dollars, finish_reason, model}``."""
    url, headers, body_model = resolve_model_call(model)
    payload = {
        "model": body_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0,
    }
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
            resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
            body = resp.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ChatError(f"call to {model.model_id} failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
    try:
        choice = body["choices"][0]
        content = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as exc:
        raise ChatError(f"malformed response from {model.model_id}: {exc!r}") from exc
    usage = body.get("usage") or {}
    tin = int(usage.get("prompt_tokens", 0) or 0)
    tout = int(usage.get("completion_tokens", 0) or 0)
    return {
        "content": content,
        "tokens_in": tin,
        "tokens_out": tout,
        "dollars": call_dollars(model, tin, tout),
        "finish_reason": (choice.get("finish_reason") or "").strip(),
        "model": model.model_id,
    }
