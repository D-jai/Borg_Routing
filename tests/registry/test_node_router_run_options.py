"""R5: a chat alias (borg_route:light, :coding, :local ...) drives the router."""
import pytest

from src.registry.models import ModelInfo
from src.registry.node_router import DEFAULT_FLOORS, select_model_for_node
from tests.registry.test_node_router import CHEAP, MID, TOP, _matrix, _model, _node


def _route(node, run_options, matrix=None):
    return select_model_for_node(
        node=node, skill=None, matrix=matrix or _matrix(), floors=DEFAULT_FLOORS,
        time_cost_per_sec=0.0001, default_model=TOP, run_options=run_options,
    )


def test_tier_alias_sets_the_floor_for_every_step():
    # the planner asked for heavy; the operator chose light
    model, decision = _route(
        _node(task_type="extraction", tier="heavy"),
        {"alias": "borg_route:light", "tier": "light"},
    )
    assert model.model_id == CHEAP.model_id
    assert decision["tier"] == "light" and decision["floor"] == DEFAULT_FLOORS["light"]
    assert any("borg_route:light" in n for n in decision["notes"])
    assert decision["alias"] == "borg_route:light"


def test_heavy_alias_lifts_a_light_step():
    model, _d = _route(_node(task_type="extraction", tier="light"),
                       {"alias": "borg_route:heavy", "tier": "heavy"})
    assert model.model_id == TOP.model_id


def test_specialty_alias_overrides_the_steps_kind_of_work():
    coder = _model("coder", 0, 2e-7, 4e-7, scores={"coding": 9.2, "reasoning": 5.0})
    m = _matrix(TOP, MID, CHEAP, coder)
    opts = {"alias": "borg_route:coding", "task_type": "coding"}
    model, decision = _route(_node(tier="heavy"), opts, matrix=m)  # no task_type on the step
    assert decision["task_type"] == "coding" and model.model_id == coder.model_id
    assert any("borg_route:coding" in n for n in decision["notes"])
    # a step that states its own kind of work is overridden, like a tier is
    model2, decision2 = _route(_node(task_type="writing", tier="heavy"), opts, matrix=m)
    assert decision2["task_type"] == "coding" and model2.model_id == coder.model_id
    note = " ".join(decision2["notes"])
    assert "borg_route:coding" in note and "writing" in note  # names what it replaced
    # without the alias the step keeps its own kind of work
    _m3, decision3 = _route(_node(task_type="writing", tier="heavy"), None, matrix=m)
    assert decision3["task_type"] == "writing"


def test_creative_alias_reads_as_writing():
    _m, decision = _route(_node(tier="mid"), {"alias": "borg_route:creative", "task_type": "creative"})
    assert decision["task_type"] == "writing"


def test_local_alias_prefers_a_local_model_that_clears_the_floor():
    # Slow enough that its time cost makes it dearer than the cloud mid model,
    # so only the alias can make it win.
    local = ModelInfo(
        model_id="ollama/stub/local", source="ollama", endpoint="http://127.0.0.1:11434",
        context_length=200_000, pricing=None, throughput_tps=1.0,
        task_scores={t: 8.0 for t in ("reasoning", "coding", "extraction", "writing")},
        checked=True,
    )
    m = _matrix(TOP, MID, CHEAP, local)
    node = _node(task_type="reasoning", tier="mid")
    default_pick, _d = _route(node, None, matrix=m)
    assert default_pick.model_id == MID.model_id  # cloud wins on cost by default
    local_first, decision = _route(node, {"alias": "borg_route:local", "strategy": "LOCAL_FIRST"}, matrix=m)
    assert local_first.model_id == "ollama/stub/local"
    assert decision["strategy"] == "LOCAL_FIRST"
    assert any("borg_route:local" in n for n in decision["notes"])


def test_no_run_options_changes_nothing():
    a, da = _route(_node(task_type="extraction", tier="light"), None)
    assert a.model_id == CHEAP.model_id
    assert "alias" not in da
