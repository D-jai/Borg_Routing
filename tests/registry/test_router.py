"""Tests for src/registry/router.py.

The router only considers models whose `checked` flag is True, so the module
fixture flips every row to checked=True. A separate test asserts that the
router honours the filter when rows are unchecked.

TC-REG-030 — coding task (SYSTEM_CHOOSE) picks the cheapest qualifying model
TC-REG-031 — LOCAL_FIRST keeps external models out when a local qualifies
TC-REG-032 — LOCAL_FIRST falls back to external when no local meets the bar
TC-REG-033 — specialty_hint prefers models with that tag
TC-REG-034 — BRT-REG-020 raised when no model meets quality requirement
TC-REG-035 — roleplay task returns cheapest model meeting the roleplay bar
TC-REG-036 — router ignores unchecked rows (even when they would qualify)
"""
import pytest

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo
from src.registry.router import select_model

SEED_PATH = "data/capability_matrix.yaml"


# Since BRT-DS-01 removed the hand-seeded LM Studio rows (now discovery-driven),
# router tests install a small set of representative local rows in-fixture so
# the routing behaviours under test remain covered.
_LOCAL_TEST_ROWS = [
    ModelInfo(
        model_id="lmstudio/test/gemma-4-e4b-it",
        source="lmstudio",
        endpoint="http://localhost:1234",
        context_length=32000,
        throughput_tps=40.0,
        task_scores={
            "reasoning": 5.5, "coding": 5.5, "summarize": 6.5, "writing": 6.0,
            "roleplay": 5.5, "agentic": 5.5, "extraction": 6.5,
        },
        specialty_tags=["fast_light"],
        params_b=7.5,
        publisher="test",
    ),
    ModelInfo(
        model_id="lmstudio/test/gemma-4-26b-a4b",
        source="lmstudio",
        endpoint="http://localhost:1234",
        context_length=128000,
        throughput_tps=35.0,
        task_scores={
            "reasoning": 6.8, "coding": 6.8, "summarize": 7.5, "writing": 7.2,
            "roleplay": 6.5, "agentic": 6.8, "extraction": 7.2,
        },
        specialty_tags=["mid_local", "moe", "long_context_local"],
        params_b=26.0,
        publisher="test",
    ),
]


@pytest.fixture(scope="module")
def matrix():
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    for row in _LOCAL_TEST_ROWS:
        cm.upsert(row)
    # Router operates on the checked pool; fixtures mark everything checked.
    for m in cm.all():
        cm.set_checked(m.model_id, True)
    return cm


def test_coding_system_choose(matrix):
    """TC-REG-030 — SYSTEM_CHOOSE picks the cheapest model meeting the coding bar."""
    from src.registry.cost import effective_cost

    model = select_model(matrix, task_type="coding", quality_requirement=8.0)
    assert model.task_scores.get("coding", 0) >= 8.0

    candidates = [
        m for m in matrix.all()
        if m.task_scores.get("coding", 0) >= 8.0 and m.context_length >= 1000
    ]
    my_cost = effective_cost(model, 1000, 500)
    all_costs = [effective_cost(m, 1000, 500) for m in candidates]
    assert my_cost == min(all_costs)


def test_local_first_excludes_externals(matrix):
    """TC-REG-031 — LOCAL_FIRST picks a local model when one qualifies."""
    model = select_model(
        matrix,
        task_type="coding",
        quality_requirement=6.0,
        strategy="LOCAL_FIRST",
    )
    assert model.source in ("lmstudio", "ollama"), (
        f"Expected local source but got '{model.source}' for model '{model.model_id}'"
    )


def test_local_first_falls_back_to_external(matrix):
    """TC-REG-032 — LOCAL_FIRST falls back to external when quality bar eliminates all locals."""
    # No local scores >= 9.5 on reasoning; frontier OpenRouter does.
    model = select_model(
        matrix,
        task_type="reasoning",
        quality_requirement=9.5,
        strategy="LOCAL_FIRST",
    )
    assert model.source == "openrouter"


def test_specialty_hint_preferred(matrix):
    """TC-REG-033 — specialty_hint prefers models with the tag."""
    model = select_model(
        matrix,
        task_type="coding",
        quality_requirement=6.0,
        specialty_hint="coding_specialist",
    )
    assert "coding_specialist" in model.specialty_tags


def test_no_qualifying_model_raises_brt_reg_020(matrix):
    """TC-REG-034 — BRT-REG-020 raised when quality bar is impossibly high."""
    with pytest.raises(ValueError) as exc_info:
        select_model(matrix, task_type="coding", quality_requirement=99.0)
    assert "BRT-REG-020" in str(exc_info.value)


def test_roleplay_task_returns_specialist(matrix):
    """TC-REG-035 — roleplay task returns cheapest model meeting the roleplay bar."""
    from src.registry.cost import effective_cost

    model = select_model(
        matrix,
        task_type="roleplay",
        quality_requirement=8.0,
    )
    assert model.task_scores.get("roleplay", 0) >= 8.0

    candidates = [
        m for m in matrix.all()
        if m.task_scores.get("roleplay", 0) >= 8.0 and m.context_length >= 1000
    ]
    my_cost = effective_cost(model, 1000, 500)
    all_costs = [effective_cost(m, 1000, 500) for m in candidates]
    assert my_cost == min(all_costs)


def test_router_ignores_unchecked_rows():
    """TC-REG-036 — router raises BRT-REG-020 when no row is checked, even if rows qualify."""
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    # Intentionally do NOT set_checked — every row is False.
    with pytest.raises(ValueError) as exc_info:
        select_model(cm, task_type="coding", quality_requirement=6.0)
    assert "BRT-REG-020" in str(exc_info.value)
