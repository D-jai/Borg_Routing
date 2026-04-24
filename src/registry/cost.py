"""Effective cost formula for model selection.

Unified formula works for local, external, and mixed setups:
  effective_cost = dollar_cost + time_seconds * time_cost_per_sec

- External models: dollar_cost dominates (rates from OpenRouter pricing).
- Local models: dollar_cost = 0; time_cost dominates (tokens/sec is inverse cost).

Error codes:
- BRT-REG-013: zero or negative throughput_tps on a local model
"""
from .models import ModelInfo

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
