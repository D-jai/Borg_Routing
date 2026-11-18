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

import asyncio
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


# Process memory (ruling 28 4a): a model whose probe failed is not probed again by
# run starts until Discover or a tick re-measures it, or the process restarts.
_failed: dict[str, str] = {}
# Single flight (ruling 28, round 2): at most one probe per model id at a time in
# this process. The task that owns a row's probe is kept here until it ends; a
# second trigger awaits it instead of probing. A row is "measuring" while it is here.
_inflight: dict[str, asyncio.Task] = {}
_tasks: set = set()  # strong references to background waiters


def failure_of(model_id: str) -> Optional[str]:
    """Why this model's last probe failed in this process, or None."""
    return _failed.get(model_id)


def speed_status(model_id: str) -> Optional[str]:
    """``"measuring"`` while a probe of this row runs, ``"BRT-REG-016"`` after a
    failed one, else None."""
    if model_id in _inflight:
        return "measuring"
    if model_id in _failed:
        return "BRT-REG-016"
    return None


def reset_state() -> None:
    """Forget failures and running probes (tests)."""
    _failed.clear()
    _inflight.clear()


def _figure(tps: float) -> float:
    """Four significant figures: a slow host's 0.004 tok/s never rounds to 0."""
    return float(f"{tps:.4g}")


async def _own_probe(
    matrix: CapabilityMatrix, model_id: str, post: Optional[PostJson], clock: Clock
) -> Optional[float]:
    """The one probe of a row: every HTTP call of it sits under one deadline of
    ``PROBE_TIMEOUT_S`` (ruling 28, round 2). Stores the figure, or records the
    failure, unless a figure arrived meanwhile: a failure never overwrites a
    stored figure and never marks a row that has one as failed."""
    model = matrix.get(model_id)
    try:
        tps = await asyncio.wait_for(probe_tps(model, post=post, clock=clock), timeout=PROBE_TIMEOUT_S)
        if tps <= 0:
            raise ValueError(f"figure {tps} is not positive")
    except Exception as exc:  # noqa: BLE001 - a failed probe never stops the caller
        current = matrix.get(model_id)
        if current is not None and current.throughput_tps is not None:
            return current.throughput_tps
        if isinstance(exc, asyncio.TimeoutError):
            reason = f"TimeoutError: no figure within {PROBE_TIMEOUT_S:g} s"
        else:
            reason = f"{type(exc).__name__}: {exc}"
        _failed[model_id] = reason
        logger.warning(
            "BRT-REG-016: could not measure the speed of '%s' at %s: %s. "
            "It is priced as no faster than the slowest measured model.",
            model_id, model.endpoint, reason,
        )
        return None
    _failed.pop(model_id, None)
    figure = _figure(tps)
    matrix.update_throughput(model_id, figure)
    logger.info("Measured %s at %s tok/s", model_id, figure)
    return figure


def _probe_task(
    matrix: CapabilityMatrix, model_id: str, post: Optional[PostJson] = None, clock: Clock = time.perf_counter
) -> asyncio.Task:
    """The row's in-flight probe task, started if there is none. Only the
    owning task removes the row from ``_inflight``, when it ends."""
    task = _inflight.get(model_id)
    if task is not None and not task.done():
        return task
    task = asyncio.get_running_loop().create_task(_own_probe(matrix, model_id, post, clock))
    _inflight[model_id] = task

    def _release(done: asyncio.Task, model_id: str = model_id) -> None:
        if _inflight.get(model_id) is done:
            del _inflight[model_id]

    task.add_done_callback(_release)
    return task


async def measure_throughput(
    matrix: CapabilityMatrix,
    model_id: str,
    post: Optional[PostJson] = None,
    clock: Clock = time.perf_counter,
    skip_failed: bool = False,
) -> Optional[float]:
    """Probe one row and store its figure with ``update_throughput``.

    When a probe of the row is already running (a tick, Discover or a run start
    began it), this awaits that probe's result and makes no HTTP call of its own.
    Returns the figure, or None when the row needs no probe (not local, priced,
    no endpoint, already measured), when ``skip_failed`` and its probe already
    failed in this process, or when the probe failed (BRT-REG-016, logged and
    remembered). Never raises. The caller saves the matrix.
    """
    model = matrix.get(model_id)
    if not needs_measuring(model):
        return None
    running = _inflight.get(model_id)
    if running is None or running.done():
        if skip_failed and model_id in _failed:
            return None
        running = _probe_task(matrix, model_id, post, clock)
    try:
        return await asyncio.shield(running)
    except Exception:  # noqa: BLE001 - e.g. the owner was cancelled
        return None


def to_measure(matrix: CapabilityMatrix, checked_only: bool = True, skip_failed: bool = False) -> list[str]:
    """The ids ``measure_unmeasured`` would probe or wait for (a row whose probe
    is running is always included, so a run start waits for its figure)."""
    return [
        row.model_id for row in matrix.all()
        if (row.checked or not checked_only) and needs_measuring(row)
        and (row.model_id in _inflight or not (skip_failed and row.model_id in _failed))
    ]


async def measure_unmeasured(
    matrix: CapabilityMatrix,
    checked_only: bool = True,
    post: Optional[PostJson] = None,
    clock: Clock = time.perf_counter,
    skip_failed: bool = False,
) -> dict[str, Optional[float]]:
    """Probe every local row with no figure (ticked rows only, unless
    ``checked_only`` is False; rows whose probe already failed are left out
    when ``skip_failed``, unless a probe of them is running). Returns
    ``{model_id: figure or None}``, one entry per row probed or waited for.
    Rows are probed one at a time (a local host often serves one call at a
    time). Never raises."""
    out: dict[str, Optional[float]] = {}
    for model_id in to_measure(matrix, checked_only, skip_failed):
        out[model_id] = await measure_throughput(matrix, model_id, post=post, clock=clock)
    return out


def measure_in_background(
    matrix: CapabilityMatrix,
    model_ids: list[str],
    on_done: Optional[Callable[[], None]] = None,
):
    """Start probing ``model_ids`` in the background and return at once
    (ruling 28 item 7: Discover and the tick do not wait). A row whose probe is
    already running is not probed again: this waits for that probe. Rows are
    probed one after another; ``on_done`` (save the matrix, share it with the
    engine) runs after all of them. Returns the waiting task, or None."""
    ids = [i for i in model_ids if needs_measuring(matrix.get(i))]
    if not ids:
        return None
    loop = asyncio.get_running_loop()
    # Register every row now, so it shows "measuring" from this moment; the
    # first row starts at once, later rows start when the one before ends.
    first = _probe_task(matrix, ids[0])

    async def _run() -> None:
        try:
            await asyncio.shield(first)
            for model_id in ids[1:]:
                await measure_throughput(matrix, model_id)
            if on_done is not None:
                on_done()
        except Exception as exc:  # noqa: BLE001
            logger.warning("BRT-REG-016: background speed probe failed: %s", exc)

    task = loop.create_task(_run())
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task
