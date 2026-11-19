"""Planner ruling 20, R4: a skill step's own fields are honoured.

Bench stage 1, job A: the plan put ``extra_fields: {urgency}`` in a
classify_into_buckets step's args and an ``output_schema`` with urgency on
the node, but the rendered prompt asked only for bucket / confidence /
defer_reason, so re_plan ran a second full pass for urgency. The skill
(data/skills.yaml) is unchanged; rendering adds the step's fields.

TC-SKL-032 — extra_fields (map, names, comma string, JSON string) join the
             per-item schema and one "Also return" instruction line
TC-SKL-033 — added fields go into a JSON-Schema skill schema's properties
             and required
TC-SKL-037 — R1-R4 review (6): the node's output_schema is never read (it
             describes the final answer's rows, not each item); render takes
             no node schema; JSON-Schema keywords are never fields
TC-SKL-034 — no extra fields: the render is exactly the skill's
TC-SKL-035 — a nameless, duplicate or invalid-JSON extra_fields raises
             BRT-SKL-008
"""
import pytest

from src.skills.library import SkillLibrary, _with_fields

SKILLS_PATH = "data/skills.yaml"
BUCKETS = ["billing", "technical"]
URGENCY = "integer from 1 (lowest) to 5 (highest)"


@pytest.fixture(scope="module")
def lib() -> SkillLibrary:
    sl = SkillLibrary()
    sl.load(SKILLS_PATH)
    return sl


def _classify(lib, **extra):
    return lib.render("classify_into_buckets", 1,
                      {"buckets": BUCKETS, "items_ref": "n1", **extra})


def test_extra_fields_map_is_asked_for_and_required(lib):
    """TC-SKL-032: the bench's own shape."""
    plain = _classify(lib)
    task = _classify(lib, extra_fields={"urgency": URGENCY})
    assert task.output_schema == {**plain.output_schema, "urgency": URGENCY}
    assert task.instruction.startswith(plain.instruction)
    assert task.instruction.endswith(f"Also return these fields: urgency ({URGENCY}).")
    assert task.skill_args["extra_fields"] == {"urgency": URGENCY}


@pytest.mark.parametrize("extra", [
    ["urgency", "region"], "urgency, region", '["urgency", "region"]',
    [{"name": "urgency"}, {"name": "region", "type": "string"}],
])
def test_extra_fields_other_shapes(lib, extra):
    """TC-SKL-032: names, a comma string, a JSON string, entries."""
    task = _classify(lib, extra_fields=extra)
    assert task.output_schema["urgency"] == "string"
    assert task.output_schema["region"] == "string"
    assert "Also return these fields: urgency (string); region (string)." in task.instruction


def test_json_schema_skill_schema_gets_properties_and_required():
    """TC-SKL-033: added fields go into properties and required."""
    out = _with_fields({"type": "object", "properties": {"a": {"type": "string"}},
                        "required": ["a"]}, {"u": "integer"})
    assert set(out["properties"]) == {"a", "u"} and out["required"] == ["a", "u"]


@pytest.mark.parametrize("extra", [None, {}, [], "", {"bucket": "string", "confidence": "x"}])
def test_no_extra_fields_render_is_the_skills_own(lib, extra):
    """TC-SKL-034 (failure direction): nothing new asked, nothing changed."""
    plain = _classify(lib)
    task = _classify(lib, extra_fields=extra)
    assert task.output_schema == plain.output_schema
    assert task.instruction == plain.instruction
    assert "Also return" not in task.instruction


@pytest.mark.parametrize("extra", [
    [{"type": "integer"}], ["urgency", "urgency"], '{"urgency": ', [3],
])
def test_bad_extra_fields_fail_loudly(lib, extra):
    """TC-SKL-035."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        _classify(lib, extra_fields=extra)


def test_render_takes_no_node_output_schema(lib):
    """TC-SKL-037: job A's node (output_schema {file, category, urgency},
    extra_fields {urgency}) asks for the skill's keys plus urgency only."""
    import inspect
    assert "step_output_schema" not in inspect.signature(SkillLibrary.render).parameters
    task = _classify(lib, extra_fields={"urgency": URGENCY})
    assert list(task.output_schema) == ["bucket", "confidence", "defer_reason", "urgency"]
    assert "file" not in task.instruction.split("Also return")[-1]
    assert "category" not in task.instruction.split("Also return")[-1]


@pytest.mark.parametrize("extra, expected", [
    ({"type": "object", "properties": {"urgency": {"type": "integer"}},
      "required": ["urgency"]}, {"urgency": "integer"}),
    ({"type": "array", "items": {"type": "object", "properties": {
        "urgency": {"type": "integer", "description": "1-5"}}}}, {"urgency": "integer — 1-5"}),
    ({"type": "array", "items": {"type": "string"}}, {}),
    ({"urgency": "integer", "type": "object"}, {"urgency": "integer"}),
    ('{"type": "object", "properties": {"risk": {"type": "string"}}}', {"risk": "string"}),
])
def test_json_schema_keywords_are_never_fields(lib, extra, expected):
    """TC-SKL-037: a JSON-Schema-shaped extra_fields gives its properties;
    type, items, properties, required ... are never added as fields."""
    plain = _classify(lib)
    task = _classify(lib, extra_fields=extra)
    added = {k: v for k, v in task.output_schema.items() if k not in plain.output_schema}
    assert added == expected
    for kw in ("type", "items", "properties", "required"):
        assert kw not in task.output_schema
    if not expected:
        assert task.instruction == plain.instruction
