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
    """A model call failed; ``dollars`` is what was spent before the failure (0 if unknown).

    ``transport`` is True only for a provider or network failure (HTTP 429 or 5xx, a connection error, a
    connect or read timeout): the bench reruns such a run once. A bad request, an auth error or an unreadable
    answer is not transport."""

    def __init__(self, message: str, dollars: float = 0.0, transport: bool = False):
        super().__init__(message)
        self.dollars = dollars
        self.transport = transport


def is_transport_exception(exc: BaseException) -> bool:
    """True for HTTP 429 or 5xx, a connection error, or a timeout (connect, read, write, pool)."""
    if isinstance(exc, httpx.HTTPStatusError):
        code = exc.response.status_code
        return code == 429 or 500 <= code <= 599
    return isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError))


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
        raise ChatError(f"call to {model.model_id} failed: {type(exc).__name__}: {str(exc)[:200]}",
                        transport=is_transport_exception(exc)) from exc
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


def answerer_caps(model, requested_max_tokens=None) -> tuple[int, "int | None"]:
    """``(first_cap, retry_cap)``: BORG_ROUTE's own answerer cap policy, reused, not copied.

    ``src.agents.executor._answerer_caps``: ``ANSWERER_MAX_TOKENS`` (4x, at least
    8000, for reasoning-family models) with ``requested_max_tokens`` as a floor;
    the retry cap is double that, at most 16000, or None when the first cap is
    already at the ceiling.
    """
    from src.agents.executor import _answerer_caps  # noqa: PLC0415  (pure function, no side effects)

    return _answerer_caps(model, requested_max_tokens)


async def chat_capped(model, system: str, user: str, requested_max_tokens=None, send=None) -> dict:
    """One call under the answerer cap policy: one retry at the retry cap on ``finish_reason == "length"``.

    Returns ``chat``'s fields summed over the attempts, the final ``finish_reason``
    and ``attempts`` (``max_tokens``, tokens, dollars and ``finish_reason`` of each).
    A ChatError on the retry carries the first attempt's dollars too.
    """
    send = send or chat
    first_cap, retry_cap = answerer_caps(model, requested_max_tokens)
    attempts = []
    r = await send(model, system, user, first_cap)
    attempts.append(_attempt(first_cap, r))
    if (r.get("finish_reason") or "") == "length" and retry_cap is not None:
        try:
            r = await send(model, system, user, retry_cap)
        except ChatError as exc:
            raise ChatError(str(exc), dollars=attempts[0]["dollars"] + exc.dollars,
                            transport=getattr(exc, "transport", False)) from exc
        attempts.append(_attempt(retry_cap, r))
    return {
        "content": r.get("content") or "",
        "tokens_in": sum(a["tokens_in"] for a in attempts),
        "tokens_out": sum(a["tokens_out"] for a in attempts),
        "dollars": sum(a["dollars"] for a in attempts),
        "finish_reason": (r.get("finish_reason") or "").strip(),
        "attempts": attempts,
        "model": model.model_id,
    }


def _attempt(cap: int, r: dict) -> dict:
    return {
        "max_tokens": cap,
        "tokens_in": int(r.get("tokens_in") or 0),
        "tokens_out": int(r.get("tokens_out") or 0),
        "dollars": float(r.get("dollars") or 0.0),
        "finish_reason": (r.get("finish_reason") or "").strip(),
    }
