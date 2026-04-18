"""Tests for src/registry/models.py.

TC-REG-001 — roundtrip serialisation of ModelInfo
TC-REG-002 — BRT-REG-001 raised on invalid source
TC-REG-003 — BRT-REG-002 raised on negative pricing
TC-REG-004 — local model with no pricing or throughput is valid
TC-REG-005 — task_scores and specialty_tags default to empty collections
"""
import pytest
from pydantic import ValidationError

from src.registry.models import ModelInfo, Pricing


def _make_local_model(**kwargs) -> dict:
    return {
        "model_id": "lmstudio/test-model",
        "source": "lmstudio",
        "context_length": 8192,
        **kwargs,
    }


def _make_external_model(**kwargs) -> dict:
    return {
        "model_id": "openrouter/anthropic/claude-sonnet-4",
        "source": "openrouter",
        "context_length": 200000,
        "pricing": {"input_per_token": 0.000003, "output_per_token": 0.000015},
        **kwargs,
    }


def test_roundtrip_local_model():
    """TC-REG-001a — local model serialises and deserialises cleanly."""
    data = _make_local_model(throughput_tps=18.0, params_b=35.0)
    m = ModelInfo(**data)
    assert m.model_id == "lmstudio/test-model"
    assert m.source == "lmstudio"
    assert m.pricing is None
    assert m.throughput_tps == 18.0
    # roundtrip via model_dump
    m2 = ModelInfo(**m.model_dump())
    assert m2 == m


def test_roundtrip_external_model():
    """TC-REG-001b — external model with pricing serialises cleanly."""
    data = _make_external_model(throughput_tps=80.0)
    m = ModelInfo(**data)
    assert m.pricing is not None
    assert m.pricing.input_per_token == pytest.approx(0.000003)
    m2 = ModelInfo(**m.model_dump())
    assert m2 == m


def test_invalid_source_raises_brt_reg_001():
    """TC-REG-002 — BRT-REG-001 raised for unknown source."""
    with pytest.raises(ValidationError) as exc_info:
        ModelInfo(**_make_local_model(source="unknown_provider"))
    assert "BRT-REG-001" in str(exc_info.value)


def test_negative_pricing_raises_brt_reg_002():
    """TC-REG-003 — BRT-REG-002 raised for negative pricing."""
    with pytest.raises(ValidationError) as exc_info:
        ModelInfo(
            **_make_external_model(
                pricing={"input_per_token": -0.001, "output_per_token": 0.001}
            )
        )
    assert "BRT-REG-002" in str(exc_info.value)


def test_local_model_no_pricing_valid():
    """TC-REG-004 — local model with no pricing or throughput is valid."""
    m = ModelInfo(**_make_local_model())
    assert m.pricing is None
    assert m.throughput_tps is None


def test_defaults_empty_collections():
    """TC-REG-005 — task_scores and specialty_tags default to empty."""
    m = ModelInfo(**_make_local_model())
    assert m.task_scores == {}
    assert m.specialty_tags == []
