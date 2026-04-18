"""Tests for src/registry/cost.py.

TC-REG-020 — local model returns time-only cost (dollar_cost=0)
TC-REG-021 — external model returns dollar + time combined cost
TC-REG-022 — BRT-REG-013 raised on zero/negative throughput_tps
TC-REG-023 — local model with no throughput_tps returns 0.0 (not yet benchmarked)
TC-REG-024 — custom time_cost_per_sec is applied correctly
"""
import pytest

from src.registry.cost import effective_cost
from src.registry.models import ModelInfo, Pricing


def _local(throughput_tps=None):
    return ModelInfo(
        model_id="lmstudio/test-local",
        source="lmstudio",
        context_length=8192,
        throughput_tps=throughput_tps,
    )


def _external(input_rate=0.000003, output_rate=0.000015, throughput_tps=80.0):
    return ModelInfo(
        model_id="openrouter/test-ext",
        source="openrouter",
        context_length=200000,
        pricing=Pricing(input_per_token=input_rate, output_per_token=output_rate),
        throughput_tps=throughput_tps,
    )


def test_local_model_time_only_cost():
    """TC-REG-020 — local model cost is time-only; no dollar component."""
    model = _local(throughput_tps=20.0)
    cost = effective_cost(model, 1000, 500, time_cost_per_sec=0.01)
    expected_seconds = (1000 + 500) / 20.0
    expected = expected_seconds * 0.01
    assert cost == pytest.approx(expected)


def test_external_model_dollar_plus_time_cost():
    """TC-REG-021 — external model returns combined dollar + time cost."""
    model = _external(input_rate=0.000003, output_rate=0.000015, throughput_tps=80.0)
    cost = effective_cost(model, 1000, 500, time_cost_per_sec=0.0083)
    dollar = 1000 * 0.000003 + 500 * 0.000015
    time_cost = (1500 / 80.0) * 0.0083
    assert cost == pytest.approx(dollar + time_cost)


def test_zero_throughput_raises_brt_reg_013():
    """TC-REG-022 — BRT-REG-013 raised on zero throughput_tps."""
    model = _local(throughput_tps=0.0)
    with pytest.raises(ValueError) as exc_info:
        effective_cost(model, 500, 250)
    assert "BRT-REG-013" in str(exc_info.value)


def test_negative_throughput_raises_brt_reg_013():
    """TC-REG-022b — BRT-REG-013 raised on negative throughput_tps."""
    model = _local(throughput_tps=-5.0)
    with pytest.raises(ValueError) as exc_info:
        effective_cost(model, 500, 250)
    assert "BRT-REG-013" in str(exc_info.value)


def test_no_throughput_returns_dollar_cost_only():
    """TC-REG-023 — local model with throughput_tps=None returns 0.0 time cost."""
    model = _local(throughput_tps=None)
    cost = effective_cost(model, 1000, 500)
    assert cost == pytest.approx(0.0)


def test_custom_time_cost_per_sec():
    """TC-REG-024 — custom time_cost_per_sec is applied correctly."""
    model = _local(throughput_tps=10.0)
    cost_default = effective_cost(model, 100, 100, time_cost_per_sec=0.0083)
    cost_double = effective_cost(model, 100, 100, time_cost_per_sec=0.0166)
    assert cost_double == pytest.approx(cost_default * 2, rel=1e-6)
