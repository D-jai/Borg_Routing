"""Work order 1.6: dollars per call, and the baseline model's price for the same tokens."""
import pytest

from src.db.schema import create_all
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.cost import (
    BASELINE_CONFIG_KEY,
    call_dollars,
    price_call,
    resolve_baseline_model,
    select_baseline_model,
)
from src.registry.models import ModelInfo, Pricing
from tests.e2e.stub_model_server import stub_model, three_tier_matrix


def _local() -> ModelInfo:
    return ModelInfo(model_id="ollama/llama", source="ollama", context_length=4096, checked=True)


def test_priced_call_is_the_exact_product():
    m = stub_model("top", 9.5, 5e-6, 2.5e-5)
    assert call_dollars(m, 1000, 200) == 1000 * 5e-6 + 200 * 2.5e-5


def test_local_model_costs_zero():
    assert call_dollars(_local(), 5000, 700) == 0.0


def test_unknown_model_costs_zero_without_crashing():
    assert call_dollars(None, 100, 100) == 0.0


def test_missing_token_counts_cost_zero():
    m = stub_model("top", 9.5, 5e-6, 2.5e-5)
    assert call_dollars(m, None, None) == 0.0


def test_baseline_is_checked_model_with_highest_reasoning_score():
    matrix = three_tier_matrix()
    matrix.upsert(stub_model("unchecked_genius", 10.0, 1e-5, 1e-5, checked=False))
    assert select_baseline_model(matrix).model_id == "openrouter/stub/top"


def test_configured_baseline_overrides():
    matrix = three_tier_matrix()
    chosen = select_baseline_model(matrix, configured_id="openrouter/stub/mid")
    assert chosen.model_id == "openrouter/stub/mid"


def test_configured_baseline_that_is_unknown_falls_back_to_best():
    matrix = three_tier_matrix()
    assert select_baseline_model(matrix, configured_id="nope/x").model_id == "openrouter/stub/top"


def test_empty_matrix_has_no_baseline():
    assert select_baseline_model(CapabilityMatrix()) is None


def test_price_call_gives_actual_and_baseline():
    matrix = three_tier_matrix()
    cheap = matrix.get("openrouter/stub/cheap")
    top = matrix.get("openrouter/stub/top")
    actual, baseline = price_call(cheap, top, 1000, 500)
    assert actual == 1000 * 1e-7 + 500 * 2e-7
    assert baseline == 1000 * 5e-6 + 500 * 2.5e-5


def test_price_call_without_baseline_model_leaves_baseline_unset():
    actual, baseline = price_call(_local(), None, 10, 10)
    assert actual == 0.0
    assert baseline is None


@pytest.mark.asyncio
async def test_baseline_config_key_beats_env(tmp_path, monkeypatch):
    from src.db.state import set_config

    db = str(tmp_path / "b.db")
    await create_all(db)
    matrix = three_tier_matrix()
    monkeypatch.setenv("BASELINE_MODEL_ID", "openrouter/stub/cheap")
    assert (await resolve_baseline_model(matrix, db)).model_id == "openrouter/stub/cheap"
    await set_config(BASELINE_CONFIG_KEY, "openrouter/stub/mid", db_path=db)
    assert (await resolve_baseline_model(matrix, db)).model_id == "openrouter/stub/mid"


def test_pricing_object_with_zero_rates_costs_zero():
    m = ModelInfo(model_id="x", source="custom_key", context_length=10,
                  pricing=Pricing(input_per_token=0.0, output_per_token=0.0))
    assert call_dollars(m, 10, 10) == 0.0


@pytest.mark.asyncio
async def test_price_with_baseline_survives_an_empty_matrix_and_unknown_model(tmp_path):
    from src.registry.cost import price_with_baseline

    db = str(tmp_path / "e.db")
    await create_all(db)
    dollars, baseline = await price_with_baseline(None, 10, 10, db, matrix=CapabilityMatrix())
    assert dollars == 0.0 and baseline is None
