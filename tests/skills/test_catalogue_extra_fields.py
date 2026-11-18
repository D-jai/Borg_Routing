"""Planner ruling 35c: the planner's skill catalogue teaches ``extra_fields``.

Only a skill whose step has a per-item output schema of its own (an
apply_to_each step whose schema is written in the skill, not taken from an arg)
shows the text: classify_into_buckets, summarize, filter_by_criterion,
transform_style. The other six do not (see the reasons per skill below).
"""
import re
import uuid

import pytest

from src.envelopes.schema import WrappedTask
from src.orchestrator import engine
from src.skills.library import SkillLibrary
from tests.prompts.test_orchestrator_prompt_routing_labels import planner_prompt  # noqa: F401

TEXT = 'extra_fields (more fields per item, for example {"urgency": "integer 1-5"})'
SHOWN = {"classify_into_buckets", "summarize", "filter_by_criterion", "transform_style"}
# extract_fields: fields come from its `fields` arg; compare_and_rank,
# verify_claim: one answer, not per item; decompose_task: a child plan;
# retry_escalate, defer_and_collect: the schema is an arg, not the skill's own.
NOT_SHOWN = {"extract_fields", "compare_and_rank", "decompose_task", "verify_claim",
             "retry_escalate", "defer_and_collect"}


@pytest.fixture(scope="module")
def lib() -> SkillLibrary:
    sl = SkillLibrary()
    sl.load("data/skills.yaml")
    return sl


def _sections(text: str) -> dict:
    parts = re.split(r"(?m)^### skill: (\w+)", text)
    return {parts[i]: parts[i + 1] for i in range(1, len(parts), 2)}


def test_the_split_covers_all_ten_skills(lib):
    assert set(_sections(lib.catalogue_for_prompt())) == SHOWN | NOT_SHOWN


def test_exactly_these_skills_show_the_extra_fields_text(lib):
    sec = _sections(lib.catalogue_for_prompt())
    assert {s for s, t in sec.items() if "extra_fields" in t} == SHOWN
    for s in SHOWN:
        assert TEXT in sec[s]
    for s in NOT_SHOWN:
        assert "extra_fields" not in sec[s]


def _args_line(section: str) -> str:
    return next(l for l in section.splitlines() if l.startswith("**Required args:**"))


def test_classify_keeps_concurrency_limit_and_adds_extra_fields(lib):
    line = _args_line(_sections(lib.catalogue_for_prompt())["classify_into_buckets"])
    assert line == f"**Required args:** buckets, items_ref; optional: concurrency_limit, {TEXT}"


def test_summarize_and_transform_keep_their_own_optional_args(lib):
    sec = _sections(lib.catalogue_for_prompt())
    assert _args_line(sec["summarize"]) == (
        f"**Required args:** items_ref, max_points; optional: focus, {TEXT}")
    assert "concurrency_limit" not in sec["summarize"]
    assert _args_line(sec["transform_style"]).startswith(
        "**Required args:** items_ref, target_style; optional: target_format, max_length, ")


def test_a_skill_without_it_is_unchanged(lib):
    sec = _sections(lib.catalogue_for_prompt())
    assert _args_line(sec["verify_claim"]) == "**Required args:** claim, evidence_ref"
    assert _args_line(sec["extract_fields"]) == (
        "**Required args:** items_ref, fields; optional: concurrency_limit")
    assert _args_line(sec["defer_and_collect"]) == (
        "**Required args:** items_ref, instruction, output_schema, confidence_threshold")


@pytest.mark.asyncio
async def test_the_planner_prompt_the_engine_builds_shows_the_text(planner_prompt):  # noqa: F811
    assert TEXT in planner_prompt


# -- a classify step with extra_fields, built by the engine, packs and returns the field ----

def _items(n):
    return [f"note {i:03d}" for i in range(n)]


def _node(items):
    return {"type": "apply_to_each", "id": "n1", "skill_id": "classify_into_buckets",
            "skill_args": {"buckets": ["a", "b"], "items_ref": items,
                           "extra_fields": {"urgency": "integer 1-5"}}}


def _from_node(node):
    from tests.e2e.test_item_packing import OPERATOR
    return engine._build_task_from_node(
        node, run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1",
        model_endpoint="https://openrouter.ai/api", model_name="openrouter/stub/cheap",
        user_instruction=OPERATOR, results={}, data_scope="derived", lane=None)


def test_the_built_step_asks_for_urgency():
    task = _from_node(_node(_items(2)))
    assert isinstance(task, WrappedTask) and task.skill_id == "classify_into_buckets"
    assert "urgency" in task.output_schema
    assert "Also return these fields: urgency (integer 1-5)." in task.instruction


from tests.e2e.test_item_packing import pk  # noqa: E402,F401
from tests.e2e.test_item_packing import _run  # noqa: E402


async def test_the_step_packs_two_packs_of_20_and_returns_urgency_per_item(pk):
    out = {"bucket": "a", "confidence": 0.9, "urgency": 3}
    stub, env = await _run(pk, _from_node(_node(_items(40))),
                           pack_item_for=lambda r, t: dict(out),
                           answer_for=lambda r: dict(out))
    assert [len(r.pack_ids) for r in stub.of_kind("packed")] == [20, 20]
    assert env.output["total"] == 40 and env.output["success_count"] == 40
    assert all(r["output"]["urgency"] == 3 for r in env.output["results"])
