"""Every placeholder in a rendered skill is filled, or render fails loudly.

Live fault (Check 1 attempt 3): extract_fields reached the model as the raw
template "Fields to extract:\\n{fields_formatted}". The template names derived
args (``fields_formatted``, ``criteria_formatted``, ``focus_instruction``,
``target_format_instruction``, ``max_length_instruction``) that no arg
supplies; render never built them and left the placeholder in.

TC-SKL-012 — each skill in data/skills.yaml renders with no leftover {name}
TC-SKL-013 — extract_fields with the live arg shapes names every field
TC-SKL-014 — a placeholder nothing can fill raises BRT-SKL-008
TC-SKL-015 — a derived arg whose source is empty raises BRT-SKL-008
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

# Live node n4 (Check 1 attempt 3, re_plan): fields as dicts, one with enum_values.
LIVE_FIELDS_DICTS = [
    {"name": "product", "type": "string",
     "description": "The product name from the review (e.g., 'Brewmaster kettle')"},
    {"name": "sentiment", "type": "enum", "enum_values": ["positive", "negative", "neutral"],
     "description": "The sentiment expressed in the review"},
]
# Live node n7: fields as bare names.
LIVE_FIELDS_NAMES = ["product", "sentiment"]


def _sample(name: str, hint: str):
    """A representative value for a required arg, from its type hint."""
    hint = (hint or "").replace(" ", "")
    if name.endswith("_ref"):
        return "n0"
    if hint.startswith("list[dict"):
        return [{"name": "alpha", "type": "string", "weight": 0.5, "description": "the alpha"},
                {"name": "beta", "type": "int", "weight": 0.5, "description": "the beta"}]
    if hint.startswith("list"):
        return ["red", "green"]
    if hint.startswith("dict"):
        return {"answer": "string"}
    if hint.startswith("int"):
        return 5
    if hint.startswith("float"):
        return 0.7
    return "sample text"


def _args(skill: dict, nulls: bool = False) -> dict:
    """Representative args; with nulls, every optional (``| null``) arg is None."""
    args = {}
    for name, hint in (skill.get("required_args") or {}).items():
        optional = "null" in str(hint)
        args[name] = None if (nulls and optional) else _sample(name, str(hint))
    return args


def _leftovers(task) -> set[str]:
    text = "\n".join([
        task.instruction,
        task.state_query or "",
        str((task.dependency_input or {}).get("inputs_ref", "")),
        json.dumps(task.output_schema),
    ])
    return set(PLACEHOLDER.findall(text))


@pytest.fixture(scope="module")
def lib() -> SkillLibrary:
    sl = SkillLibrary()
    sl.load(SKILLS_PATH)
    return sl


@pytest.mark.parametrize("nulls", [False, True], ids=["all_args", "optional_null"])
@pytest.mark.parametrize("skill", _SKILLS, ids=[s["skill_id"] for s in _SKILLS])
def test_each_skill_renders_with_no_leftover_placeholder(lib, skill, nulls):
    """TC-SKL-012."""
    task = lib.render(skill["skill_id"], skill.get("version", 1), _args(skill, nulls))
    assert not _leftovers(task), f"{skill['skill_id']}: unfilled {_leftovers(task)}"
    assert "None" not in task.instruction, task.instruction


@pytest.mark.parametrize("fields", [LIVE_FIELDS_DICTS, LIVE_FIELDS_NAMES], ids=["dicts", "names"])
def test_extract_fields_live_shapes_name_every_field(lib, fields):
    """TC-SKL-013."""
    task = lib.render("extract_fields", 1,
                      {"items_ref": "", "fields": fields, "concurrency_limit": 12})
    assert "{fields_formatted}" not in task.instruction
    assert "product" in task.instruction and "sentiment" in task.instruction
    if fields is LIVE_FIELDS_DICTS:
        assert "positive" in task.instruction
        assert "The sentiment expressed in the review" in task.instruction


def test_placeholder_nothing_can_fill_raises_skl_008(lib):
    """TC-SKL-014."""
    lib_copy = SkillLibrary()
    lib_copy._skills = [{
        "skill_id": "broken_skill", "version": 1, "envelope_type": "answerer",
        "required_args": {"topic": "str"},
        "outbound_template": {"type": "answerer",
                              "instruction": "Write about {topic} using {mystery_arg}."},
    }]
    with pytest.raises(RuntimeError, match=r"BRT-SKL-008.*mystery_arg"):
        lib_copy.render("broken_skill", 1, {"topic": "kettles"})


@pytest.mark.parametrize("empty", [[], None], ids=["empty_list", "none"])
def test_derived_arg_from_empty_source_raises_skl_008(lib, empty):
    """TC-SKL-015."""
    with pytest.raises(RuntimeError, match=r"BRT-SKL-008.*fields_formatted"):
        lib.render("extract_fields", 1,
                   {"items_ref": "n0", "fields": empty, "concurrency_limit": 3})


def test_braces_inside_an_arg_value_are_not_a_placeholder(lib):
    """A value that itself contains {word} is data, not an unfilled placeholder."""
    task = lib.render("filter_by_criterion", 1,
                      {"items_ref": "n0", "criterion": "mentions {price}",
                       "concurrency_limit": 3})
    assert "mentions {price}" in task.instruction
