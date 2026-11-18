"""Planner rulings 28 and 28h: a local model's speed is measured, and an
unmeasured one is never free against a priced model.

- (b) with a speed figure somewhere in the pool, an unmeasured local model is
  priced as no faster than the slowest model that has one;
- (28h) with no figure anywhere in the pool, it is ranked after every priced
  model that qualifies, and the record says so in words;
- the lane (raw_local / all_local) and LOCAL_FIRST still choose local;
- BRT-REG-013 is unchanged.
"""
import pytest

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.cost import (
    UNMEASURED_RANKED_AFTER_PRICED,
    effective_cost,
    slowest_measured_tps,
)
from src.registry.models import ModelInfo, Pricing
from src.registry.node_router import DEFAULT_FLOORS, select_model_for_node

TASKS = ("reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction")


def _cloud(name, score, price_in, price_out, tps=None):
    return ModelInfo(
        model_id=f"openrouter/stub/{name}", source="openrouter",
        endpoint="https://openrouter.ai/api", context_length=200_000,
        pricing=Pricing(input_per_token=price_in, output_per_token=price_out),
        throughput_tps=tps, task_scores={t: score for t in TASKS}, checked=True,
    )


def _local(name, score, tps=None):
    return ModelInfo(
        model_id=f"ollama/{name}", source="ollama", endpoint="http://127.0.0.1:11434",
        context_length=32_000, pricing=None, throughput_tps=tps,
        task_scores={t: score for t in TASKS}, checked=True,
    )


def _matrix(*models):
    m = CapabilityMatrix(path="__test_unused__.yaml")
    for row in models:
        m.upsert(row)
    return m


def _route(matrix, tier="light", time_cost=0.0001, **kw):
    node = {"type": "answerer", "id": "n1", "instruction": "Do the step.",
            "task_type": "summarize", "tier": tier}
    default = matrix.all()[0]
    return select_model_for_node(
        node=node, skill=None, matrix=matrix, floors=DEFAULT_FLOORS,
        time_cost_per_sec=time_cost, default_model=default, **kw,
    )


CHEAP_FAST = _cloud("cheap", 7.0, 1e-7, 2e-7, tps=200.0)
SLOW_WEAK = _cloud("slowweak", 3.0, 1e-7, 2e-7, tps=10.0)  # below every floor; sets the slowest figure
UNMEASURED = _local("unmeasured", 8.0)


# --- the four tests R28 names -------------------------------------------------

def test_an_unmeasured_local_model_does_not_beat_a_priced_cloud_model_at_est_cost_0():
    # A figure exists in the pool: the unmeasured model is priced at the slowest one (10 tok/s).
    m = _matrix(CHEAP_FAST, SLOW_WEAK, UNMEASURED)
    model, d = _route(m)
    assert model.model_id == CHEAP_FAST.model_id
    local = next(c for c in d["candidates"] if c["model_id"] == UNMEASURED.model_id)
    assert local["est_cost"] > 0.0
    total = d["est_input_tokens"] + d["est_output_tokens"]
    assert local["est_cost"] == pytest.approx(total / 10.0 * 0.0001)
    # No figure anywhere in the pool (ruling 28h): still never ahead of the priced model.
    bare = _matrix(_cloud("cheap", 7.0, 1e-7, 2e-7), UNMEASURED)
    model2, _d2 = _route(bare)
    assert model2.model_id == "openrouter/stub/cheap"


def test_a_measured_slow_local_model_loses_a_light_step_at_the_default_time_value_and_wins_at_0():
    slow_local = _local("slow", 8.0, tps=2.0)
    m = _matrix(CHEAP_FAST, slow_local)
    lost, _d = _route(m, time_cost=0.0001)
    assert lost.model_id == CHEAP_FAST.model_id
    won, _d = _route(m, time_cost=0.0)
    assert won.model_id == slow_local.model_id


def test_a_raw_lane_step_still_goes_local():
    is_local = lambda row: row.source == "ollama"  # noqa: E731
    with_figure = _matrix(CHEAP_FAST, SLOW_WEAK, UNMEASURED)
    model, d = _route(with_figure, local_filter=is_local)
    assert model.model_id == UNMEASURED.model_id and d["local_only"] is True
    # and with no figure anywhere in the pool (ruling 28h)
    no_figure = _matrix(_cloud("cheap", 7.0, 1e-7, 2e-7), UNMEASURED)
    model2, _d2 = _route(no_figure, local_filter=is_local)
    assert model2.model_id == UNMEASURED.model_id


def test_brt_reg_013_is_unchanged():
    for bad in (0.0, -5.0):
        row = _local("bad", 8.0, tps=bad)
        with pytest.raises(ValueError, match="BRT-REG-013"):
            effective_cost(row, 500, 250)
        # a pool figure never hides a bad figure of the model's own
        with pytest.raises(ValueError, match="BRT-REG-013"):
            effective_cost(row, 500, 250, unmeasured_tps=40.0)


# --- ruling 28h -------------------------------------------------------------

def test_with_no_figure_in_the_pool_an_unmeasured_model_is_ranked_after_priced_models_and_the_record_says_so():
    m = _matrix(_cloud("dear", 9.5, 5e-6, 2.5e-5), UNMEASURED)
    model, d = _route(m, tier="mid")
    assert model.model_id == "openrouter/stub/dear"
    assert UNMEASURED_RANKED_AFTER_PRICED == "speed not measured: ranked after priced models"
    assert UNMEASURED_RANKED_AFTER_PRICED in d["notes"]
    ids = [c["model_id"] for c in d["candidates"]]
    assert ids.index("openrouter/stub/dear") < ids.index(UNMEASURED.model_id)


def test_with_no_priced_model_qualifying_unmeasured_local_models_keep_the_existing_tie_break():
    # Two unmeasured locals and a priced model below the floor: the existing order
    # (higher score first, then matrix order) decides, unchanged.
    low = _local("low", 7.0)
    high = _local("high", 9.0)
    twin = _local("twin", 9.0)
    below = _cloud("below", 4.0, 1e-7, 2e-7)
    m = _matrix(below, low, high, twin)
    model, d = _route(m, tier="light")
    assert model.model_id == high.model_id
    assert [c["model_id"] for c in d["candidates"]] == [high.model_id, twin.model_id, low.model_id]
    assert UNMEASURED_RANKED_AFTER_PRICED in d["notes"]


def test_local_first_still_picks_an_unmeasured_local_model_with_no_figure_in_the_pool():
    m = _matrix(_cloud("cheap", 7.0, 1e-7, 2e-7), UNMEASURED)
    model, _d = _route(m, run_options={"alias": "borg_route:local", "strategy": "LOCAL_FIRST"})
    assert model.model_id == UNMEASURED.model_id


# --- (b), (e) -----------------------------------------------------------------

def test_slowest_measured_tps_is_the_slowest_checked_figure_or_none():
    unchecked_slower = _cloud("unchecked", 7.0, 1e-7, 2e-7, tps=1.0).model_copy(update={"checked": False})
    assert slowest_measured_tps([CHEAP_FAST, SLOW_WEAK, UNMEASURED, unchecked_slower]) == 10.0
    assert slowest_measured_tps([UNMEASURED, _cloud("x", 7.0, 1e-7, 2e-7)]) is None


def test_the_route_record_shows_the_time_part_of_est_cost():
    m = _matrix(CHEAP_FAST, SLOW_WEAK, UNMEASURED)
    _model, d = _route(m)
    assert d["est_cost"] == pytest.approx(d["est_dollars"] + d["est_time_cost"])
    for c in d["candidates"]:
        assert c["est_cost"] == pytest.approx(c["est_dollars"] + c["est_time_cost"])
    assert any("slowest measured speed" in n and UNMEASURED.model_id in n for n in d["notes"])
