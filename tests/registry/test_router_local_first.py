"""Slice BRT-RT-01 router tests — time-cost knob + lowered quality bar.

TC-RT-006 — select_model with time_cost_per_sec=0 → LM Studio beats OpenRouter
            at equal quality.
TC-RT-007 — select_model with time_cost_per_sec=0.01 → fast OpenRouter beats
            slower LM Studio.
TC-RT-008 — select_model with quality_requirement=6.5 → 6.8-score LM Studio row
            is eligible (the Phase-1 unblock for mid-tier local orchestrators).
"""
import pytest

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing
from src.registry.router import select_model


def _build_matrix() -> CapabilityMatrix:
    cm = CapabilityMatrix()
    # Fast-but-external row (costs money, near-instant).
    cm.upsert(
        ModelInfo(
            model_id="openrouter/vendor/fast",
            source="openrouter",
            endpoint="https://openrouter.ai/api",
            context_length=128000,
            pricing=Pricing(input_per_token=5e-6, output_per_token=5e-6),
            throughput_tps=200.0,
            task_scores={"reasoning": 7.0},
            checked=True,
        )
    )
    # Slow-but-free row (local hardware).
    cm.upsert(
        ModelInfo(
            model_id="lmstudio/test/slow-local",
            source="lmstudio",
            endpoint="http://localhost:1234",
            context_length=32000,
            throughput_tps=20.0,
            task_scores={"reasoning": 7.0},
            checked=True,
        )
    )
    # Mid-tier local at 6.8 reasoning — eligible only at quality_requirement≤6.8.
    cm.upsert(
        ModelInfo(
            model_id="lmstudio/test/gemma-4-26b",
            source="lmstudio",
            endpoint="http://localhost:1234",
            context_length=128000,
            throughput_tps=35.0,
            task_scores={"reasoning": 6.8},
            checked=True,
        )
    )
    return cm


def test_time_cost_zero_favours_local():
    """TC-RT-006 — time_cost_per_sec=0 → LM Studio wins (dollar cost > 0 for OR)."""
    matrix = _build_matrix()
    picked = select_model(
        matrix=matrix,
        task_type="reasoning",
        quality_requirement=7.0,
        time_cost_per_sec=0.0,
    )
    assert picked.source == "lmstudio"


def test_time_cost_high_favours_fast_external():
    """TC-RT-007 — high time_cost_per_sec → fast OpenRouter wins vs slow local."""
    matrix = _build_matrix()
    picked = select_model(
        matrix=matrix,
        task_type="reasoning",
        quality_requirement=7.0,
        time_cost_per_sec=0.01,
    )
    assert picked.source == "openrouter"


def test_quality_bar_6_5_includes_mid_local():
    """TC-RT-008 — quality_requirement=6.5 admits the 6.8-score local row.

    Uses a minimal matrix with only the 6.8 row so the selection is
    unambiguous: raising the bar to 7.0 would leave the router with
    no candidate (BRT-REG-020).
    """
    from src.registry.capability_matrix import CapabilityMatrix

    cm = CapabilityMatrix()
    cm.upsert(
        ModelInfo(
            model_id="lmstudio/test/gemma-4-26b",
            source="lmstudio",
            endpoint="http://localhost:1234",
            context_length=128000,
            throughput_tps=35.0,
            task_scores={"reasoning": 6.8},
            checked=True,
        )
    )
    picked = select_model(
        matrix=cm,
        task_type="reasoning",
        quality_requirement=6.5,
        time_cost_per_sec=0.0,
    )
    assert picked.model_id == "lmstudio/test/gemma-4-26b"

    # Sanity guard: at 7.0 the 6.8 row is (correctly) excluded.
    with pytest.raises(ValueError) as exc:
        select_model(
            matrix=cm,
            task_type="reasoning",
            quality_requirement=7.0,
            time_cost_per_sec=0.0,
        )
    assert "BRT-REG-020" in str(exc.value)



