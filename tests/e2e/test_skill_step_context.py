"""Skill steps reach the model with their data and with braces kept as data.

Skill render review (16-Nov-2026, BUILD.md section 8):

- compare_and_rank and verify_claim name their data in a ``state_query``
  that nothing executes, so the model saw no items and no evidence. The
  engine now writes the data into the step's context: inline items, or the
  output of the step the ``*_ref`` names, as a numbered list. With neither,
  the step fails with BRT-SKL-008 before any model call.
- The executor filled its stage prompt with one ``str.replace`` per slot, so
  a ``{output_schema}`` inside an item, or a ``{side_effects_list}`` inside a
  criterion, was replaced by a later slot. It now fills in one pass.

The real engine, executor and router run against the scripted stand-in.
"""
import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

RANK = "Evaluate each item against the given criteria"
VERIFY = "Assess whether this claim is supported"
PROPOSALS = [
    "Proposal ALPHA: cheapest, ships in 6 weeks.",
    "Proposal BRAVO: mid price, ships in 2 weeks.",
    "Proposal CHARLIE: premium, ships tomorrow.",
]
CRITERIA = [{"name": "price", "weight": 0.5, "description": "cost"},
            {"name": "speed", "weight": 0.5, "description": "delivery"}]
EVIDENCE = "EVIDENCE-42: the kettle boils a litre in 90 seconds."


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skill_context.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-skill-context")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _answer(req):
    if RANK in req.system:
        return {"rankings": [], "rationale": "r"}
    if VERIFY in req.system:
        return {"verdict": "supported", "confidence": 0.9,
                "cited_evidence_ids": ["1"], "explanation": "e"}
    if "Gather the evidence" in req.system:
        return {"result": EVIDENCE}
    return {"decision": "keep", "reason": "r"}


@pytest.mark.asyncio
async def test_compare_and_rank_inline_items_reach_the_model_context(env):
    db_path, mp = env
    node = {"type": "answerer", "id": "n1", "skill_id": "compare_and_rank",
            "skill_version": 1, "task_type": "reasoning", "tier": "mid",
            "skill_args": {"items_ref": PROPOSALS, "criteria": CRITERIA}}
    stub = StubModels(plan_nodes=[node], answer_for=_answer).install(mp)
    result = await run_pipeline("Rank these proposals.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    req = stub.step_request(RANK)
    for i, proposal in enumerate(PROPOSALS, start=1):
        assert f"{i}. {proposal}" in req.system, req.system


@pytest.mark.asyncio
async def test_verify_claim_evidence_ref_step_output_reaches_the_model_context(env):
    db_path, mp = env
    plan = [
        {"type": "answerer", "id": "n1", "instruction": "Gather the evidence.",
         "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}},
        {"type": "answerer", "id": "n2", "skill_id": "verify_claim", "skill_version": 1,
         "task_type": "reasoning", "tier": "mid",
         "skill_args": {"claim": "The kettle is fast.", "evidence_ref": "n1"}},
    ]
    stub = StubModels(plan_nodes=plan, answer_for=_answer).install(mp)
    result = await run_pipeline("Check the claim against the evidence.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    req = stub.step_request(VERIFY)
    assert EVIDENCE in req.system, req.system


@pytest.mark.asyncio
async def test_answerer_skill_with_no_data_fails_with_skl_008_and_no_call(env):
    db_path, mp = env
    node = {"type": "answerer", "id": "n1", "skill_id": "compare_and_rank",
            "skill_version": 1, "task_type": "reasoning", "tier": "mid",
            "skill_args": {"items_ref": "no_such_step", "criteria": CRITERIA}}
    stub = StubModels(plan_nodes=[node], answer_for=_answer).install(mp)
    await run_pipeline("Rank these proposals.", db_path=db_path)
    await drain_log_writes()
    assert not [r for r in stub.of_kind("answerer") if RANK in r.system]
    text = " ".join(r.system + r.user for r in stub.of_kind("re_plan"))
    assert "BRT-SKL-008" in text and "no_such_step" in text


@pytest.mark.asyncio
async def test_braces_in_an_item_or_criterion_stay_literal_in_the_stage_prompt(env):
    db_path, mp = env
    item = "Review mentions {output_schema} in its text."
    node = {"type": "apply_to_each", "id": "n1", "skill_id": "filter_by_criterion",
            "skill_version": 1, "task_type": "extraction", "tier": "light",
            "skill_args": {"items_ref": [item],
                           "criterion": "mentions {side_effects_list}",
                           "concurrency_limit": 2}}
    stub = StubModels(plan_nodes=[node], answer_for=_answer).install(mp)
    result = await run_pipeline("Keep reviews that mention it.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    req = stub.step_request("does it match this criterion")
    assert item in req.system
    assert "CRITERION: mentions {side_effects_list}" in req.system
