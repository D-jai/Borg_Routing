"""Skill render review (16-Nov-2026, BUILD.md section 8): every rendered field
is complete and every schema is a real per-item schema.

TC-SKL-016 — "derived from fields" builds a flat schema, one key per field
TC-SKL-017 — fields given as names, a dict or a comma string normalise the same
TC-SKL-018 — nameless, duplicate or empty fields raise BRT-SKL-008
TC-SKL-019 — a schema arg given as a JSON string is parsed; invalid JSON fails
TC-SKL-020 — every templated string field in skills.yaml renders with no {name}
TC-SKL-021 — concurrency_limit renders to the arg's int (null/omitted -> 20)
TC-SKL-022 — decompose_task context and retry_escalate trigger_reason render
TC-SKL-023 — a null in an arg whose hint has no "| null" raises BRT-SKL-008
TC-SKL-024 — an omitted "| null" arg is treated as null
TC-SKL-025 — _instruction wording: "100 words" once; a list joined with ", "
TC-SKL-026 — list args given as a comma string are split; buckets as enum
TC-SKL-027 — the planner's catalogue marks optional args
"""
import json
import re

import pytest
import yaml

from src.skills.library import SkillLibrary

SKILLS_PATH = "data/skills.yaml"
PLACEHOLDER = re.compile(r"\{(\w+)\}")

with open(SKILLS_PATH, encoding="utf-8") as _f:
    _SKILLS = yaml.safe_load(_f)


@pytest.fixture(scope="module")
def lib() -> SkillLibrary:
    sl = SkillLibrary()
    sl.load(SKILLS_PATH)
    return sl


def _extract(lib, fields):
    return lib.render("extract_fields", 1,
                      {"items_ref": "n1", "fields": fields, "concurrency_limit": 3})


# -- 1. derived schema -------------------------------------------------------

def test_derived_from_fields_builds_flat_schema(lib):
    """TC-SKL-016."""
    task = _extract(lib, [
        {"name": "product", "type": "string", "description": "the product"},
        {"name": "sentiment", "type": "enum", "enum_values": ["positive", "negative"],
         "description": "the sentiment"},
        {"name": "years"},
    ])
    assert "schema_note" not in task.output_schema
    assert list(task.output_schema) == ["product", "sentiment", "years"]
    assert task.output_schema["product"] == "string | null — the product"
    assert task.output_schema["sentiment"] == "enum:positive|negative|null — the sentiment"
    assert task.output_schema["years"] == "string | null"


@pytest.mark.parametrize("fields", [
    ["product", "sentiment"],
    "product, sentiment",
    {"product": "string", "sentiment": {"type": "enum", "enum_values": ["pos", "neg"]}},
], ids=["names", "comma_string", "dict"])
def test_field_shapes_normalise(lib, fields):
    """TC-SKL-017."""
    task = _extract(lib, fields)
    assert list(task.output_schema) == ["product", "sentiment"]
    assert task.output_schema["product"] == "string | null"
    lines = [l for l in task.instruction.splitlines() if l.startswith("- ")]
    assert [l.split()[1] for l in lines] == ["product", "sentiment"], task.instruction
    if isinstance(fields, dict):
        assert task.output_schema["sentiment"] == "enum:pos|neg|null"


@pytest.mark.parametrize("fields", [
    [{"field": "product", "type": "string"}],
    [{"name": "product"}, {"name": "product"}],
    ["product", "product"],
    "product, , sentiment",
    [],
    "",
    {},
], ids=["nameless", "dup_dicts", "dup_names", "empty_entry", "empty_list", "empty_str",
        "empty_dict"])
def test_bad_fields_raise_skl_008(lib, fields):
    """TC-SKL-018."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        _extract(lib, fields)


# -- 2. JSON-string schema ---------------------------------------------------

def test_json_string_schema_arg_is_parsed(lib):
    """TC-SKL-019."""
    task = lib.render("decompose_task", 1, {
        "sub_task_instruction": "do X", "context_for_child": "ctx",
        "expected_return_schema": '{"a": "string"}'})
    assert task.output_schema == {"a": "string"}
    task = lib.render("retry_escalate", 1, {
        "failed_items_ref": "n3", "original_instruction": "classify",
        "original_output_schema": '{"bucket": "string"}', "escalation_note": None})
    assert task.output_schema == {"bucket": "string"}


@pytest.mark.parametrize("bad", ["{not json", '["a"]', "plain words"])
def test_invalid_json_schema_arg_raises_skl_008(lib, bad):
    """TC-SKL-019 (failure path)."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        lib.render("decompose_task", 1, {
            "sub_task_instruction": "do X", "context_for_child": "ctx",
            "expected_return_schema": bad})


# -- 6. every templated field renders ----------------------------------------

def _sample(name: str, hint: str):
    hint = hint.replace(" ", "")
    if name.endswith("_ref"):
        return "n0"
    if hint.startswith("list[dict"):
        return [{"name": "alpha", "type": "string", "weight": 0.5, "description": "a"},
                {"name": "beta", "type": "int", "weight": 0.5, "description": "b"}]
    if hint.startswith("list"):
        return ["red", "green"]
    if hint.startswith("dict"):
        return {"answer": "string"}
    if hint.startswith("int"):
        return 5
    if hint.startswith("float"):
        return 0.7
    return "sample text"


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


@pytest.mark.parametrize("skill", _SKILLS, ids=[s["skill_id"] for s in _SKILLS])
def test_every_templated_field_is_rendered(lib, skill):
    """TC-SKL-020."""
    args = {n: _sample(n, str(h)) for n, h in (skill.get("required_args") or {}).items()}
    task = lib.render(skill["skill_id"], skill.get("version", 1), args)
    rendered = task.model_dump(mode="json", exclude={"skill_args"})
    leftovers = {s for s in _strings(rendered) for s in PLACEHOLDER.findall(s)}
    assert not leftovers, f"{skill['skill_id']}: unfilled {leftovers}"
    # Each templated top-level field reaches its task field, filled.
    where = {
        "instruction": task.instruction,
        "context": task.context,
        "state_query": task.state_query,
        "trigger_reason": task.trigger_reason,
        "inputs_ref": (task.dependency_input or {}).get("inputs_ref"),
        "concurrency_limit": (task.dependency_input or {}).get("concurrency_limit"),
        "output_schema": task.output_schema,
    }
    for key, raw in (skill.get("outbound_template") or {}).items():
        if not (isinstance(raw, str) and PLACEHOLDER.search(raw)):
            continue
        assert key in where, f"{skill['skill_id']}: templated field '{key}' is never rendered"
        value = where[key]
        assert value not in (None, "", {}, "initial_plan"), f"{skill['skill_id']}.{key}: {value!r}"
        whole = PLACEHOLDER.fullmatch(raw.strip())
        if whole and isinstance(args.get(whole.group(1)), (str, int)) and key != "output_schema":
            assert value == args[whole.group(1)], f"{skill['skill_id']}.{key}: {value!r}"


@pytest.mark.parametrize("value,expected", [(7, 7), ("7", 7), (None, 20), ("omit", 20)])
def test_concurrency_limit_renders(lib, value, expected):
    """TC-SKL-021."""
    args = {"items_ref": "n1", "criterion": "x"}
    if value != "omit":
        args["concurrency_limit"] = value
    task = lib.render("filter_by_criterion", 1, args)
    assert task.dependency_input["concurrency_limit"] == expected


def test_concurrency_limit_not_an_int_raises_skl_008(lib):
    """TC-SKL-021 (failure path)."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        lib.render("filter_by_criterion", 1,
                   {"items_ref": "n1", "criterion": "x", "concurrency_limit": "many"})


def test_decompose_context_and_retry_trigger_render(lib):
    """TC-SKL-022."""
    task = lib.render("decompose_task", 1, {
        "sub_task_instruction": "do X", "context_for_child": "the distilled context",
        "expected_return_schema": {"a": "string"}})
    assert task.context == "the distilled context"
    task = lib.render("retry_escalate", 1, {
        "failed_items_ref": "n3", "original_instruction": "classify",
        "original_output_schema": {"b": "string"}, "escalation_note": None})
    assert task.trigger_reason == "escalation_from:n3"


# -- 8. nulls ------------------------------------------------------------------

@pytest.mark.parametrize("skill_id,args,null_arg", [
    ("filter_by_criterion", {"items_ref": "n1", "concurrency_limit": 3}, "criterion"),
    ("summarize", {"items_ref": "n1", "focus": None}, "max_points"),
    ("decompose_task", {"sub_task_instruction": "x", "context_for_child": "c"},
     "expected_return_schema"),
])
def test_null_in_non_optional_arg_raises_skl_008(lib, skill_id, args, null_arg):
    """TC-SKL-023."""
    with pytest.raises(RuntimeError, match=f"BRT-SKL-008.*{null_arg}"):
        lib.render(skill_id, 1, {**args, null_arg: None})


def test_omitted_optional_arg_is_null(lib):
    """TC-SKL-024."""
    task = lib.render("transform_style", 1, {"items_ref": "n1", "target_style": "formal"})
    assert "None" not in task.instruction
    assert "Target style: formal" in task.instruction
    task = lib.render("summarize", 1, {"items_ref": "n1", "max_points": 3})
    assert "Focus on" not in task.instruction


def test_omitted_non_optional_arg_still_raises_skl_005(lib):
    """TC-SKL-024 (failure path)."""
    with pytest.raises(RuntimeError, match="BRT-SKL-005.*criterion"):
        lib.render("filter_by_criterion", 1, {"items_ref": "n1"})


# -- 9. _instruction wording -----------------------------------------------------

def test_instruction_wording(lib):
    """TC-SKL-025."""
    task = lib.render("transform_style", 1, {
        "items_ref": "n1", "target_style": "x",
        "target_format": ["markdown", "plain"], "max_length": "100 words"})
    assert "Keep it to at most 100 words." in task.instruction
    assert "words words" not in task.instruction
    assert "Target format: markdown, plain." in task.instruction
    assert "[" not in task.instruction


# -- 10. comma strings for list args -----------------------------------------------

def test_comma_string_list_args_are_split(lib):
    """TC-SKL-026."""
    task = lib.render("classify_into_buckets", 1,
                      {"buckets": "billing, sales", "items_ref": "n1",
                       "concurrency_limit": 3})
    assert task.output_schema["bucket"] == "enum:billing|sales|defer"
    task = lib.render("classify_into_buckets", 1,
                      {"buckets": ["billing", "sales"], "items_ref": "n1",
                       "concurrency_limit": 3})
    assert task.output_schema["bucket"] == "enum:billing|sales|defer"
    assert '["' not in task.instruction
    task = lib.render("compare_and_rank", 1,
                      {"items_ref": "n1", "criteria": "price, speed"})
    assert "- price\n- speed" in task.instruction


# -- 11. catalogue marks optional args ---------------------------------------------

def test_catalogue_marks_optional_args(lib):
    """TC-SKL-027."""
    text = lib.catalogue_for_prompt()
    assert "**Required args:** items_ref, target_style; optional: target_format, max_length" in text
    assert "**Required args:** claim, evidence_ref\n" in text
