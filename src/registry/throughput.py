"""Measure a local model's generation speed (planner ruling 28).

One short generation gives the model's output tokens per second, stored with
``CapabilityMatrix.update_throughput``. It runs when a local model is
discovered (Discover), ticked (the checkbox), or, still without a figure, at
the start of a run; the bench runner calls the same function at its start.

Load time stays out of the figure:
- Ollama (``/api/generate``, non-stream) reports ``eval_count`` and
  ``eval_duration`` (ns): the server's own generation rate is used.
- Any other local server (LM Studio, OpenAI-compatible): one warm-up call,
  then a second call is timed: ``usage.completion_tokens`` / seconds.

Only local rows with no price are probed (``src/registry/locality.py``
decides "local"); a cloud model is never called. Nothing here raises: a
failed probe is logged with BRT-REG-016 and the figure stays unset, so the
router prices the model as no faster than the slowest measured one in the
pool (``src/registry/cost.py``).

Error codes:
- BRT-REG-016: speed probe of a local model failed (logged, not raised)
"""
from __future__ import annotations

import logging
import time
from typing import Awaitable, Callable, Optional

import httpx

from .capability_matrix import CapabilityMatrix
from .locality import is_local_model
from .models import ModelInfo

logger = logging.getLogger(__name__)

PROBE_PROMPT = "Count from 1 to 40, numbers only, separated by spaces."
PROBE_MAX_TOKENS = 32
# A CPU host may load the model before it answers; the first call can be slow.
PROBE_TIMEOUT_S = 180.0

PostJson = Callable[[str, dict, float], Awaitable[dict]]
Clock = Callable[[], float]


async def _post_json(url: str, body: dict, timeout: float) -> dict:
    """POST ``body`` as JSON and return the JSON reply. Tests replace this."""
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(url, json=body)
        resp.raise_for_status()
        return resp.json()


def needs_measuring(model: Optional[ModelInfo]) -> bool:
    """A local, unpriced row with an endpoint and no speed figure."""
    return (
        model is not None
        and model.throughput_tps is None
        and model.pricing is None
        and bool(model.endpoint)
        and is_local_model(model)
    )


def _server_model_name(model: ModelInfo) -> str:
    return model.model_id.split("/", 1)[-1]


async def _ollama_rate(model: ModelInfo, post: PostJson) -> Optional[float]:
    """The server's own generation rate, or None when the reply has no eval figures."""
    reply = await post(
        f"{model.endpoint.rstrip('/')}/api/generate",
        {
            "model": _server_model_name(model),
            "prompt": PROBE_PROMPT,
            "stream": False,
            "options": {"num_predict": PROBE_MAX_TOKENS},
        },
        PROBE_TIMEOUT_S,
    )
    count = reply.get("eval_count")
    duration_ns = reply.get("eval_duration")
    if not count or not duration_ns or count <= 0 or duration_ns <= 0:
        return None
    return float(count) / (float(duration_ns) / 1e9)


async def _timed_rate(model: ModelInfo, post: PostJson, clock: Clock) -> float:
    """Warm up once, then time a second call: completion tokens / seconds."""
    url = f"{model.endpoint.rstrip('/')}/v1/chat/completions"
    body = {
        "model": _server_model_name(model),
        "messages": [{"role": "user", "content": PROBE_PROMPT}],
        "max_tokens": PROBE_MAX_TOKENS,
        "stream": False,
    }
    await post(url, body, PROBE_TIMEOUT_S)  # warm-up: loads the model
    start = clock()
    reply = await post(url, body, PROBE_TIMEOUT_S)
    elapsed = clock() - start
    tokens = ((reply or {}).get("usage") or {}).get("completion_tokens")
    if not tokens or tokens <= 0 or elapsed <= 0:
        raise ValueError(f"no completion_tokens or no elapsed time (tokens={tokens}, seconds={elapsed:.3f})")
    return float(tokens) / elapsed


async def probe_tps(
    model: ModelInfo,
    post: Optional[PostJson] = None,
    clock: Clock = time.perf_counter,
) -> float:
    """Output tokens per second of one short generation on ``model``.

    Raises whatever the HTTP call raises, or ValueError when the reply gives
    no figure; ``measure_throughput`` turns that into BRT-REG-016.
    """
    post = post or _post_json
    if model.source == "ollama":
        rate = await _ollama_rate(model, post)
        if rate is not None:
            return rate
    return await _timed_rate(model, post, clock)


async def measure_throughput(
    matrix: CapabilityMatrix,
    model_id: str,
    post: Optional[PostJson] = None,
    clock: Clock = time.perf_counter,
) -> Optional[float]:
    """Probe one row and store its figure with ``update_throughput``.

    Returns the figure, or None when the row needs no probe (not local, priced,
    no endpoint, already measured) or the probe failed (BRT-REG-016, logged).
    Never raises. The caller saves the matrix.
    """
    model = matrix.get(model_id)
    if not needs_measuring(model):
        return None
    try:
        tps = await probe_tps(model, post=post, clock=clock)
        if tps <= 0:
            raise ValueError(f"figure {tps} is not positive")
    except Exception as exc:  # noqa: BLE001 - a failed probe never stops the caller
        logger.warning(
            "BRT-REG-016: could not measure the speed of '%s' at %s: %s. "
            "It is priced as no faster than the slowest measured model.",
            model_id, model.endpoint, f"{type(exc).__name__}: {exc}",
        )
        return None
    matrix.update_throughput(model_id, round(tps, 2))
    logger.info("Measured %s at %.2f tok/s", model_id, tps)
    return round(tps, 2)


async def measure_unmeasured(
    matrix: CapabilityMatrix,
    checked_only: bool = True,
    post: Optional[PostJson] = None,
    clock: Clock = time.perf_counter,
) -> dict[str, Optional[float]]:
    """Probe every local row with no figure (ticked rows only, unless
    ``checked_only`` is False). Returns ``{model_id: figure or None}``, one
    entry per row probed. Rows are probed one at a time (a local host often
    serves one call at a time). Never raises."""
    out: dict[str, Optional[float]] = {}
    for row in matrix.all():
        if checked_only and not row.checked:
            continue
        if not needs_measuring(row):
            continue
        out[row.model_id] = await measure_throughput(matrix, row.model_id, post=post, clock=clock)
    return out
