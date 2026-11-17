"""A skill step reaches the model with every placeholder filled (Check 1 attempt 3).

The live re_plan node n4 (extract_fields, 12 inline reviews, fields with
enum_values) went out as "Fields to extract:\\n{fields_formatted}" and every
item call failed. Here the exact node shape runs on the stand-in: one call per
item, each carrying the field list. A skill step whose args cannot fill its
template fails with BRT-SKL-008 and makes no step model call.

The real engine, executor and router run against the scripted stand-in.
"""
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

EXTRACT = "Extract the following fields"

REVIEWS = [
    "1. Brewmaster kettle: boils fast and looks great, love it.",
    "2. Brewmaster kettle: the lid cracked after two weeks, very disappointed.",
    "3. Brewmaster kettle: does the job, nothing special.",
    "4. Brewmaster kettle: quiet, quick, and easy to clean.",
    "5. Lumo desk lamp: bright, adjustable, perfect for my desk.",
    "6. Lumo desk lamp: the switch broke in a month, would not buy again.",
    "7. Lumo desk lamp: fine for the price, average build.",
    "8. Lumo desk lamp: flickers at night, returned it.",
    "9. Trekpak backpack: comfortable on long hikes, great pockets.",
    "10. Trekpak backpack: zipper held up through a rough trip, very happy.",
    "11. Trekpak backpack: a bit heavy but sturdy, decent value.",
    "12. Trekpak backpack: straps frayed early, not impressed.",
]

# The live node n4, as the planner wrote it (re_plan, Check 1 attempt 3).
LIVE_NODE = {
    "type": "apply_to_each",
    "id": "n4",
    "skill_id": "extract_fields",
    "skill_version": 1,
    "skill_args": {
        "items_ref": "reviews",
        "fields": [
            {"name": "product", "type": "string",
             "description": "The product name from the review (e.g., 'Brewmaster kettle', "
                            "'Lumo desk lamp', 'Trekpak backpack')"},
            {"name": "sentiment", "type": "enum",
             "enum_values": ["positive", "negative", "neutral"],
             "description": "The sentiment expressed in the review"},
        ],
        "concurrency_limit": 12,
    },
    "items": REVIEWS,
    "output_schema": {"product": "string", "sentiment": "enum[positive, negative, neutral]"},
    "task_type": "extraction",
    "tier": "light",
    "recommended_model": None,
    "trigger_reason": "initial_plan",
}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skill_render.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-skill-render")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _answer(req):
    # Stand in for the live model: a prompt with a raw placeholder is refused.
    if "{fields_formatted}" in req.system:
        return {"error": "insufficient context: fields_formatted was not provided"}
    return {"product": "x", "sentiment": "positive"}


def _rows(db_path):
    with sqlite3.connect(db_path) as conn:
        return conn.execute(
            "SELECT status, inbound_payload FROM envelopes WHERE type='apply_to_each' "
            "ORDER BY rowid"
        ).fetchall()


@pytest.mark.asyncio
async def test_live_extract_fields_node_renders_complete_and_runs_once_per_item(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[LIVE_NODE], answer_for=_answer).install(mp)
    result = await run_pipeline("Extract product and sentiment from each review.",
                                db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    calls = [r for r in stub.of_kind("answerer") if EXTRACT in r.system]
    assert len(calls) == len(REVIEWS), f"{len(calls)} per-item calls"
    for review in REVIEWS:
        assert sum(review in r.system for r in calls) == 1, review
    for call in calls:
        assert "{fields_formatted}" not in call.system
        assert "product" in call.system and "sentiment" in call.system
        assert "positive, negative, neutral" in call.system
    assert [r[0] for r in _rows(db_path)] == ["resolved"]


@pytest.mark.asyncio
async def test_skill_args_that_cannot_fill_the_template_fail_with_no_step_call(env):
    db_path, mp = env
    node = {**LIVE_NODE, "skill_args": {**LIVE_NODE["skill_args"], "fields": []}}
    stub = StubModels(plan_nodes=[node], answer_for=_answer).install(mp)
    await run_pipeline("Extract product and sentiment from each review.", db_path=db_path)
    await drain_log_writes()
    assert not stub.of_kind("answerer"), "an unfillable template must make no model call"
    assert not _rows(db_path), "no apply_to_each envelope may be sent"
    # The step fails at build time; re_plan is told why, by code and placeholder.
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    text = " ".join(r.system + r.user for r in replans)
    assert "BRT-SKL-008" in text and "fields_formatted" in text
