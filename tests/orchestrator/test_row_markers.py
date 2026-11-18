"""Unit tests for src/orchestrator/row_markers.py (planner ruling 21; design
note docs/design/compose_row_markers.md, sections 2-5)."""
import json
from uuid import uuid4

import pytest

from src.envelopes.schema import InboundEnvelope
from src.orchestrator import engine, row_markers as rm


def _env(output, node_type="apply_to_each", scope=None, status="resolved"):
    env = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status=status,
                          output=output, **({"failure_reason": "x"} if status == "failed" else {}))
    env._node_type = node_type
    env._data_scope = scope
    return env


def _agg(results, failures=()):
    return {"results": list(results), "failures": list(failures),
            "total": len(results) + len(failures), "success_count": len(results),
            "failure_count": len(failures)}


class _Lane:
    keeps_raw_local = True
    has_documents = True

    @staticmethod
    def is_part_id(value):
        return isinstance(value, str) and value.startswith("doc_")


# --- rows ---------------------------------------------------------------------

def test_rows_merge_results_and_failures_in_input_order():
    env = _env(_agg(
        [{"index": 2, "item": "c", "output": {"s": 3}, "confidence": 1},
         {"index": 0, "item": "a", "output": {"s": 1}, "confidence": 1}],
        [{"index": 1, "item": "b", "reason": {"code": "BRT-LANE-004", "doc": "doc_1"}}],
    ))
    assert rm.rows_for(env, None) == [
        {"item": "a", "s": 1}, {"item": "b", "error": "BRT-LANE-004"}, {"item": "c", "s": 3}]


@pytest.mark.parametrize("reason, from_model, code", [
    ("BRT-ANS-005: missing keys", False, "BRT-ANS-005"),
    ("BRT-ANS-005: it said so", True, "BRT-ANS-010"),
    ("something odd", False, "BRT-ORC-004"),
    ({"code": "BRT-ANS-010", "message": "no"}, False, "BRT-ANS-010"),
])
def test_failure_codes(reason, from_model, code):
    entry = {"index": 0, "item": "a", "reason": reason}
    env = _env(_agg([], [entry]))
    if from_model:
        env._model_reason_indexes = {0}
    assert rm.rows_for(env, None) == [{"item": "a", "error": code}]


@pytest.mark.parametrize("item, expected", [
    ("text", "text"), (7, 7), ({"id": "x1", "body": "long"}, "x1"), ({"body": "b"}, 0), ([1], 0),
])
def test_item_id_rules(item, expected):
    env = _env(_agg([{"index": 0, "item": item, "output": {"s": 1}}]))
    assert rm.rows_for(env, None)[0]["item"] == expected


def test_output_keys_renamed_and_non_object_output():
    env = _env(_agg([{"index": 0, "item": "a", "output": {"item": "i", "error": "e", "k": 1}},
                     {"index": 1, "item": "b", "output": "plain"}]))
    assert rm.rows_for(env, None) == [
        {"item": "a", "output_item": "i", "output_error": "e", "k": 1},
        {"item": "b", "output": "plain"}]


def test_raw_item_is_part_id_or_index():
    env = _env(_agg([{"index": 0, "item": "doc_001", "output": {"s": 1}},
                     {"index": 1, "item": "RAW TEXT", "output": {"s": 2}}]), scope="raw")
    assert [r["item"] for r in rm.rows_for(env, _Lane())] == ["doc_001", 1]
    assert [r["item"] for r in rm.rows_for(env, None)] == ["doc_001", "RAW TEXT"]


def test_state_op_entries_sorted_and_plain_list_stored_order():
    entries = _env({"result": [{"index": 3, "item": "d", "output": {"s": 4}},
                               {"index": 1, "item": "b", "output": {"s": 2}}]}, "state_op")
    plain = _env({"result": [{"name": "z"}, {"name": "y"}]}, "state_op")
    steps = rm.list_steps({"e": entries, "p": plain, "c": _env({"result": {"n": 2}}, "state_op")}, None)
    assert set(steps) == {"e", "p"}
    assert steps["e"].rows == [{"item": "b", "s": 2}, {"item": "d", "s": 4}]
    assert steps["e"].order == "input order"
    assert steps["p"].rows == [{"name": "z"}, {"name": "y"}]
    assert steps["p"].order == "stored order"


def test_raw_state_op_of_plain_values_not_offered():
    raw_plain = _env({"result": ["secret one", "secret two"]}, "state_op", scope="raw")
    assert rm.list_steps({"r": raw_plain}, _Lane()) == {}


def test_not_list_steps():
    results = {
        "ans": _env({"result": [1, 2]}, "answerer"),
        "failed": _env(_agg([]), status="failed"),
        "bad id@": _env(_agg([{"index": 0, "item": "a", "output": {}}])),
        " pad": _env(_agg([{"index": 0, "item": "a", "output": {}}])),
        "x" * 201: _env(_agg([{"index": 0, "item": "a", "output": {}}])),
        "ok": _env(_agg([])),
    }
    steps = rm.list_steps(results, None)
    assert list(steps) == ["ok"] and steps["ok"].rows == []


def test_fields_and_block_line():
    env = _env(_agg([{"index": 0, "item": "a", "output": {"b": 1, "c": 2}},
                     {"index": 1, "item": "b", "output": {"d": 3}}],
                    [{"index": 2, "item": "c", "reason": "BRT-ANS-005: x"}]))
    steps = rm.list_steps({"s": env}, None)
    assert rm.step_lines(steps) == "- s: 3 rows, input order; fields: item, b, c, d, error"


# --- parsing --------------------------------------------------------------------

def _steps():
    return rm.list_steps({"rate": _env(_agg([{"index": 0, "item": "a|b", "output": {"s": "x\ny"}}]))}, None)


@pytest.mark.parametrize("text", ["@@ROWS:nope@@", "@@Row:rate@@", "@@ROWS rate@@",
                                  "@@ROWS:rate@", "@@ rows_tabel:rate@@", "a @@row"])
def test_invalid_hits(text):
    assert rm.invalid_hits(text, _steps(), [])


@pytest.mark.parametrize("text", ["@@ROWS:rate@@", "@@ rows : rate @@", "@@rows_table:rate@@",
                                  "no marker", "diff @@ -1,3 +1,4 @@", "mail a@b.c"])
def test_valid_or_no_hits(text):
    assert rm.invalid_hits(text, _steps(), []) == []


def test_quoted_hit_is_not_invalid():
    assert rm.invalid_hits("see @@ROWS:example@@", _steps(), ["what is @@ROWS:example@@"]) == []


# --- rendering ------------------------------------------------------------------

def test_json_block_fenced_and_backticks_dropped():
    out, placed = rm.place("A `@@ROWS:rate@@` B", _steps())
    assert out == 'A\n```json\n[\n{"item": "a|b", "s": "x\\ny"}\n]\n```\nB'
    assert placed == {"rate": 1}


def test_json_marker_alone_inside_fence_not_double_fenced():
    out, _ = rm.place("```json\n@@ROWS:rate@@\n```", _steps())
    assert out == '```json\n[\n{"item": "a|b", "s": "x\\ny"}\n]\n```'


def test_table_escapes_and_reopens_fence():
    out, _ = rm.place("```text\nhead\n@@ROWS_TABLE:rate@@\ntail\n```", _steps())
    assert out == ("```text\nhead\n```\n\n| item | s |\n| --- | --- |\n| a\\|b | x y |\n\n"
                   "```text\ntail\n```")


def test_dict_leaf_rules():
    value = {"all": "@@ROWS:rate@@", "note": "in line @@ROWS:rate@@ here"}
    out, placed = rm.place(value, _steps())
    assert out["all"] == [{"item": "a|b", "s": "x\ny"}]
    assert out["note"] == 'in line [{"item": "a|b", "s": "x\\ny"}] here'
    assert placed == {"rate": 2}


def test_no_rescan_of_placed_rows():
    env = _env(_agg([{"index": 0, "item": "@@ROWS:other@@", "output": {}}]))
    other = _env(_agg([{"index": 0, "item": "secret", "output": {}}]))
    steps = rm.list_steps({"one": env, "other": other}, None)
    out, placed = rm.place("@@ROWS:one@@", steps)
    assert "secret" not in out and placed == {"one": 1}


def test_guard_called_per_block():
    seen = []
    rm.place("@@ROWS:rate@@ and @@ROWS_TABLE:rate@@", _steps(),
             guard=lambda rows, text: seen.append((len(rows), text[:3])))
    assert [s[0] for s in seen] == [1, 1]


# --- review of 8fd4c23 -----------------------------------------------------------

def test_marker_as_a_dict_key_is_left_over():
    assert rm.invalid_hits({"@@ROWS:rate@@": "x"}, _steps(), [])
    assert rm.invalid_hits([{"k": {"@@rows:rate@@": 1}}], _steps(), [])


def test_quoted_hit_matches_decoded_leaf_and_runs_on():
    quoted = ['use @@rows "x" here']
    assert rm.invalid_hits('They said: use @@rows "x" here. Done.', _steps(), quoted) == []
    assert rm.invalid_hits('use @@rows "y" here', _steps(), quoted)


@pytest.mark.parametrize("step_id", ['ra"te', "ra\\te"])
def test_marker_in_a_dict_leaf_is_read_decoded(step_id):
    steps = rm.list_steps({step_id: _env(_agg([{"index": 0, "item": "a", "output": {}}]))}, None)
    value = {"rows": f"@@ROWS:{step_id}@@"}
    assert rm.invalid_hits(value, steps, []) == []
    out, _ = rm.place(value, steps)
    assert out == {"rows": [{"item": "a"}]}


TABLE = "| item | s |\n| --- | --- |\n| a\\|b | x y |"


@pytest.mark.parametrize("fence, close", [("````text", "````"), ("~~~", "~~~")])
def test_table_inside_long_or_tilde_fence_ends_up_outside(fence, close):
    text = f"Intro\n{fence}\nhead\n```\n@@ROWS_TABLE:rate@@\ntail\n{close}\nEnd"
    out, _ = rm.place(text, _steps())
    assert out == f"Intro\n{fence}\nhead\n```\n{close}\n\n{TABLE}\n\n{fence}\ntail\n{close}\nEnd"


@pytest.mark.parametrize("fence", ["~~~", "````"])
def test_table_alone_in_a_fence_leaves_no_stray_fence(fence):
    out, _ = rm.place(f"Intro\n{fence}\n@@ROWS_TABLE:rate@@\n{fence}\nEnd", _steps())
    assert out == f"Intro\n\n{TABLE}\n\nEnd"
    assert "~" not in out and "`" not in out


def test_tilde_fence_counts_for_the_json_form():
    out, _ = rm.place("~~~\n@@ROWS:rate@@\n~~~", _steps())
    assert out == '~~~\n[\n{"item": "a|b", "s": "x\\ny"}\n]\n~~~'


def test_table_marker_in_a_longer_leaf_is_compact():
    out, _ = rm.place({"n": "see @@ROWS_TABLE:rate@@ ok"}, _steps())
    assert out == {"n": 'see [{"item": "a|b", "s": "x\\ny"}] ok'}


def test_scalar_state_op_list_line():
    steps = rm.list_steps({"v": _env({"result": [3, 1, 2]}, "state_op")}, None)
    assert rm.step_lines(steps) == "- v: 3 rows, stored order; fields: (values)"


# --- review round 2 (final fix) ----------------------------------------------------

@pytest.mark.parametrize("quoted, reply", [
    (["Rows are marked @@ROWS\nnext line"], "here: @@ROWS:bogus@@"),
    (["@@ROW"], "see @@ROWS_TABLE:typo@@"),
    (["@@ROWS:other@@"], "see @@ROWS:bogus@@"),
])
def test_a_quoted_fragment_does_not_exempt_a_whole_invalid_marker(quoted, reply):
    assert rm.invalid_hits(reply, _steps(), quoted), "an invalid marker must not survive"


def test_an_exactly_quoted_marker_is_still_left_as_text():
    assert rm.invalid_hits("as you wrote, @@ROWS:x@@", _steps(), ["what does @@ROWS:x@@ do?"]) == []


@pytest.mark.parametrize("text", [
    "Intro\n```\n@@ROWS_TABLE:rate@@\n\n```\nEnd",
    "Intro\n```\n\n@@ROWS_TABLE:rate@@\n```\nEnd",
    "Intro\n```\n@@ROWS_TABLE:rate@@",
    "Intro\r\n```\r\n@@ROWS_TABLE:rate@@\r\n```\r\nEnd",
])
def test_no_empty_or_stray_fence_is_left_around_a_table(text):
    out, _ = rm.place(text, _steps())
    assert TABLE in out
    assert "`" not in out, repr(out)
