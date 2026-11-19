"""Skill render round-2 review (16-Nov-2026, BUILD.md section 8, "1ba958f
round-2 review (4)").

TC-SKL-028 — a list[dict] arg (fields) given as a JSON object string is parsed
             as JSON, never split on its commas; a JSON array string too
TC-SKL-029 — a list arg that looks like JSON but is not valid JSON, or a JSON
             object for a list arg that cannot take one, raises BRT-SKL-008
TC-SKL-030 — a whole-number float concurrency_limit (12.0, "12.0") is 12
TC-SKL-031 — a non-whole or non-numeric concurrency_limit raises BRT-SKL-008
"""
import pytest

from src.skills.library import SkillLibrary

SKILLS_PATH = "data/skills.yaml"


@pytest.fixture(scope="module")
def lib() -> SkillLibrary:
    sl = SkillLibrary()
    sl.load(SKILLS_PATH)
    return sl


def _extract(lib, fields, concurrency_limit=3):
    return lib.render("extract_fields", 1, {
        "items_ref": "n1", "fields": fields, "concurrency_limit": concurrency_limit})


# -- fields as a JSON string ----------------------------------------------------

def test_fields_json_object_string_one_field(lib):
    """TC-SKL-028."""
    task = _extract(lib, '{"a": "string"}')
    assert task.output_schema == {"a": "string | null"}


def test_fields_json_object_string_two_fields(lib):
    """TC-SKL-028: the comma inside the object is not a field separator."""
    task = _extract(lib, '{"a":"string","b":"int"}')
    assert task.output_schema == {"a": "string | null", "b": "int | null"}


def test_fields_json_array_string_keeps_commas_in_descriptions(lib):
    """TC-SKL-028: a JSON array string is parsed, not split."""
    task = _extract(lib, '[{"name": "a", "description": "x, y"}, {"name": "b"}]')
    assert task.output_schema == {"a": "string | null — x, y", "b": "string | null"}


def test_criteria_json_object_string_is_parsed(lib):
    """TC-SKL-028: any list[dict] arg takes a JSON object string."""
    task = lib.render("compare_and_rank", 1, {
        "items_ref": "n1", "criteria": '{"price": "low is better", "speed": "fast"}'})
    assert "- price (low is better)\n- speed (fast)" in task.instruction, task.instruction


@pytest.mark.parametrize("bad", ['{"a": "string"', '{a: string}', '[{"name": "a"},', "[not json"])
def test_fields_invalid_json_string_raises_skl_008(lib, bad):
    """TC-SKL-029."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        _extract(lib, bad)


def test_list_of_str_arg_given_a_json_object_raises_skl_008(lib):
    """TC-SKL-029: buckets (list[str]) cannot be a JSON object."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        lib.render("classify_into_buckets", 1, {
            "items_ref": "n1", "buckets": '{"x": 1}', "concurrency_limit": 3})


def test_plain_comma_string_still_splits(lib):
    """TC-SKL-028 (control): a string that does not look like JSON is a comma list."""
    task = _extract(lib, "a, b")
    assert list(task.output_schema) == ["a", "b"]


# -- concurrency_limit -----------------------------------------------------------

@pytest.mark.parametrize("value", [12.0, "12.0", " 12.0 ", "12"])
def test_whole_number_float_concurrency_limit_is_accepted(lib, value):
    """TC-SKL-030."""
    task = _extract(lib, ["a"], concurrency_limit=value)
    assert task.dependency_input["concurrency_limit"] == 12
    assert isinstance(task.dependency_input["concurrency_limit"], int)


@pytest.mark.parametrize("value", [2.5, "2.5", "many", "nan", "inf", float("inf"), True, 0.0, "-3.0"])
def test_bad_concurrency_limit_raises_skl_008(lib, value):
    """TC-SKL-031."""
    with pytest.raises(RuntimeError, match="BRT-SKL-008"):
        _extract(lib, ["a"], concurrency_limit=value)
