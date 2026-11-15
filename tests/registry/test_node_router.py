"""Per-step routing: which model serves one plan node, and why."""
import pytest

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing
from src.registry.node_router import DEFAULT_FLOORS, select_model_for_node

TASK_TYPES = ("reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction")


def _model(name, score, price_in, price_out, checked=True, context_length=200_000, scores=None):
    return ModelInfo(
        model_id=f"openrouter/stub/{name}",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=context_length,
        pricing=Pricing(input_per_token=price_in, output_per_token=price_out),
        task_scores=scores or {t: score for t in TASK_TYPES},
        checked=checked,
    )


TOP = _model("top", 9.5, 5e-6, 2.5e-5)
MID = _model("mid", 8.0, 1e-6, 3e-6)
CHEAP = _model("cheap", 6.5, 1e-7, 2e-7)


def _matrix(*models) -> CapabilityMatrix:
    matrix = CapabilityMatrix()
    for m in models or (TOP, MID, CHEAP):
        matrix.upsert(m)
    return matrix


def _route(node, skill=None, matrix=None, default=TOP, floors=None):
    return select_model_for_node(
        node=node,
        skill=skill,
        matrix=matrix or _matrix(),
        floors=floors or DEFAULT_FLOORS,
        time_cost_per_sec=0.0001,
        default_model=default,
    )


def _node(**fields) -> dict:
    return {"type": "answerer", "id": "n1", "instruction": "Do the step.", **fields}


@pytest.mark.parametrize(
    "tier, expected",
    [("light", CHEAP), ("mid", MID), ("heavy", TOP)],
)
def test_tier_picks_the_cheapest_model_that_clears_its_floor(tier, expected):
    model, decision = _route(_node(task_type="extraction", tier=tier))
    assert model.model_id == expected.model_id
    assert decision["reason"] == "routed"
    assert decision["tier"] == tier
    assert decision["floor"] == DEFAULT_FLOORS[tier]
    assert decision["chosen"] == expected.model_id


def test_task_type_selects_on_that_types_score():
    coder = _model("coder", 0, 2e-7, 4e-7, scores={"coding": 9.2, "reasoning": 5.0})
    model, decision = _route(
        _node(task_type="coding", tier="heavy"), matrix=_matrix(TOP, MID, CHEAP, coder)
    )
    assert model.model_id == coder.model_id  # clears 9.0 on coding, cheaper than top
    assert decision["task_type"] == "coding"


def test_planner_named_model_wins_when_it_is_in_the_pool():
    model, decision = _route(
        _node(task_type="extraction", tier="light", recommended_model=TOP.model_id)
    )
    assert model.model_id == TOP.model_id
    assert decision["reason"] == "planner_pick"


def test_planner_named_model_without_provider_prefix_is_matched():
    model, decision = _route(_node(tier="light", recommended_model="stub/mid"))
    assert model.model_id == MID.model_id
    assert decision["reason"] == "planner_pick"


def test_planner_named_model_that_is_unchecked_is_ignored():
    unchecked_top = _model("top", 9.5, 5e-6, 2.5e-5, checked=False)
    model, decision = _route(
        _node(task_type="extraction", tier="light", recommended_model=unchecked_top.model_id),
        matrix=_matrix(unchecked_top, MID, CHEAP),
    )
    assert model.model_id == CHEAP.model_id
    assert decision["reason"] == "routed"
    assert any("not in the pool" in n for n in decision["notes"])


def test_planner_named_model_that_does_not_exist_is_ignored():
    model, decision = _route(_node(tier="light", recommended_model="nobody/home"))
    assert model.model_id == CHEAP.model_id
    assert decision["reason"] == "routed"


def test_skill_supplies_tier_and_kind_when_the_node_has_none():
    skill = {"recommended_tier": "light", "recommended_specialty": "extraction"}
    model, decision = _route(_node(skill_id="extract_fields"), skill=skill)
    assert model.model_id == CHEAP.model_id
    assert (decision["task_type"], decision["tier"]) == ("extraction", "light")


def test_node_labels_override_the_skills():
    skill = {"recommended_tier": "light", "recommended_specialty": "extraction"}
    model, decision = _route(_node(task_type="reasoning", tier="heavy"), skill=skill)
    assert model.model_id == TOP.model_id
    assert (decision["task_type"], decision["tier"]) == ("reasoning", "heavy")


def test_unlabelled_step_is_reasoning_at_mid():
    model, decision = _route(_node())
    assert model.model_id == MID.model_id
    assert (decision["task_type"], decision["tier"]) == ("reasoning", "mid")


def test_creative_means_writing():
    _model_, decision = _route(_node(task_type="creative", tier="light"))
    assert decision["task_type"] == "writing"


def test_unknown_kind_of_work_becomes_reasoning_and_is_noted():
    _model_, decision = _route(_node(task_type="alchemy", tier="light"))
    assert decision["task_type"] == "reasoning"
    assert any("alchemy" in n for n in decision["notes"])


def test_unknown_tier_becomes_mid_and_is_noted():
    model, decision = _route(_node(tier="enormous"))
    assert model.model_id == MID.model_id
    assert decision["tier"] == "mid"
    assert any("enormous" in n for n in decision["notes"])


def test_null_skill_specialty_is_treated_as_absent():
    skill = {"recommended_tier": "light", "recommended_specialty": None}
    _model_, decision = _route(_node(), skill=skill)
    assert (decision["task_type"], decision["tier"]) == ("reasoning", "light")


def test_no_model_clears_the_floor_falls_back_to_the_default_model():
    model, decision = _route(_node(tier="heavy"), matrix=_matrix(CHEAP), default=CHEAP)
    assert model.model_id == CHEAP.model_id
    assert decision["reason"] == "fallback_default"
    assert decision["chosen"] == CHEAP.model_id
    assert "BRT-REG-020" in decision["error"]


def test_candidates_are_recorded_cheapest_first_and_all_clear_the_floor():
    _model_, decision = _route(_node(task_type="extraction", tier="mid"))
    ids = [c["model_id"] for c in decision["candidates"]]
    assert ids == [MID.model_id, TOP.model_id]          # cheap (6.5) misses 7.5
    costs = [c["est_cost"] for c in decision["candidates"]]
    assert costs == sorted(costs)
    assert decision["candidates"][0]["score"] == 8.0
    assert decision["est_cost"] == costs[0]


def test_a_model_whose_context_is_too_small_is_not_offered():
    tiny = _model("tiny", 6.5, 1e-9, 1e-9, context_length=300)
    long_node = _node(tier="light", instruction="x" * 8000)   # about 2000 tokens
    model, decision = _route(long_node, matrix=_matrix(TOP, MID, CHEAP, tiny))
    assert model.model_id == CHEAP.model_id
    assert tiny.model_id not in [c["model_id"] for c in decision["candidates"]]
    assert decision["est_input_tokens"] >= 2000


def test_custom_floors_are_honoured():
    floors = {"light": 7.0, "mid": 7.5, "heavy": 9.0}
    model, decision = _route(_node(tier="light"), floors=floors)
    assert model.model_id == MID.model_id       # cheap (6.5) no longer clears light
    assert decision["floor"] == 7.0
