"""Effective cost formula for model selection.

Unified formula works for local, external, and mixed setups:
  effective_cost = dollar_cost + time_seconds * time_cost_per_sec

- External models: dollar_cost dominates (rates from OpenRouter pricing).
- Local models: dollar_cost = 0; time_cost dominates (tokens/sec is inverse cost).

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


def effective_cost(
    model: ModelInfo,
    estimated_input_tokens: int,
    estimated_output_tokens: int,
    time_cost_per_sec: float = _DEFAULT_TIME_COST_PER_SEC,
) -> float:
    """Return the unified effective cost in USD for one model call.

    Args:
        model: The model being evaluated.
        estimated_input_tokens: Expected number of input tokens.
        estimated_output_tokens: Expected number of output tokens.
        time_cost_per_sec: How much a second of waiting costs (configurable
            via Admin UI; default $30/hr = $0.0083/sec).

    Returns:
        Effective cost in USD (float). Lower is cheaper.

    Raises:
        ValueError: BRT-REG-013 if model has a non-positive throughput_tps
            and no pricing (local model with bad throughput measurement).
    """
    total_tokens = estimated_input_tokens + estimated_output_tokens

    # Dollar cost (external models; zero for local)
    if model.pricing is not None:
        dollar_cost = (
            estimated_input_tokens * model.pricing.input_per_token
            + estimated_output_tokens * model.pricing.output_per_token
        )
    else:
        dollar_cost = 0.0

    # Time cost (applies to all models; dominates for local)
    tps = model.throughput_tps
    if tps is None:
        # No throughput measured yet — assume infinite speed (zero time cost).
        # This keeps undiscovered local models eligible; they'll be benchmarked later.
        time_cost = 0.0
    elif tps <= 0:
        raise ValueError(
            f"BRT-REG-013: model '{model.model_id}' has invalid throughput_tps={tps}. "
            "throughput_tps must be positive. Re-run benchmark to correct."
        )
    else:
        time_seconds = total_tokens / tps
        time_cost = time_seconds * time_cost_per_sec

    return dollar_cost + time_cost
