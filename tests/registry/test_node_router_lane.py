"""TC-LANE-010..: routing a step that must stay local (node_router local_filter).

Design note section 6.1: local models only, a cloud ``recommended_model`` is
ignored, and there is no fallback to the default model (BRT-LANE-002).
"""
import pytest

from src.lane.errors import LaneError
from src.registry.locality import is_local_model
from src.registry.node_router import DEFAULT_FLOORS, select_model_for_node
from tests.e2e.stub_model_server import lane_matrix, three_tier_matrix

NODE = {"instruction": "Extract the client name.", "task_type": "extraction", "tier": "light"}


def _route(matrix, node=NODE, **kwargs):
    default = matrix.get("openrouter/stub/top")
    return select_model_for_node(
        node=node, skill=None, matrix=matrix, floors=dict(DEFAULT_FLOORS),
        time_cost_per_sec=0.0001, default_model=default, **kwargs,
    )


# Happy path ---------------------------------------------------------------------

def test_a_local_only_step_gets_the_local_model():
    model, decision = _route(lane_matrix(), local_filter=is_local_model)
    assert model.model_id == "ollama/stub/local"
    assert decision["local_only"] is True
    assert [c["model_id"] for c in decision["candidates"]] == ["ollama/stub/local"]


def test_a_cloud_recommended_model_is_ignored_for_a_local_only_step():
    node = dict(NODE, recommended_model="openrouter/stub/top")
    model, decision = _route(lane_matrix(), node=node, local_filter=is_local_model)
    assert model.model_id == "ollama/stub/local"
    assert decision["reason"] == "routed"


def test_without_the_filter_routing_is_unchanged():
    model, decision = _route(lane_matrix(local_score=5.0))
    assert model.model_id == "openrouter/stub/cheap"
    assert "local_only" not in decision


def test_document_size_counts_in_the_token_estimate():
    _model, decision = _route(lane_matrix(), local_filter=is_local_model,
                              extra_input_tokens=3000)
    assert decision["est_input_tokens"] >= 3000


# Unhappy path -------------------------------------------------------------------

def test_no_local_model_raises_lane_002_not_a_fallback():
    with pytest.raises(LaneError) as info:
        _route(three_tier_matrix(), local_filter=is_local_model)
    assert info.value.code == "BRT-LANE-002"


def test_a_local_model_below_the_floor_raises_lane_002():
    with pytest.raises(LaneError) as info:
        _route(lane_matrix(local_score=5.0), local_filter=is_local_model)
    assert info.value.code == "BRT-LANE-002"


def test_a_document_too_large_for_every_local_model_raises_lane_002():
    with pytest.raises(LaneError):
        _route(lane_matrix(), local_filter=is_local_model, extra_input_tokens=500_000)
