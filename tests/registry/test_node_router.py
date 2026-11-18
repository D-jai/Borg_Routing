"""Per-step routing: which model serves one plan node, and why."""
import pytest

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing
from src.registry.node_router import (
    _TASK_TYPE_ALIASES,
    DEFAULT_FLOORS,
    fallback_decision,
    select_model_for_node,
)

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
    assert any("creative" in n and "writing" in n for n in decision["notes"])


@pytest.mark.parametrize("word, mapped", sorted(_TASK_TYPE_ALIASES.items()))
def test_every_alias_mapping_is_written_into_the_notes(word, mapped):
    _model_, decision = _route(_node(task_type=word, tier="light"))
    assert decision["task_type"] == mapped
    assert any(f"'{word}'" in n and mapped in n for n in decision["notes"]), decision["notes"]


def test_a_task_type_word_needs_no_note():
    _model_, decision = _route(_node(task_type="coding", tier="light"))
    assert decision["notes"] == []


def test_the_fallback_decision_carries_every_field():
    d = fallback_decision(
        node=_node(task_type="extraction", tier="light"),
        skill=None,
        floors=DEFAULT_FLOORS,
        default_model=TOP,
        error="routing failed: boom",
    )
    assert d["reason"] == "fallback_default"
    assert (d["task_type"], d["tier"], d["floor"]) == ("extraction", "light", 6.0)
    assert d["chosen"] == TOP.model_id
    assert d["est_cost"] is None
    assert d["candidates"] == []
    assert d["error"] == "routing failed: boom"


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


# --- work order 1.5 review fixes ---------------------------------------------

def _local(name, score, tps=50.0):
    return ModelInfo(
        model_id=f"ollama/{name}",
        source="ollama",
        endpoint="http://127.0.0.1:11434",
        context_length=32_000,
        pricing=None,
        throughput_tps=tps,
        task_scores={t: score for t in TASK_TYPES},
        checked=True,
    )


def test_est_dollars_is_dollars_only_and_est_cost_adds_time():
    model, d = _route(_node(task_type="coding", tier="mid"))
    expected = d["est_input_tokens"] * 1e-6 + d["est_output_tokens"] * 3e-6
    assert d["est_dollars"] == pytest.approx(expected)
    assert d["est_cost"] >= d["est_dollars"]
    for cand in d["candidates"]:
        assert "est_dollars" in cand


def test_a_local_chosen_model_has_zero_est_dollars_but_baseline_stays_positive():
    from src.registry.cost import call_dollars

    local = _local("llama", 9.8)
    matrix = _matrix(TOP, local)
    model, d = _route(_node(task_type="coding", tier="heavy"), matrix=matrix)
    assert model.model_id == local.model_id
    assert d["est_dollars"] == 0.0
    assert d["est_cost"] > 0.0  # time cost is in est_cost, not est_dollars
    baseline = call_dollars(TOP, d["est_input_tokens"], d["est_output_tokens"])
    assert baseline > 0


def test_candidate_est_dollars_matches_the_baseline_estimate_when_it_is_a_candidate():
    from src.registry.cost import call_dollars

    _model_, d = _route(_node(task_type="coding", tier="light"))
    top = next(c for c in d["candidates"] if c["model_id"] == TOP.model_id)
    assert top["est_dollars"] == pytest.approx(
        call_dollars(TOP, d["est_input_tokens"], d["est_output_tokens"])
    )


def test_fallback_decision_carries_the_token_estimate_and_est_dollars():
    d = fallback_decision(
        node=_node(task_type="extraction", tier="light", instruction="x" * 4000),
        skill=None, floors=DEFAULT_FLOORS, default_model=TOP, error="boom",
    )
    assert d["est_input_tokens"] == 1000
    assert d["est_output_tokens"] == 500
    assert d["est_dollars"] == pytest.approx(1000 * 5e-6 + 500 * 2.5e-5)
    assert d["candidates_reason"]


def test_fallback_decision_est_is_null_when_no_estimate_can_be_made():
    class Bad(dict):
        def get(self, *a, **k):
            raise RuntimeError("cannot read node")

    d = fallback_decision(
        node=Bad(), skill=None, floors=DEFAULT_FLOORS, default_model=TOP, error="boom",
    )
    assert d["est_input_tokens"] is None
    assert d["est_dollars"] is None  # null, never 0.0


def test_planner_pick_lists_candidates_for_information_and_keeps_the_pick():
    model, d = _route(_node(task_type="coding", tier="light", recommended_model=TOP.model_id))
    assert d["reason"] == "planner_pick"
    assert model.model_id == TOP.model_id
    assert len(d["candidates"]) >= 2
    costs = [c["est_cost"] for c in d["candidates"]]
    assert costs == sorted(costs)
    assert d["est_dollars"] > 0


def test_router_miss_fallback_says_why_there_are_no_candidates():
    model, d = _route(_node(task_type="coding", tier="heavy"), matrix=_matrix(CHEAP))
    assert d["reason"] == "fallback_default"
    assert d["candidates"] == []
    assert "floor" in d["candidates_reason"]
    assert d["est_dollars"] is not None


def test_candidates_are_capped_at_eight_and_include_the_chosen_model():
    models = [_model(f"m{i:02d}", 8.5, 1e-6 * (i + 1), 2e-6 * (i + 1)) for i in range(11)]
    chosen, decision = _route(_node(task_type="reasoning", tier="mid"), matrix=_matrix(*models))
    ids = [c["model_id"] for c in decision["candidates"]]
    assert len(ids) == 8
    assert chosen.model_id in ids
    assert ids[0] == chosen.model_id            # cheapest first
    costs = [c["est_cost"] for c in decision["candidates"]]
    assert costs == sorted(costs)
