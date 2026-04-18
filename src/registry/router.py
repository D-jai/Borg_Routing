"""Model router — specialty-primary model selection.

select_model() chooses the cheapest model that meets the quality bar and
context requirement for a given task. Tier (light/mid/heavy) is a derived
concept expressed through quality_requirement; specialty is expressed through
per-task task_scores in the capability matrix.

Error codes:
- BRT-REG-020: no model meets the requirements
"""
from typing import Literal, Optional

from .capability_matrix import CapabilityMatrix
from .cost import effective_cost
from .models import ModelInfo

_DEFAULT_TIME_COST_PER_SEC = 0.0083


def select_model(
    matrix: CapabilityMatrix,
    task_type: str,
    estimated_input_tokens: int = 1000,
    estimated_output_tokens: int = 500,
    quality_requirement: float = 6.0,
    strategy: Literal["LOCAL_FIRST", "SYSTEM_CHOOSE"] = "SYSTEM_CHOOSE",
    specialty_hint: Optional[str] = None,
    time_cost_per_sec: float = _DEFAULT_TIME_COST_PER_SEC,
) -> ModelInfo:
    """Return the cheapest model meeting the quality + context bar.

    Args:
        matrix: Loaded CapabilityMatrix instance.
        task_type: Task type string matching task_scores keys (e.g. "coding").
        estimated_input_tokens: Expected input token count for context filtering.
        estimated_output_tokens: Expected output token count for cost calc.
        quality_requirement: Minimum task_score threshold (0–10).
        strategy: LOCAL_FIRST tries local models first; SYSTEM_CHOOSE picks
            globally cheapest regardless of source.
        specialty_hint: Optional specialty_tag string; candidates with this tag
            are preferred (sorted to the front before cost sort).
        time_cost_per_sec: Passed through to effective_cost().

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
            "Run discovery and benchmark to populate the capability matrix, "
            "or lower quality_requirement."
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

    # Sort by effective_cost ascending; cheapest first
    candidates.sort(
        key=lambda m: effective_cost(
            m, estimated_input_tokens, estimated_output_tokens, time_cost_per_sec
        )
    )

    return candidates[0]
