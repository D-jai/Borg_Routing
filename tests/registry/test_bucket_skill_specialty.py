"""Deej's ruling 12: classify_into_buckets is extraction work, read from the real data/skills.yaml.

The round-1 job A local case: a model at extraction 6.8 and reasoning 4.6 must clear
the light floor (6.0) on a classify_into_buckets step whose node names no task_type.
"""
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing
from src.registry.node_router import DEFAULT_FLOORS, select_model_for_node
from src.skills.library import SkillLibrary

TASK_TYPES = ("reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction")


def _skill() -> dict:
    lib = SkillLibrary()
    lib.load()
    return lib.get("classify_into_buckets")


def _small() -> ModelInfo:
    scores = {t: 4.6 for t in TASK_TYPES}
    scores["extraction"] = 6.8
    return ModelInfo(
        model_id="openrouter/stub/small",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=128_000,
        pricing=Pricing(input_per_token=1e-7, output_per_token=2e-7),
        task_scores=scores,
        checked=True,
    )


def _route(node: dict):
    matrix = CapabilityMatrix()
    small = _small()
    matrix.upsert(small)
    return select_model_for_node(
        node=node,
        skill=_skill(),
        matrix=matrix,
        floors=DEFAULT_FLOORS,
        time_cost_per_sec=0.0001,
        default_model=small,
    )


def _node(**fields) -> dict:
    return {"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets", **fields}


def test_the_bucket_skill_is_extraction_work():
    assert _skill()["recommended_specialty"] == "extraction"


def test_an_unlabelled_bucket_step_routes_as_extraction_and_clears_the_light_floor():
    model, decision = _route(_node())
    assert (decision["task_type"], decision["tier"]) == ("extraction", "light")
    assert decision["reason"] == "routed"
    assert model.model_id == "openrouter/stub/small"


def test_a_node_task_type_still_wins_over_the_skill():
    _model_, decision = _route(_node(task_type="reasoning"))
    assert decision["task_type"] == "reasoning"
    assert decision["reason"] == "fallback_default"  # 4.6 does not clear 6.0 on reasoning
