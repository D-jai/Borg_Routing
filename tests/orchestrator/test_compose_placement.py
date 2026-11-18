"""Unit tests for planner ruling 32e: compose places, it does not retype
(docs/design/compose_placement.md, section 2.7 "Unit tests").

``row_markers`` (targets, specs, joins, labels, OUTPUT, rendering) and
``compose_checks`` (C1, C3, C4, the item model). C2 (BRT-ORC-039) was dropped
by the planner and has no test.
"""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from src.envelopes.schema import InboundEnvelope
from src.lane.documents import Document, DocPart, LoadedDocuments
from src.orchestrator import compose_checks as cc
from src.orchestrator import row_markers as rm


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


def _ok(i, item, **out):
    return {"index": i, "item": item, "output": out}


def _fail(i, item, code="BRT-ANS-005"):
    return {"index": i, "item": item, "reason": {"code": code, "message": "m"}}


class _Lane:
    keeps_raw_local = False
    has_documents = True

    def __init__(self, manifest="names"):
        self.settings = SimpleNamespace(manifest=manifest)
        self.docs = LoadedDocuments([
            Document("doc_001", "job/a.txt", "txt", 1, "x", [DocPart("doc_001", "doc_001", "x")]),
            Document("doc_002", "job/b.txt", "txt", 1, "y", [DocPart("doc_002", "doc_002", "y")]),
            Document("doc_003", "job/c.md", "md", 1, "z",
                     [DocPart("doc_003#1", "doc_003", "z1"), DocPart("doc_003#2", "doc_003", "z2")]),
        ], [])

    def is_part_id(self, value):
        return isinstance(value, str) and value in self.docs.part_index


def _steps(results, lane=None):
    return rm.list_steps(results, lane)


# --- parsing -------------------------------------------------------------------------

def _one(rows=None, sid="s1"):
    rows = rows or [_ok(0, "a", bucket="x", confidence=0.9)]
    return _steps({sid: _env(_agg([r for r in rows if "reason" not in r],
                                  [r for r in rows if "reason" in r]))})


@pytest.mark.parametrize("sid", ["a|b", "a+b"])
def test_ids_holding_a_bar_or_plus_match_whole(sid):
    steps = _one(sid=sid)
    target, problem = rm.parse_target("ROWS", f" {sid} ", steps)
    assert problem is None and target.ids == [sid] and target.spec is None


def test_join_and_spec_parsing():
    steps = _steps({"a": _env(_agg([_ok(0, "x", k=1)])), "b": _env(_agg([_ok(0, "x", m=2)]))})
    target, problem = rm.parse_target("ROWS", "a + b | file=item, k, out=m", steps)
    assert problem is None
    assert target.ids == ["a", "b"] and target.spec == [("file", "item"), ("k", "k"), ("out", "m")]
    target, _ = rm.parse_target("ROWS", "a+b", steps)
    assert target.ids == ["a", "b"] and target.spec is None
    target, _ = rm.parse_target("ROWS", "a | output.k", steps)
    assert target.spec == [("k", "k")]


def test_the_capture_holds_600_characters():
    steps = _one()
    spec = ", ".join(f"n{i}=bucket" for i in range(40))
    text = f"@@ROWS:s1 | {spec}@@"
    assert 200 < len(text) - 4 <= 600
    m = rm.STRICT.search(text)
    assert m and m.group(2).startswith("s1 | n0=bucket")


@pytest.mark.parametrize("reply, code, sentence", [
    ("@@ROWS:nope@@", "BRT-ORC-032", rm.S_NO_STEP),
    ("@@ROWS:s1 + nope | item@@", "BRT-ORC-032", rm.S_NO_STEP),
    ("@@OUTPUT:s1@@", "BRT-ORC-032",
     "@@OUTPUT:s1@@ names a step that has rows, not a long output: use @@ROWS:s1@@ for it."),
    ("@@ROWS:long@@", "BRT-ORC-032",
     "@@ROWS:long@@ names a step that has a long output, not rows: use @@OUTPUT:long@@ for it."),
    ("@@Row:s1@@", "BRT-ORC-032", rm.S_MISSPELLED),
    ("@@OUTPUTS:long@@", "BRT-ORC-032", rm.S_MISSPELLED),
    ("@@ROWS:s1 | " + "a" * 700 + "@@", "BRT-ORC-037", rm.S_TOO_LONG),
    ("@@ROWS:zz | " + "a" * 700 + "@@", "BRT-ORC-032", rm.S_TOO_LONG),
    ("@@ROWS:s1 | file=item, urgency@@", "BRT-ORC-037",
     "The marker for s1 names fields that step does not have: urgency. Its fields are: item, "
     "bucket, confidence. No step produced the others; do not give values for them."),
    ("@@ROWS:s1 | a=item, a=bucket@@", "BRT-ORC-037",
     "The marker for s1 uses the name a twice; each name may appear once."),
    ("@@ROWS:s1 | @@", "BRT-ORC-037", "The marker for s1 has a bar but no fields after it."),
    ("@@ROWS:s1 | [item, bucket]@@", "BRT-ORC-037",
     "The marker for s1 puts its fields in brackets; write them plainly after the bar: "
     "@@ROWS:s1 | name=field, field@@."),
    ("@@ROWS:s1 | a=b=c@@", "BRT-ORC-037",
     "The marker for s1 has a field entry the code cannot read (a=b=c): use plain names with "
     "no , = | @ brackets or line break."),
])
def test_each_marker_problem_has_its_exact_sentence(reply, code, sentence):
    steps = _one()
    outputs = rm.long_outputs({"long": _env({"text": "w " * 1500}, "answerer")}, steps)
    assert rm.marker_problems(reply, steps, outputs, []) == [(code, sentence)]


def test_a_diff_hunk_header_is_masked():
    steps = _one()
    assert rm.marker_problems("@@ -1,2 +1,2 @@ output = x", steps, {}, []) == []
    assert rm.marker_problems("@@ -1,2 +1,2 @@ rows here", steps, {}, []) == []


# --- rows ------------------------------------------------------------------------------

def test_a_failed_row_has_only_the_item_and_error_last():
    steps = _one([_ok(0, "a", bucket="x", confidence=0.9), _fail(1, "b")])
    target, _ = rm.parse_target("ROWS", "s1 | file=item, label=bucket", steps)
    rows, cols = rm.target_rows(target, steps)
    assert rows == [{"file": "a", "label": "x"}, {"file": "b", "error": "BRT-ANS-005"}]
    assert cols == ["file", "label", "error"]
    target, _ = rm.parse_target("ROWS", "s1 | status=error, file=item, bucket", steps)
    rows, _ = rm.target_rows(target, steps)
    assert rows[1] == {"status": "BRT-ANS-005", "file": "b"}
    assert rows[0] == {"status": None, "file": "a", "bucket": "x"}


def test_join_precedence_and_error_codes():
    first = _env(_agg([_ok(0, "a", bucket="x", u=1), _ok(1, "b", bucket="y", u=None),
                       _ok(3, "d", bucket=None)],
                      [_fail(2, "c", "BRT-ANS-005"), _fail(4, "e", "BRT-ANS-004")]))
    later = _env(_agg([_ok(0, "a", u=None), _ok(2, "c", u=3), _ok(3, "d", u=4)],
                      [_fail(1, "b", "BRT-ANS-010"), _fail(4, "e", "BRT-ANS-010")]))
    steps = _steps({"f": first, "g": later})
    target, _ = rm.parse_target("ROWS", "f + g | file=item, bucket, u", steps)
    rows, cols = rm.target_rows(target, steps)
    by = {r["file"]: r for r in rows}
    assert [r["file"] for r in rows] == ["a", "b", "c", "d", "e"]
    assert by["a"] == {"file": "a", "bucket": "x", "u": 1}            # a later null keeps 1
    assert by["b"] == {"file": "b", "bucket": "y", "u": None}         # a later failure keeps y
    assert by["c"] == {"file": "c", "bucket": "BRT-ANS-005", "u": 3}  # source step's code
    assert by["d"] == {"file": "d", "bucket": None, "u": 4}           # resolved with null
    assert by["e"] == {"file": "e", "error": "BRT-ANS-010"}           # no listed step resolved it
    assert cols == ["file", "bucket", "u", "error"]


def test_items_only_in_a_later_step_follow_in_their_order():
    steps = _steps({"f": _env(_agg([_ok(0, "a", k=1)])),
                    "g": _env(_agg([_ok(0, "z", k=2), _ok(1, "a", k=3)]))})
    target, _ = rm.parse_target("ROWS", "f + g | item, k", steps)
    rows, _ = rm.target_rows(target, steps)
    assert rows == [{"item": "a", "k": 3}, {"item": "z", "k": 2}]


# --- labels -------------------------------------------------------------------------------

def test_labels_names_ids_parts_and_nested_entries():
    env = _env(_agg([_ok(0, "doc_001", b="x"), _ok(1, "doc_003#2", b="y")], [_fail(2, "doc_002")]))
    steps = _steps({"s": env}, _Lane())
    assert [r["item"] for r in steps["s"].rows] == ["job/a.txt", "job/c.md#2", "job/b.txt"]
    assert steps["s"].identities == ["doc_001", "doc_003#2", "doc_002"]
    assert "fields: item (file name), b, error" in rm.step_lines(steps)
    ids = _steps({"s": env}, _Lane("ids"))
    assert [r["item"] for r in ids["s"].rows] == ["doc_001", "doc_003#2", "doc_002"]
    assert "fields: item (document id), b, error" in rm.step_lines(ids)
    nested = _env(_agg([{"index": 0, "item": {"index": 4, "item": "doc_002", "output": {}},
                         "output": {"b": "z"}}]))
    assert _steps({"r": nested}, _Lane())["r"].rows == [{"item": "job/b.txt", "b": "z"}]


def test_a_document_id_never_matches_a_part_of_a_multi_part_document():
    env = _env(_agg([_ok(0, "doc_003#1", b="low"), _ok(1, "doc_001", b="high")],
                    [_fail(2, "doc_003#2")]))
    steps = _steps({"s": env}, _Lane())
    model = cc.ItemModel.build(steps, {"s": env})
    assert model.match("doc_003") is None and model.match("job/c.md") is None
    assert model.match("job/c.md#2") == "doc_003#2" and model.match("c.md#2") == "doc_003#2"
    assert model.match("doc_001") == "doc_001" and model.match("a.txt") == "doc_001"


# --- C1 placements -------------------------------------------------------------------------

def _small():
    return _one([_ok(0, "a", bucket="x"), _ok(1, "b", bucket="y")])


def test_a_marker_as_a_key_value_gives_valid_json():
    out, placed = rm.place('```json\n{"results": @@ROWS:s1@@}\n```', _small())
    body = out.split("```json\n")[1].split("\n```")[0]
    assert json.loads(body) == {"results": [{"item": "a", "bucket": "x"}, {"item": "b", "bucket": "y"}]}
    assert placed == {"s1": 1}


def test_a_marker_as_a_quoted_value_drops_the_quotes():
    out, _ = rm.place('{"results": "@@ROWS:s1 | file=item@@"}', _small())
    assert json.loads(out) == {"results": [{"file": "a"}, {"file": "b"}]}
    out, _ = rm.place('{"note": "rows: @@ROWS:s1 | file=item@@ end"}', _small())
    assert json.loads(out) == {"note": 'rows: [{"file": "a"}, {"file": "b"}] end'}


def test_a_list_holding_only_the_marker_is_absorbed():
    out, _ = rm.place("```json\n[\n  @@ROWS:s1@@\n]\n```", _small())
    assert out == '```json\n[\n{"item": "a", "bucket": "x"},\n{"item": "b", "bucket": "y"}\n]\n```'
    out, _ = rm.place('{"all": [ @@ROWS:s1@@ ]}', _small())
    assert json.loads(out) == {"all": [{"item": "a", "bucket": "x"}, {"item": "b", "bucket": "y"}]}


def test_object_answer_list_with_only_the_marker_becomes_the_rows():
    out, _ = rm.place({"rows": ["@@ROWS:s1 | file=item@@"]}, _small())
    assert out == {"rows": [{"file": "a"}, {"file": "b"}]}


@pytest.mark.parametrize("value, nested", [
    ('```json\n[\n {"file": "a"},\n @@ROWS:s1@@\n]\n```', True),           # job A's shape
    ('[ @@ROWS:s1@@ ]', False),
    ('{"results": @@ROWS:s1@@}', False),
    ('See [the list] below.\n@@ROWS:s1@@', False),                        # closed prose brackets
    ('Read [1] and [notes](http://x).\n@@ROWS:s1@@', False),
    ('| item | bucket |\n| --- | --- |\n| a | x |\n@@ROWS_TABLE:s1@@', True),
    ({"rows": [{"file": "a"}, "@@ROWS:s1@@"]}, True),
    ({"rows": ["@@ROWS:s1@@"]}, False),
])
def test_c1_nested_or_not(value, nested):
    found = cc.nested_markers(value, rm.Resolver(_small()))
    assert bool(found) is nested


def test_c1_sentence():
    problems = cc.check('[{"item": "a"}, @@ROWS:s1@@]', _small(), {}, None, [], [], ["s1"])
    assert problems == [("BRT-ORC-038", (
        "@@ROWS:s1@@ sits inside a list or table you wrote that holds other rows. Put the "
        "marker on its own line, outside any brackets, and do not type its rows yourself."))]


# --- OUTPUT rendering -----------------------------------------------------------------------

TEXT = "line one \"q\"\nline two " + "w " * 1100


def _out():
    results = {"n1": _env({"files": TEXT}, "answerer")}
    return {}, rm.long_outputs(results, {})


def test_output_on_its_own_lines():
    steps, outputs = _out()
    out, placed = rm.place("Files:\n`@@OUTPUT:n1@@`\nEnd.", steps, outputs=outputs)
    assert out == "Files:\n\n" + TEXT + "\n\nEnd."
    assert placed == {"n1": 1}


def test_output_inside_a_fence_closes_and_reopens_it():
    steps, outputs = _out()
    out, _ = rm.place("```text\nhead\n@@OUTPUT:n1@@\ntail\n```", steps, outputs=outputs)
    assert out == "```text\nhead\n```\n\n" + TEXT + "\n\n```text\ntail\n```"


def test_output_as_a_bare_json_value_and_inside_a_quoted_string():
    steps, outputs = _out()
    out, _ = rm.place('{"code": @@OUTPUT:n1@@}', steps, outputs=outputs)
    assert json.loads(out) == {"code": TEXT}
    out, _ = rm.place('```json\n{"code": "@@OUTPUT:n1@@"}\n```', steps, outputs=outputs)
    assert json.loads(out.split("```json\n")[1].split("\n```")[0]) == {"code": TEXT}
    out, _ = rm.place('{"code": "see @@OUTPUT:n1@@"}', steps, outputs=outputs)
    assert json.loads(out) == {"code": "see " + TEXT}


def test_output_leaf_in_an_object_answer():
    steps, outputs = _out()
    out, _ = rm.place({"files": "@@OUTPUT:n1@@", "note": "x @@OUTPUT:n1@@"}, steps, outputs=outputs)
    assert out == {"files": TEXT, "note": "x " + TEXT}


def test_long_outputs_and_their_lines():
    results = {"one": _env({"a": "x" * 2500, "short": "y"}, "answerer"),
               "two": _env({"a": "x" * 2000, "b": "z" * 3000}, "answerer"),
               "small": _env({"a": "x" * 1999}, "answerer"),
               "child": _env({"response": "r" * 2200, "child_run_id": "c"}, "orchestrate")}
    outputs = rm.long_outputs(results, {})
    assert list(outputs) == ["one", "two", "child"]
    assert rm.output_lines(outputs) == (
        "- one: 2,500 characters (field a)\n"
        "- two: 2,000 characters (field a; write @@OUTPUT:two | a@@)\n"
        "- two: 3,000 characters (field b; write @@OUTPUT:two | b@@)\n"
        "- child: 2,200 characters (field response)")
    assert rm.marker_problems("@@OUTPUT:two@@", {}, outputs, [])[0][0] == "BRT-ORC-037"
    out, _ = rm.place("@@OUTPUT:two | b@@", {}, outputs=outputs)
    assert out == "z" * 3000


# --- C3 --------------------------------------------------------------------------------------

@pytest.mark.parametrize("reply, fires", [
    ("All files are in the stage result (n1).", True),
    ("See the step result above.", True),
    ("Copy it from `n1`.", True),
    ("The output of step n1 holds the code.", True),
    ("A short summary of what was built, with no pointer.", False),        # the recall limit
    ("Here it is in full: " + TEXT + " (n1)", False),                       # present, not dropped
    ("Files:\n@@OUTPUT:n1@@\nas in the stage result.", False),             # placed
])
def test_c3(reply, fires):
    steps, outputs = _out()
    problems = cc.check(reply, steps, outputs, None, [], [], ["n1"])
    assert (("BRT-ORC-040", cc.S_UNPLACED.format(id="n1")) in problems) is fires


# --- C4 and the item model -------------------------------------------------------------------

def _d_like():
    first = _env(_agg([_ok(0, "doc_001", bucket="low"), _ok(1, "doc_002", bucket="high")],
                      [_fail(2, "doc_003#1"), _fail(3, "doc_003#2")]), scope="raw")
    results = {"classify": first}
    steps = _steps(results, _Lane())
    return steps, results, cc.ItemModel.build(steps, results)


@pytest.mark.parametrize("reply, hits", [
    ('[{"file": "a.txt", "risk": "low"}, {"file": "c.md#1", "risk": "high"}]', 1),     # (a)
    ("| file | risk |\n| --- | --- |\n| c.md#2 | high |\n| a.txt | low |", 1),          # (b)
    ("- c.md#1: low", 1),                                                             # (c)
    ('{"high": ["c.md#1", "c.md#2"]}', 2),                                        # (d)
    ("- c.md#1: needs a human to label it low, medium or high", 0),
    ("- c.md#1: no result", 0),
    ('[{"file": "c.md#1", "risk": "BRT-ANS-005"}]', 0),
    ('[{"file": "c.md#1", "risk": "pending review"}]', 0),                           # not stored vocabulary
    ('[{"file": "b.txt", "risk": "low"}]', 0),                                       # altered: not caught
])
def test_c4_case_1(reply, hits):
    steps, results, model = _d_like()
    case1, case2 = cc.invented_values(reply, model, [])
    assert len(case1) == hits and case2 == {}


def test_c4_case_1_sentence_carries_the_join_hint():
    steps, results, model = _d_like()
    problems = cc.check('[{"file": "c.md#1", "risk": "low"}, {"file": "c.md#2", "risk": "low"}]',
                        steps, {}, model, [], [], ["classify"])
    assert problems == [("BRT-ORC-041", (
        "You gave values to 2 items that no step gave a value for that field (for example "
        "job/c.md#1, job/c.md#2). Do not give them values: in placed rows they show their error "
        "or stay empty, and in your own text say they have no result or no value. To show the "
        "stored values, place the rows instead of typing them: "
        "@@ROWS:classify | <name>=<field>, <field>@@."))]


def test_c4_answerer_pairing_exempts_a_value():
    steps, results, _ = _d_like()
    results = dict(results, fix={})
    results["fix"] = _env({"items": [{"file": "doc_003#1", "risk": "high"}]}, "answerer")
    model = cc.ItemModel.build(steps, results)
    case1, _ = cc.invented_values('[{"file": "c.md#1", "risk": "high"}]', model, [])
    assert case1 == []
    assert model.no_result() == ["doc_003#2"]


def test_c4_case_2_asked_field():
    steps, results, model = _d_like()
    asked = [{"step": "classify", "skill": "k", "field": "urgency"}]
    reply = ('[{"file": "a.txt", "category": "low", "urgency": 3},'
             ' {"file": "b.txt", "urgency": "n/a"}]')
    case1, case2 = cc.invented_values(reply, model, asked)
    assert case2 == {"urgency": ["doc_001"]} and case1 == []
    problems = cc.check(reply, steps, {}, model, asked, [], ["classify"])
    assert problems == [("BRT-ORC-041", (
        "You gave values for urgency to 1 items (for example job/a.txt), but no step produced "
        "urgency. Leave the field out, or tell the reader it was not produced."))]


def test_no_result_and_no_value_counts():
    first = _env(_agg([_ok(0, "a", x="1", y=None, z=""), _ok(1, "b", x=None, y=None, z="")],
                      [_fail(2, "c"), _fail(3, "d")]))
    second = _env(_agg([_ok(0, "c", x="2", y=None, z="")], [_fail(1, "d")]))
    results = {"f": first, "g": second}
    steps = _steps(results)
    model = cc.ItemModel.build(steps, results)
    assert model.no_result() == ["d"]                       # c was resolved by g
    assert model.no_value_counts() == [("x", 1)]            # y and z hold no value anywhere
    block = rm.block(steps, {}, len(model.no_result()), model.no_value_counts(), [])
    assert ("Items with no result (a step failed them and no other step gave them a value): 1. "
            "Never give them a value: say they have no result.") in block
    assert ("Rows with no value (a step returned the item but left the field empty): x 1. "
            "Leave those fields empty; do not supply a value yourself.") in block


# --- review round 1 (lead's list) -------------------------------------------------------------

@pytest.mark.parametrize("values", [[3, 1, 2], [{"name": "z"}, {"name": "y"}]])
def test_a_join_with_a_stored_order_step_is_037(values):
    steps = _steps({"a": _env(_agg([_ok(0, "x", k=1)])),
                    "p": _env({"result": values}, "state_op")})
    assert steps["p"].order == rm.STORED_ORDER
    sentence = ("The marker for a + p joins p, whose rows are in stored order with no item to "
                "join on: use @@ROWS:p@@ for it on its own.")
    for marker in ("@@ROWS:a + p@@", "@@ROWS:a + p | item, k@@", "@@ROWS:p + a | name@@"):
        problems = rm.marker_problems(marker, steps, {}, [])
        assert [c for c, _ in problems] == ["BRT-ORC-037"], marker
    assert rm.marker_problems("@@ROWS:a + p@@", steps, {}, []) == [("BRT-ORC-037", sentence)]
    out, _ = rm.place("@@ROWS:a + p@@", steps)           # never placed, never crashes
    assert out == "@@ROWS:a + p@@"


def test_scan_context_comes_from_the_original_reply():
    n1 = 'print("[")\n' + "a " * 1100
    n2 = "second text " * 200
    results = {"n1": _env({"code": n1}, "answerer"), "n2": _env({"text": n2}, "answerer"),
               "s1": _env(_agg([_ok(0, "a", bucket="x")]))}
    steps = _steps(results)
    outputs = rm.long_outputs(results, steps)
    reply = "Code:\n@@OUTPUT:n1@@\nText:\n@@OUTPUT:n2@@\nRows:\n@@ROWS:s1@@"
    out, placed = rm.place(reply, steps, outputs=outputs)
    assert out == ("Code:\n\n" + n1 + "\n\nText:\n\n" + n2 + "\n\nRows:\n"
                   '```json\n[\n{"item": "a", "bucket": "x"}\n]\n```')
    assert placed == {"n1": 1, "n2": 1, "s1": 1}


@pytest.mark.parametrize("reply", ["@@OUTPUT:n1@@\n\nOutput notes: none.",
                                   "@@ROWS:s@@\nRows above.", "@@ROWS:s@@ output above"])
def test_loose_skips_the_closing_at_signs_of_a_strict_marker(reply):
    results = {"n1": _env({"text": "t " * 1100}, "answerer"), "s": _env(_agg([_ok(0, "a", b=1)]))}
    steps = _steps(results)
    assert rm.marker_problems(reply, steps, rm.long_outputs(results, steps), []) == []


ASKED_MIXED = [
    {"step": "classify", "skill": "classify_into_buckets", "field": "urgency", "code": "BRT-ORC-035"},
    {"step": "classify", "skill": "", "field": "colour", "code": "BRT-ORC-036"},
    {"step": "classify", "skill": "classify_into_buckets", "field": "instruction",
     "code": "BRT-ORC-036"},
]


def test_036_entries_have_their_own_key_lines():
    steps, _, model = _d_like()
    block = rm.block(steps, {}, 0, [], ASKED_MIXED)
    assert ("- classify (skill classify_into_buckets): urgency\n" + rm.ASKED_TAIL) in block
    assert block.endswith(
        "The plan gave colour to classify, which does not take it; it was not used.\n"
        "The plan gave instruction to classify, which does not take it; it was not used.")
    only_036 = rm.block(steps, {}, 0, [], ASKED_MIXED[1:])
    assert rm.ASKED_HEAD not in only_036 and rm.ASKED_TAIL not in only_036


def test_the_engine_tells_035_from_036_entries(monkeypatch):
    from src.orchestrator import engine
    run_id = uuid4()
    entries = [{k: v for k, v in e.items() if k != "code"} for e in ASKED_MIXED]
    monkeypatch.setitem(engine._NOT_PRODUCED, str(run_id), entries)
    monkeypatch.setitem(engine._NOT_PRODUCED_CODES, str(run_id),
                        [e["code"] for e in ASKED_MIXED])
    assert engine.asked_but_not_produced(run_id) == entries          # three keys, as 32c reads
    steps, results, _ = _d_like()
    ctx = engine._RowContext.for_run(results, _Lane(), "", None, run_id)
    assert "- classify (skill classify_into_buckets): urgency\n" in ctx.block
    assert "The plan gave colour to classify, which does not take it; it was not used." in ctx.block


def test_c4_case_2_reads_035_entries_only():
    steps, _, model = _d_like()
    reply = '[{"file": "a.txt", "colour": "red", "instruction": "do it", "urgency": 4}]'
    _, case2 = cc.invented_values(reply, model, ASKED_MIXED)
    assert case2 == {"urgency": ["doc_001"]}


def test_long_outputs_withhold_an_unguarded_raw_step():
    class RawLane(_Lane):
        keeps_raw_local = True
    guarded = _env({"report": "g " * 1100}, "answerer", scope="raw")
    guarded.lane_guarded = True
    unguarded = _env({"report": "u " * 1100}, "answerer", scope="raw")
    plain = _env({"report": "p " * 1100}, "answerer")
    found = rm.long_outputs({"g": guarded, "u": unguarded, "p": plain}, {}, RawLane())
    assert list(found) == ["g", "p"]
    assert list(rm.long_outputs({"u": unguarded}, {}, _Lane())) == ["u"]    # by_reference: no withhold


def test_the_gate_gives_no_block_with_nothing_to_list():
    assert rm.block({}, {}, 0, [], []) is None
    assert rm.block({}, {}, 0, [], [{"step": "s", "skill": "k", "field": "f"}]).startswith(rm.INTRO)
