"""Planner ruling 32e: replay of the four stored round-2 compose inputs and
of stage-1 job A (docs/design/compose_placement.md, section 2.7).

The harness calls ``engine._handle_compose_result`` with envelopes from a
fixture (``tests/fixtures/round2_compose_{a,b,c,d}.json``, written by
``extract_round2_compose.py``), a LaneContext built from the fixture's
manifest (documents with placeholder text), scripted re-asks, and
``asked_but_not_produced`` patched to the fixture's list. Scoring uses the
bench grader as it stands. C2 (BRT-ORC-039) was dropped by the planner: D is
caught by C4 (BRT-ORC-041) alone.
"""
import json
import re
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from bench import checks, facts
from bench.run_bench import score_classification
from src.envelopes.schema import InboundEnvelope
from src.lane.context import LaneContext, lane_scope
from src.lane.documents import Document, DocPart, LoadedDocuments
from src.lane.settings import LaneSettings
from src.orchestrator import engine

ROOT = Path(__file__).resolve().parents[2]
FIX = ROOT / "tests" / "fixtures"
ANSWERS = ROOT / "bench" / "data" / "answers"

# The block's fixed lines, as the note writes them (golden text, 2.3).
INTRO = (
    "The reader sees only your answer. They cannot see any step, step id or step result, so "
    "never refer them to one: everything they need must be in your answer, written by you or "
    "placed by the code.\n"
    "The code can place stored results for you, exactly as stored. Do not retype rows or long "
    "outputs yourself; naming a few items in your own text is fine."
)
ROWS_BULLET = (
    "- Rows: write @@ROWS:<step id>@@ where a step's rows belong (a JSON array, one row per "
    "line), or @@ROWS_TABLE:<step id>@@ for a Markdown table. Put the marker on its own line, "
    "outside any brackets you write: the code writes the brackets. To choose fields, their order "
    "and their names, add them after a bar: @@ROWS:<step id> | <name>=<field>, <field>@@ (for "
    "example @@ROWS:s1 | file=item, label=bucket@@). To join steps on the item, list them with +: "
    "@@ROWS:<step id> + <step id> | ...@@; when both give a field, the later step's value is "
    "used. Use only the fields listed for those steps. An item with no result keeps its error "
    "and gets no other value."
)
OUTPUT_BULLET = ("- Whole output: write @@OUTPUT:<step id>@@ where a step's whole output "
                 "belongs; the code writes it in word for word.")
REPAIR_HEAD = "Mode: compose. Your last reply cannot be used as it is:"
REPAIR_TAIL = 'Fix these and reply with {"response": "<the final answer>"} only.'

GOLDEN_A = "\n".join([INTRO, ROWS_BULLET,
    "Steps with rows:",
    "- classify_emails: 200 rows, input order, 4 failed; fields: item (file name), bucket, "
    "confidence, defer_reason, error",
    "Items with no result (a step failed them and no other step gave them a value): 0. Never "
    "give them a value: say they have no result.",
    "Asked for but not produced (the plan asked a step for these fields and the step does not "
    "return them):",
    "- classify_emails (skill classify_into_buckets): file, category, urgency",
    "Do not supply values for these fields yourself. You may show the item or a field the step "
    "did return under one of these names in a rows marker (for example category=bucket); "
    "otherwise leave the field out and tell the reader it was not produced."])
GOLDEN_B = "\n".join([INTRO, OUTPUT_BULLET, "Steps with a long output:",
                      "- n1: 10,435 characters (field files)"])
_Q = ("tamarack_pilot_end_date, kessler_falls_loading_bays, group_compliance_contact, "
      "project_osprey_approved_budget, audit_logs_retention_days, refrigeration_units_vendor, "
      "emergency_shutdown_codeword, pell_harbour_crane_inspection_frequency")
GOLDEN_C = "\n".join([INTRO, ROWS_BULLET,
    "Steps with rows:",
    f"- extract_q1_to_q8: 83 rows, input order, 8 failed; fields: item (file name), {_Q}, error",
    "- extract_contradictions: 83 rows, input order, 2 failed; fields: item (file name), "
    "any_contradictions_or_inconsistencies, contradiction_topics, conflicting_values_quoted, error",
    "Items with no result (a step failed them and no other step gave them a value): 2. Never "
    "give them a value: say they have no result.",
    "Rows with no value (a step returned the item but left the field empty): "
    + ", ".join(f"{f} 74" for f in _Q.split(", "))
    + ", any_contradictions_or_inconsistencies 23, contradiction_topics 24, "
      "conflicting_values_quoted 24. Leave those fields empty; do not supply a value yourself."])
GOLDEN_D = "\n".join([INTRO, ROWS_BULLET,
    "Steps with rows:",
    "- classify_notes: 60 rows, input order, 31 failed; fields: item (file name), bucket, "
    "confidence, defer_reason, error",
    "- retry_failed_notes: 29 rows, input order, 0 failed; fields: item (file name), bucket, "
    "confidence, defer_reason",
    "Items with no result (a step failed them and no other step gave them a value): 31. Never "
    "give them a value: say they have no result."])


def _fixture(job):
    return json.loads((FIX / f"round2_compose_{job}.json").read_text(encoding="utf-8"))


def _key(job):
    return json.loads((ANSWERS / f"job_{job}_answers.json").read_text(encoding="utf-8"))


def _lane(fx, manifest=None):
    if fx["lane"]["mode"] == "off":
        return None
    docs = [Document(d["id"], d["name"], "txt", 1, "placeholder",
                     [DocPart(p, d["id"], f"placeholder text of {p}") for p in d["parts"]])
            for d in fx["lane"]["documents"]]
    return LaneContext(settings=LaneSettings(mode=fx["lane"]["mode"],
                                             manifest=manifest or fx["lane"]["manifest"]),
                       docs=LoadedDocuments(docs, []), db_path=":memory:")


def _results(fx):
    out = {}
    for sid, s in fx["steps"].items():
        env = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status=s["status"],
                              output=s["output"])
        env._node_type = s["node_type"]
        env._data_scope = s["data_scope"]
        out[sid] = env
    return out


async def _replay(fx, replies, monkeypatch, results=None, lane="fixture", asked=None):
    events, kinds = [], []

    async def write_event(event_type, run_id=None, decision=None, db_path=None, **_):
        events.append((event_type, decision or {}))

    async def mark(run_id, kind, db_path):
        kinds.append(kind)

    monkeypatch.setattr(engine, "write_event", write_event)
    monkeypatch.setattr(engine, "_mark_answer_kind", mark)
    listed = fx.get("asked_but_not_produced", []) if asked is None else asked
    monkeypatch.setattr(engine, "asked_but_not_produced", lambda run_id: list(listed))
    lane = _lane(fx) if lane == "fixture" else lane
    results = results or _results(fx)
    run_id = uuid4()
    sent = []
    with lane_scope(lane):
        rows = engine._RowContext.for_run(results, lane, fx.get("instruction", ""), None, run_id)
        queue = list(replies[1:])

        async def reask(message=engine._COMPOSE_REASK):
            sent.append(message + ("\n" + rows.block if rows.block else ""))
            assert queue, "an unscripted compose call"
            return queue.pop(0)

        answer = await engine._handle_compose_result(
            replies[0], results, run_id, ":memory:", reask=reask,
            rows=rows if rows.active else None, reask_invalid=reask if rows.active else None)
    codes = [d.get("code") for _, d in events if d.get("code")]
    warning = [d for t, d in events if t == "error" and d.get("severity") == "warning"]
    return SimpleNamespace(answer=answer, sent=sent, codes=codes, warning=warning,
                           kind=kinds[-1] if kinds else None, rows=rows, results=results)


def _repair(*sentences):
    return "\n".join([REPAIR_HEAD] + [f"- {s}" for s in sentences] + [REPAIR_TAIL])


def _rows_of(answer):
    blocks = re.findall(r"```json\n(.*?)\n```", answer, re.DOTALL)
    return [json.loads(b) for b in blocks]


# --- job A (round 2) --------------------------------------------------------------------------

A_SPEC = "@@ROWS:classify_emails | file=item, category=bucket@@"
A_GOOD = {"response": "Here are all 200 emails. No step produced an urgency, so none is given."
                      "\n\n" + A_SPEC}
A_NESTED = ("@@ROWS:classify_emails@@ sits inside a list or table you wrote that holds other "
            "rows. Put the marker on its own line, outside any brackets, and do not type its rows "
            "yourself.")
A_URGENCY = ("You gave values for urgency to 9 items (for example job_a/email_001.txt, "
             "job_a/email_002.txt, job_a/email_003.txt), but no step produced urgency. Leave the "
             "field out, or tell the reader it was not produced.")


def _usable_a(answer):
    score = score_classification(answer, _key("a"), ["category", "urgency"], "file")
    assert score["items_answered"] == 200
    assert score["per_field_accuracy"] == {"category": 0.925, "urgency": 0.0}
    assert "@@" not in answer
    rows = max(_rows_of(answer), key=len)
    assert len(rows) == 200 and all("file" in r for r in rows)
    assert not any("urgency" in r for r in rows)
    return rows


async def test_a1_block_golden_text(monkeypatch):
    fx = _fixture("a")
    run = await _replay(fx, [A_GOOD], monkeypatch)
    assert run.rows.block == GOLDEN_A


async def test_a2_recorded_reply_gives_038_and_041_in_one_exact_message(monkeypatch):
    fx = _fixture("a")
    run = await _replay(fx, [fx["recorded_replies"][0], A_GOOD], monkeypatch)
    assert run.sent == [_repair(A_NESTED, A_URGENCY) + "\n" + GOLDEN_A]
    assert "BRT-ORC-039" not in run.codes
    reask = [c for c in run.codes if c]
    assert reask[0] == "BRT-ORC-038"


async def test_a3_compliant_reask_is_composed_and_usable(monkeypatch):
    fx = _fixture("a")
    run = await _replay(fx, [fx["recorded_replies"][0], A_GOOD], monkeypatch)
    assert run.kind == "composed"
    rows = _usable_a(run.answer)
    assert rows[0] == {"file": "job_a/email_001.txt", "category": "billing"}
    assert rows[3] == {"file": "job_a/email_004.txt", "error": "BRT-ANS-005"}


async def test_a4_nested_again_with_the_spec_is_salvaged_s2(monkeypatch):
    fx = _fixture("a")
    recorded = fx["recorded_replies"][0]["response"]
    again = {"response": recorded.replace("@@ROWS:classify_emails@@", A_SPEC)}
    run = await _replay(fx, [fx["recorded_replies"][0], again], monkeypatch)
    assert run.kind == "salvaged"
    assert run.answer.startswith(engine.SALVAGE_NOTE)
    assert "email_010.txt\", \"category\": \"billing\", \"urgency\"" not in run.answer
    assert "## classify_emails\n```json" in run.answer
    _usable_a(run.answer)
    assert run.warning and run.warning[0]["code"] == "BRT-ORC-038"
    assert run.warning[0]["codes"] == ["BRT-ORC-038", "BRT-ORC-041"]


async def test_a5_unknown_field_gives_037_then_composed(monkeypatch):
    fx = _fixture("a")
    first = {"response": "@@ROWS:classify_emails | file=item, category=bucket, urgency@@"}
    run = await _replay(fx, [first, A_GOOD], monkeypatch)
    assert run.sent == [_repair(
        "The marker for classify_emails names fields that step does not have: urgency. Its "
        "fields are: item, bucket, confidence, defer_reason, error. No step produced the others; "
        "do not give values for them.") + "\n" + GOLDEN_A]
    assert run.kind == "composed"
    _usable_a(run.answer)


async def test_a6_nested_twice_without_a_spec_salvages_with_names(monkeypatch):
    fx = _fixture("a")
    rec = fx["recorded_replies"][0]
    run = await _replay(fx, [rec, rec], monkeypatch)
    assert run.kind == "salvaged"
    rows = [r for block in _rows_of(run.answer) for r in block]
    assert len(rows) == 200 and rows[0]["item"] == "job_a/email_001.txt"
    # The grader reads "file", so it scores none of them (a stated limit).
    assert score_classification(run.answer, _key("a"), ["category", "urgency"],
                                "file")["items_answered"] == 0


# --- job B (round 2) --------------------------------------------------------------------------

B_SIX = ("files_present", "sql_executes", "python_parses", "openapi_loads", "spec_tables",
         "spec_openapi_paths")


def _usable_b(answer, stored):
    result = checks.run_checks(answer, with_docker=False)
    assert all(result["checks"][c]["status"] == "pass" for c in B_SIX), result["checks"]
    assert stored in answer


async def test_b1_block_golden_text(monkeypatch):
    fx = _fixture("b")
    run = await _replay(fx, [{"response": "x"}], monkeypatch)
    assert run.rows.block == GOLDEN_B


async def test_b2_recorded_reply_gives_040(monkeypatch):
    fx = _fixture("b")
    run = await _replay(fx, [fx["recorded_replies"][0], {"response": "@@OUTPUT:n1@@"}], monkeypatch)
    assert run.sent == [_repair(
        "Your answer points the reader to a step result they cannot see. Put @@OUTPUT:n1@@ where "
        "that step's output belongs instead of pointing to it.") + "\n" + GOLDEN_B]


async def test_b3_output_marker_is_composed_and_usable(monkeypatch):
    fx = _fixture("b")
    stored = fx["steps"]["n1"]["output"]["files"]
    run = await _replay(fx, [{"response": "The four files follow.\n\n@@OUTPUT:n1@@"}], monkeypatch)
    assert run.kind == "composed" and run.sent == []
    assert run.answer == "The four files follow.\n\n" + stored
    _usable_b(run.answer, stored)


async def test_b4_recorded_reply_twice_is_salvaged_s1_verbatim(monkeypatch):
    fx = _fixture("b")
    stored = fx["steps"]["n1"]["output"]["files"]
    rec = fx["recorded_replies"][0]
    run = await _replay(fx, [rec, rec], monkeypatch)
    assert run.kind == "salvaged"
    assert run.answer == engine.SALVAGE_NOTE + "\n\n## n1\n" + stored
    _usable_b(run.answer, stored)


async def test_b5_summary_without_a_pointer_is_composed(monkeypatch):
    fx = _fixture("b")
    summary = {"response": "The notes service was built: a schema, an app, an API description "
                           "and tests."}
    run = await _replay(fx, [summary], monkeypatch)
    assert run.kind == "composed" and run.sent == []        # the recall limit, pinned


async def test_b6_output_inside_a_quoted_json_value_in_a_fence(monkeypatch):
    fx = _fixture("b")
    stored = fx["steps"]["n1"]["output"]["files"]
    reply = {"response": '```json\n{"files": "@@OUTPUT:n1@@"}\n```'}
    run = await _replay(fx, [reply], monkeypatch)
    body = run.answer.split("```json\n", 1)[1].rsplit("\n```", 1)[0]
    assert json.loads(body) == {"files": stored}


async def test_b7_output_as_an_object_answer_leaf(monkeypatch):
    fx = _fixture("b")
    stored = fx["steps"]["n1"]["output"]["files"]
    run = await _replay(fx, [{"response": {"files": "@@OUTPUT:n1@@"}}], monkeypatch)
    assert json.loads(run.answer) == {"files": stored}


# --- job C (round 2) --------------------------------------------------------------------------

async def test_c1_block_golden_text(monkeypatch):
    fx = _fixture("c")
    run = await _replay(fx, [{"response": "x"}], monkeypatch)
    assert run.rows.block == GOLDEN_C
    assert "82" not in run.rows.block


async def test_c2_empty_reply_gives_029_and_the_recorded_answer_is_unchanged(monkeypatch):
    fx = _fixture("c")
    run = await _replay(fx, fx["recorded_replies"], monkeypatch)
    assert run.codes == ["BRT-ORC-029"]
    assert run.sent == [engine._COMPOSE_REASK + "\n" + GOLDEN_C]
    assert run.answer == fx["recorded_replies"][1]["response"]
    assert run.kind == "composed"
    assert facts.score_facts(run.answer, _key("c"))["facts_found"] == 6


# --- job D (round 2) --------------------------------------------------------------------------

D_JOIN = "@@ROWS:classify_notes + retry_failed_notes | file=item, risk=bucket@@"
D_CASE1 = ("You gave values to 31 items that no step gave a value for that field (for example "
           "job_d/note_04.txt, job_d/note_07.txt, job_d/note_09.txt). Do not give them values: in "
           "placed rows they show their error or stay empty, and in your own text say they have no "
           "result or no value. To show the stored values, place the rows instead of typing them: "
           "@@ROWS:classify_notes + retry_failed_notes | <name>=<field>, <field>@@.")


def _stored_d(fx):
    agg = fx["steps"]["classify_notes"]["output"]
    names = {d["id"]: d["name"] for d in fx["lane"]["documents"]}
    ok = {names[e["item"]]: e["output"]["bucket"] for e in agg["results"]}
    failed = {names[e["item"]] for e in agg["failures"]}
    return ok, failed


def _usable_d(answer, fx):
    score = score_classification(answer, _key("d"), ["risk"], "file")
    assert score["items_answered"] == 60
    assert score["per_field_accuracy"] == {"risk": pytest.approx(25 / 60)}
    ok, failed = _stored_d(fx)
    rows = max(_rows_of(answer), key=len)
    for row in rows:
        if row["file"] in failed:
            assert "risk" not in row and row["error"]
        else:
            assert row["risk"] == ok[row["file"]]


def _typed_d(fx, with_medium=True):
    ok, failed = _stored_d(fx)
    rows = []
    for d in fx["lane"]["documents"]:
        base = d["name"].split("/")[-1]
        if d["name"] in ok:
            rows.append({"file": base, "risk": ok[d["name"]]})
        elif with_medium:
            rows.append({"file": base, "risk": "medium"})
    return "```json\n" + json.dumps(rows, indent=1) + "\n```"


async def test_d1_block_golden_text(monkeypatch):
    fx = _fixture("d")
    run = await _replay(fx, [{"response": "x"}], monkeypatch)
    assert run.rows.block == GOLDEN_D


async def test_d2_recorded_reply_is_caught_by_c4_alone(monkeypatch):
    fx = _fixture("d")
    run = await _replay(fx, [fx["recorded_replies"][0], {"response": D_JOIN}], monkeypatch)
    assert run.sent == [_repair(D_CASE1) + "\n" + GOLDEN_D]
    assert [c for c in run.codes if c.startswith("BRT-ORC-0")] == ["BRT-ORC-041"]


async def test_d3_the_suggested_join_marker_is_composed_and_usable(monkeypatch):
    fx = _fixture("d")
    run = await _replay(fx, [fx["recorded_replies"][0], {"response": D_JOIN}], monkeypatch)
    assert run.kind == "composed"
    _usable_d(run.answer, fx)


async def test_d4_typed_again_plus_the_marker_is_salvaged_s2(monkeypatch):
    fx = _fixture("d")
    again = {"response": "Labels:\n" + _typed_d(fx) + "\n\n" + D_JOIN}
    run = await _replay(fx, [fx["recorded_replies"][0], again], monkeypatch)
    assert run.kind == "salvaged" and run.warning[0]["codes"] == ["BRT-ORC-041"]
    _usable_d(run.answer, fx)


async def test_d5_faithful_retype_without_invented_values_is_composed(monkeypatch):
    fx = _fixture("d")
    faithful = {"response": _typed_d(fx, with_medium=False)}
    run = await _replay(fx, [fx["recorded_replies"][0], faithful], monkeypatch)
    assert run.kind == "composed"
    assert run.answer == faithful["response"]


async def test_d6_typed_twice_with_medium_salvages_in_the_plain_form(monkeypatch):
    fx = _fixture("d")
    rec = fx["recorded_replies"][0]
    run = await _replay(fx, [rec, rec], monkeypatch)
    assert run.kind == "salvaged"
    ok, failed = _stored_d(fx)
    rows = [r for block in _rows_of(run.answer) for r in block]
    for row in rows:
        assert set(row) <= {"item", "bucket", "confidence", "defer_reason", "error"}
        if row["item"] in failed:
            assert "bucket" not in row
    assert "medium" not in json.dumps([r for r in rows if r["item"] in failed])
    assert score_classification(run.answer, _key("d"), ["risk"], "file")["items_answered"] == 0


async def test_d7_after_32d_the_retry_resolves_four(monkeypatch):
    fx = _fixture("d")
    agg = json.loads(json.dumps(fx["steps"]["classify_notes"]["output"]))
    retry = {"results": [], "failures": [], "total": 31, "success_count": 4, "failure_count": 27}
    labels = ["low", "high", "medium", "low"]
    for k, entry in enumerate(agg["failures"]):
        if k < 4:
            retry["results"].append({"index": k, "item": entry["item"], "original_index": entry["index"],
                                     "output": {"bucket": labels[k], "confidence": 0.8,
                                                "defer_reason": ""}})
        else:
            retry["failures"].append({"index": k, "item": entry["item"],
                                      "original_index": entry["index"],
                                      "reason": {"code": "BRT-ANS-005", "message": "m"}})
    fx = dict(fx, steps={"classify_notes": fx["steps"]["classify_notes"],
                         "retry_failed": {"node_type": "apply_to_each", "data_scope": "raw",
                                          "status": "resolved", "output": retry}})
    line = {"response": "Rechecked: note_04.txt low, note_07.txt high, note_09.txt medium."}
    run = await _replay(fx, [line], monkeypatch)
    assert ("Items with no result (a step failed them and no other step gave them a value): 27."
            in run.rows.block)
    assert run.kind == "composed" and run.sent == []


# --- stage-1 job A (round 1) -------------------------------------------------------------------

def _round1():
    rec = json.loads((FIX / "stage1_job_a_replay.json").read_text(encoding="utf-8"))
    docs = sorted(set(rec["items"]["extract_urgency"]) | set(rec["failures"]["classify_emails"]))
    fx = {"instruction": rec["instruction"], "asked_but_not_produced": [],
          "lane": {"mode": "by_reference", "manifest": "names",
                   "documents": [{"id": d, "name": f"job_a/email_{d[4:]}.txt", "parts": [d]}
                                 for d in docs]},
          "steps": {}}
    for step in ("classify_emails", "extract_urgency"):
        ok, bad = rec["items"][step], rec["failures"].get(step, {})
        results = [{"index": i, "item": d, "output": ok[d]} for i, d in enumerate(docs) if d in ok]
        failures = [{"index": i, "item": d, "reason": {"code": bad[d], "message": "m"}}
                    for i, d in enumerate(docs) if d in bad]
        fx["steps"][step] = {"node_type": "apply_to_each", "data_scope": "raw", "status": "resolved",
                             "output": {"results": results, "failures": failures,
                                        "total": len(docs), "success_count": len(results),
                                        "failure_count": len(failures)}}
    return fx, rec, docs


def _typed_join(rec, docs):
    return [{"file": f"email_{d[4:]}.txt", **{k: v for k, v in rec["items"]["extract_urgency"][d].items()
                                               if k in ("category", "urgency")}} for d in docs]


async def test_r1_1_typed_join_fires_nothing(monkeypatch):
    fx, rec, docs = _round1()
    reply = {"response": "```json\n" + json.dumps(_typed_join(rec, docs)) + "\n```"}
    run = await _replay(fx, [reply], monkeypatch)
    assert run.kind == "composed" and run.sent == []


async def test_r1_2_join_marker_the_later_step_wins(monkeypatch):
    fx, rec, docs = _round1()
    marker = "@@ROWS:classify_emails + extract_urgency | file=item, category, urgency@@"
    run = await _replay(fx, [{"response": marker}], monkeypatch)
    rows = _rows_of(run.answer)[0]
    assert [r["file"] for r in rows] == [f"job_a/email_{d[4:]}.txt" for d in docs]
    for d, row in zip(docs, rows):
        later = rec["items"]["extract_urgency"][d]
        assert row == {"file": f"job_a/email_{d[4:]}.txt", "category": later["category"],
                       "urgency": later["urgency"]}
    assert len(rec["failures"]["classify_emails"]) == 11


async def test_r1_3_grouped_reply_fires_nothing(monkeypatch):
    fx, rec, docs = _round1()
    groups: dict = {}
    for row in _typed_join(rec, docs):
        groups.setdefault(row["category"], []).append(row["file"])
    run = await _replay(fx, [{"response": json.dumps(groups)}], monkeypatch)
    assert run.kind == "composed" and run.sent == []


async def test_r1_4_csv_reply_fires_nothing(monkeypatch):
    fx, rec, docs = _round1()
    csv = "file,category,urgency\n" + "\n".join(
        f"{r['file']},{r['category']},{r['urgency']}" for r in _typed_join(rec, docs))
    run = await _replay(fx, [{"response": csv}], monkeypatch)
    assert run.kind == "composed" and run.answer == csv


async def test_r1_5_a_join_field_carries_its_source_steps_error_code(monkeypatch):
    fx, rec, docs = _round1()
    marker = "@@ROWS:classify_emails + extract_urgency | file=item, bucket, urgency@@"
    run = await _replay(fx, [{"response": marker}], monkeypatch)
    rows = {r["file"]: r for r in _rows_of(run.answer)[0]}
    for d, code in rec["failures"]["classify_emails"].items():
        row = rows[f"job_a/email_{d[4:]}.txt"]
        assert row == {"file": f"job_a/email_{d[4:]}.txt", "bucket": code,
                       "urgency": rec["items"]["extract_urgency"][d]["urgency"]}


# --- ids mode, action fail ---------------------------------------------------------------------

async def test_ids_mode_shows_no_name_in_block_reask_or_rows(monkeypatch):
    fx = _fixture("d")
    lane = _lane(fx, manifest="ids")
    # Compose was shown document ids only, so a typed reply names doc ids.
    ok, failed = _stored_d(fx)
    names = {d["name"]: d["id"] for d in fx["lane"]["documents"]}
    typed = [{"file": names[n], "risk": ok.get(n, "medium")} for n in names]
    first = {"response": "```json\n" + json.dumps(typed) + "\n```"}
    run = await _replay(fx, [first, {"response": D_JOIN}], monkeypatch, lane=lane)
    for text in run.sent + [run.rows.block, run.answer]:
        assert "job_d/" not in text
    assert "for example doc_004, doc_007, doc_009" in run.sent[0]
    rows = _rows_of(run.answer)[0]
    assert all(r["file"].startswith("doc_") for r in rows)
    assert "fields: item (document id)" in run.rows.block


async def test_action_fail_places_rows_and_runs_no_c3_or_c4(monkeypatch):
    fx = _fixture("d")
    # note_04 has no result; "medium" next to it is an invented value, and a
    # sentence points at a step result: C4 and C3 would fire on an answer.
    rationale = ("I decline to label the rest.\nnote_04.txt: medium\n"
                 "See the step result for the others.\n" + D_JOIN)
    run = await _replay(fx, [{"action": "fail", "rationale": rationale}], monkeypatch)
    assert run.kind == "declined" and run.sent == []
    assert "@@" not in run.answer and len(_rows_of(run.answer)[0]) == 60
    assert "note_04.txt: medium" in run.answer            # compose's own text kept
    # The same text as an answer does fire C4: the decline is what spares it.
    as_answer = [c for c, _ in run.rows.problems(rationale)]
    assert "BRT-ORC-041" in as_answer
    assert run.rows.problems(rationale, declined=True) == []


async def test_action_fail_runs_no_c3_and_still_places_an_output(monkeypatch):
    fx = _fixture("b")
    stored = fx["steps"]["n1"]["output"]["files"]
    rationale = "I will not summarise; copy the rest from the stage result (n1).\n@@OUTPUT:n1@@"
    pointer_only = "Copy the files from the stage result (n1)."
    run = await _replay(fx, [{"action": "fail", "rationale": rationale}], monkeypatch)
    assert run.kind == "declined" and run.sent == []
    assert stored in run.answer and "@@" not in run.answer
    assert [c for c, _ in run.rows.problems(pointer_only)] == ["BRT-ORC-040"]
    assert run.rows.problems(pointer_only, declined=True) == []
