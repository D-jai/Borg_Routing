"""Model router — specialty-primary model selection.

select_model() chooses the cheapest model that meets the quality bar and
context requirement for a given task. Tier (light/mid/heavy) is a derived
concept expressed through quality_requirement; specialty is expressed through
per-task task_scores in the capability matrix.

Only `checked == True` rows are considered (CapabilityMatrix.query_by_task
applies the filter by default).

Error codes:
- BRT-REG-020: no model meets the requirements
"""
from typing import Literal, Optional

from .capability_matrix import CapabilityMatrix
from .cost import route_rank, slowest_measured_tps
from .models import ModelInfo

_DEFAULT_TIME_COST_PER_SEC = 0.0001  # BRT-RT-01: local-dev default; see cost.py


def select_model(
    matrix: CapabilityMatrix,
    task_type: str,
    estimated_input_tokens: int = 1000,
    estimated_output_tokens: int = 500,
    quality_requirement: float = 6.0,
    strategy: Literal["LOCAL_FIRST", "SYSTEM_CHOOSE"] = "SYSTEM_CHOOSE",
    specialty_hint: Optional[str] = None,
    time_cost_per_sec: float = _DEFAULT_TIME_COST_PER_SEC,
    pool: Optional[list[ModelInfo]] = None,
) -> ModelInfo:
    """Return the cheapest checked model meeting the quality + context bar.

    Args:
        matrix: Loaded CapabilityMatrix instance.
        task_type: Task type string matching task_scores keys (e.g. "coding").
        estimated_input_tokens: Expected input token count for context filtering.
        estimated_output_tokens: Expected output token count for cost calc.
        quality_requirement: Minimum task_score threshold (0-10).
        strategy: LOCAL_FIRST tries local models first; SYSTEM_CHOOSE picks
            globally cheapest regardless of source.
        specialty_hint: Optional specialty_tag string; candidates with this tag
            are preferred (sorted to the front before cost sort).
        time_cost_per_sec: Passed through to effective_cost().
        pool: The models whose slowest speed figure an unmeasured model is
            priced at (ruling 28 b); default every row of ``matrix``. With no
            figure in the pool, an unmeasured model is ranked after every
            priced model (ruling 28h).

    Returns:
        The selected ModelInfo.

    Raises:
        ValueError: BRT-REG-020 if no candidate meets requirements.
    """
    candidates = matrix.query_by_task(
        task_type,
        min_score=quality_requirement,
        min_context_length=estimated_input_tokens,
    )

    if not candidates:
        raise ValueError(
            f"BRT-REG-020: no model found for task_type='{task_type}' "
            f"with quality_requirement={quality_requirement} and "
            f"min_context_length={estimated_input_tokens}. "
            "Possible causes: no models are currently checked on the Models page; "
            "the Get Info pass has not been run so the models have no scores yet; "
            "or the quality bar is too strict. Check models, lower the bar, or "
            "re-run Get Info."
        )

    # LOCAL_FIRST: restrict to local sources if any qualify
    if strategy == "LOCAL_FIRST":
        local_candidates = [
            m for m in candidates if m.source in ("lmstudio", "ollama")
        ]
        if local_candidates:
            candidates = local_candidates

    # specialty_hint: prefer models that list the hint in specialty_tags
    if specialty_hint:
        hinted = [m for m in candidates if specialty_hint in m.specialty_tags]
        if hinted:
            candidates = hinted

    # Sort by effective_cost ascending; cheapest first. An unmeasured model is
    # priced at the pool's slowest speed figure, or, with none, ranked after
    # the priced models (rulings 28 b, 28h). The sort is stable: equal keys
    # keep the query's order (higher score first).
    unmeasured_tps = slowest_measured_tps(pool if pool is not None else matrix.all())
    candidates.sort(
        key=lambda m: route_rank(
            m, estimated_input_tokens, estimated_output_tokens, time_cost_per_sec,
            unmeasured_tps=unmeasured_tps,
        )
    )

    return candidates[0]
