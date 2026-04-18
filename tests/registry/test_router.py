"""Tests for src/registry/router.py.

TC-REG-030 — coding task (SYSTEM_CHOOSE) selects qwen3-coder or deepseek
TC-REG-031 — LOCAL_FIRST keeps external models out when a local qualifies
TC-REG-032 — LOCAL_FIRST falls back to external when no local meets the bar
TC-REG-033 — specialty_hint prefers models with that tag
TC-REG-034 — BRT-REG-020 raised when no model meets quality requirement
TC-REG-035 — roleplay task returns a roleplay-specialist model
"""
import pytest

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.router import select_model

SEED_PATH = "data/capability_matrix.yaml"


@pytest.fixture(scope="module")
def matrix():
    cm = CapabilityMatrix(SEED_PATH)
    cm.load()
    return cm


def test_coding_system_choose(matrix):
    """TC-REG-030 — SYSTEM_CHOOSE picks the cheapest model meeting the coding bar."""
    from src.registry.cost import effective_cost

    model = select_model(matrix, task_type="coding", quality_requirement=8.0)
    assert model.task_scores.get("coding", 0) >= 8.0

    # Verify it is truly the cheapest qualifying candidate
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
    # quality_requirement=9.5 — no local model scores this high for reasoning
    model = select_model(
        matrix,
        task_type="reasoning",
        quality_requirement=9.5,
        strategy="LOCAL_FIRST",
    )
    # Only claude-sonnet-4 has reasoning=9.5; it's external
    assert model.source == "openrouter"
    assert "claude" in model.model_id.lower()


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
