"""Effective cost formula for model selection.

Unified formula works for local, external, and mixed setups:
  effective_cost = dollar_cost + time_seconds * time_cost_per_sec

- External models: dollar_cost dominates (rates from OpenRouter pricing).
- Local models: dollar_cost = 0; time_cost dominates (tokens/sec is inverse cost).
- A model with no price and no speed figure is never free (planner ruling 28):
  it is priced at the slowest speed figure in the pool, or, when no model in
  the pool has one, ranked after every priced model (ruling 28h). The figure
  is measured by src/registry/throughput.py.

Error codes:
- BRT-REG-013: zero or negative throughput_tps on a local model
"""
import os
from typing import Optional

from .models import ModelInfo

# Config key (and env fallback BASELINE_MODEL_ID) that pins the baseline model.
BASELINE_CONFIG_KEY = "baseline_model_id"


def call_dollars(
    model: Optional[ModelInfo],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
) -> float:
    """Dollars one call cost: tokens at the serving model's per-token prices.

    0.0 for a model with no pricing (local), an unknown model (None), or
    missing token counts. Never raises.
    """
    if model is None or model.pricing is None:
        return 0.0
    return (
        (tokens_in or 0) * model.pricing.input_per_token
        + (tokens_out or 0) * model.pricing.output_per_token
    )


# Ruling 22: the receipt's comparison figure is a floor. chars / 4 stands in for tokens, with no
# tokenizer and no calibration factor.
CHARS_PER_TOKEN_FLOOR = 4

# On round 1 (17-Nov-2026) the real direct cost was this many times the floor, lowest and highest
# of jobs A, C and B (2.2, 1.3, 4.4; B includes a discarded retry). The one place the figure is kept:
# GET /runs/{id} sends it with the digest, and the receipt and the user guide quote it.
DIRECT_FLOOR_RATIO_RANGE = (1.3, 4.4)


def direct_floor_dollars(
    input_chars: int,
    answer_chars: int,
    baseline: Optional[ModelInfo],
) -> Optional[float]:
    """The floor of one direct pass on the baseline model (ruling 22):
    ``input_chars`` (operator message plus every supplied document, each counted once) / 4 at the
    baseline's input price, plus ``answer_chars`` (the final answer as assembled) / 4 at its
    output price. None when there is no baseline or it has no price: unknown, never 0.
    """
    if baseline is None or baseline.pricing is None:
        return None
    return (
        (max(0, input_chars) / CHARS_PER_TOKEN_FLOOR) * baseline.pricing.input_per_token
        + (max(0, answer_chars) / CHARS_PER_TOKEN_FLOOR) * baseline.pricing.output_per_token
    )


def select_baseline_model(matrix, configured_id: Optional[str] = None) -> Optional[ModelInfo]:
    """The model a run is compared against: "what if the best model did every call".

    ``configured_id`` (when it names a row in the matrix) wins. Otherwise the
    checked model with the highest ``reasoning`` score; ties go to the
    lexicographically smaller id so the choice is stable. None if no checked
    row exists.
    """
    if configured_id:
        row = matrix.get(configured_id)
        if row is not None:
            return row
    checked = [m for m in matrix.all() if m.checked]
    if not checked:
        return None
    return min(
        checked,
        key=lambda m: (-m.task_scores.get("reasoning", 0.0), m.model_id),
    )


async def resolve_baseline_with_note(matrix, db_path: str) -> tuple[Optional[ModelInfo], Optional[str]]:
    """``(baseline, note)``: the baseline model and, when a configured id is
    not in the matrix, a note saying the highest-reasoning model was used
    instead. Config key ``baseline_model_id`` wins over env
    ``BASELINE_MODEL_ID``, which wins over the default.
    """
    from src.db.state import get_config  # noqa: PLC0415 - avoid import cycle

    configured = None
    try:
        configured = await get_config(BASELINE_CONFIG_KEY, db_path=db_path)
    except Exception:
        configured = None
    if not configured:
        configured = os.getenv("BASELINE_MODEL_ID") or None
    configured = str(configured) if configured else None
    baseline = select_baseline_model(matrix, configured)
    note = None
    if configured and (baseline is None or baseline.model_id != configured):
        note = (
            f"configured baseline '{configured}' is not in the matrix; "
            f"used {baseline.model_id if baseline else 'no model'} instead"
        )
    return baseline, note


async def resolve_baseline_model(matrix, db_path: str) -> Optional[ModelInfo]:
    """Baseline model using config key ``baseline_model_id``, then env
    ``BASELINE_MODEL_ID``, then the highest-reasoning checked model.

    Shared by cost recording (1.6) and the routing record (1.5).
    """
    baseline, _note = await resolve_baseline_with_note(matrix, db_path)
    return baseline


def price_call(
    model: Optional[ModelInfo],
    baseline: Optional[ModelInfo],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
) -> tuple[float, Optional[float]]:
    """(dollars, baseline_dollars) for one call. baseline_dollars is None when
    there is no baseline model to compare against."""
    dollars = call_dollars(model, tokens_in, tokens_out)
    baseline_dollars = None if baseline is None else call_dollars(baseline, tokens_in, tokens_out)
    return dollars, baseline_dollars


async def price_with_baseline(
    model: Optional[ModelInfo],
    tokens_in: Optional[int],
    tokens_out: Optional[int],
    db_path: str,
    matrix=None,
) -> tuple[float, Optional[float]]:
    """``price_call`` with the baseline looked up from the shared matrix.

    Never raises: any failure to find a baseline leaves baseline_dollars None
    and the call's own dollars still computed.
    """
    baseline = None
    try:
        if matrix is None:
            from src.orchestrator.engine import _get_matrix  # noqa: PLC0415

            matrix = _get_matrix()
        baseline = await resolve_baseline_model(matrix, db_path)
    except Exception:
        baseline = None
    return price_call(model, baseline, tokens_in, tokens_out)


# Primary: single developer running locally (electricity + modest respect for
# wait time). Override: TIME_COST_PER_SEC env var, or Admin UI → time-cost
# field, which persists to the `time_cost_per_sec` config key in DB and wins
# over this default at orchestrate_run() time. For a production SaaS
# deployment 0.0083 (~$30/hr) is more appropriate — set it via Admin UI.
_DEFAULT_TIME_COST_PER_SEC = 0.0001

# Planner ruling 28h: the route record's words when an unmeasured model was put
# behind every priced one (no model in the pool has a speed figure).
UNMEASURED_RANKED_AFTER_PRICED = "speed not measured: ranked after priced models"


def speed_unmeasured(model: ModelInfo) -> bool:
    """A model with no price and no speed figure (a local model not measured
    yet, or whose probe failed). Ruling 28: never priced as free.

    A priced model with no speed figure is not covered: its waiting still
    counts as zero (later work, ruling 28h)."""
    return model.throughput_tps is None and model.pricing is None


def slowest_measured_tps(models) -> Optional[float]:
    """The slowest speed figure among the checked ``models`` (the pool), or
    None when no checked model has one. Ruling 28 (b): an unmeasured model is
    priced as no faster than this."""
    figures = [
        m.throughput_tps for m in models
        if m.checked and m.throughput_tps is not None and m.throughput_tps > 0
    ]
    return min(figures) if figures else None


def time_cost(
    model: ModelInfo,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    time_cost_per_sec: float = _DEFAULT_TIME_COST_PER_SEC,
    unmeasured_tps: Optional[float] = None,
) -> float:
    """The time part of ``effective_cost``: tokens / speed x time cost per second.

    ``unmeasured_tps`` is the speed an unmeasured model is priced at (the
    pool's slowest figure, ruling 28 b). With it None, an unmeasured model's
    time part is 0.0 here, and the router ranks it after every priced model
    instead (``route_rank``, ruling 28h).

    Raises:
        ValueError: BRT-REG-013 when the model's own throughput_tps is 0 or less.
    """
    tps = model.throughput_tps
    if tps is not None and tps <= 0:
        raise ValueError(
            f"BRT-REG-013: model '{model.model_id}' has invalid throughput_tps={tps}. "
            "throughput_tps must be positive. Re-run benchmark to correct."
        )
    if tps is None:
        if model.pricing is None and unmeasured_tps is not None and unmeasured_tps > 0:
            tps = unmeasured_tps
        else:
            # A priced model with no figure (waiting counts as zero, later work),
            # or an unmeasured one in a pool with no figure (ranked by route_rank).
            return 0.0
    total_tokens = estimated_input_tokens + estimated_output_tokens
    return (total_tokens / tps) * time_cost_per_sec


def ranked_after_priced(model: ModelInfo, unmeasured_tps: Optional[float]) -> bool:
    """True when ruling 28h puts ``model`` behind every priced model: it has no
    speed figure and no price, and no model in the pool has a figure either."""
    return speed_unmeasured(model) and unmeasured_tps is None


def route_rank(
    model: ModelInfo,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    time_cost_per_sec: float = _DEFAULT_TIME_COST_PER_SEC,
    unmeasured_tps: Optional[float] = None,
) -> tuple[bool, float]:
    """The router's sort key: ``(ranked after priced models, effective cost)``.

    Python's sort is stable, so models with equal keys keep the order the
    matrix query gave them (higher score first): the existing tie-break.
    """
    return (
        ranked_after_priced(model, unmeasured_tps),
        effective_cost(
            model, estimated_input_tokens, estimated_output_tokens,
            time_cost_per_sec, unmeasured_tps=unmeasured_tps,
        ),
    )


def effective_cost(
    model: ModelInfo,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    time_cost_per_sec: float = _DEFAULT_TIME_COST_PER_SEC,
    unmeasured_tps: Optional[float] = None,
) -> float:
    """Return the unified effective cost in USD for one model call.

    Args:
        model: The model being evaluated.
        estimated_input_tokens: Expected number of input tokens.
        estimated_output_tokens: Expected number of output tokens.
        time_cost_per_sec: How much a second of waiting costs (configurable
            via Admin UI; default $0.0001/sec, about $0.36/hr).
        unmeasured_tps: The speed a model with no figure and no price is
            priced at (the pool's slowest figure, ruling 28 b). None leaves
            its time part at 0.0; the router then ranks it after priced
            models (``route_rank``).

    Returns:
        Effective cost in USD (float). Lower is cheaper.

    Raises:
        ValueError: BRT-REG-013 if model has a non-positive throughput_tps
            (bad throughput measurement).
    """
    # Dollar cost (external models; zero for local)
    if model.pricing is not None:
        dollar_cost = (
            estimated_input_tokens * model.pricing.input_per_token
            + estimated_output_tokens * model.pricing.output_per_token
        )
    else:
        dollar_cost = 0.0

    # Time cost (applies to all models; dominates for local)
    return dollar_cost + time_cost(
        model, estimated_input_tokens, estimated_output_tokens,
        time_cost_per_sec, unmeasured_tps=unmeasured_tps,
    )
