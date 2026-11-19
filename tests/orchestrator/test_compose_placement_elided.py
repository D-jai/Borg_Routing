"""Planner ruling 36a: check C5, a list typed and cut short (BRT-ORC-039).

It fires when a list step listed in the block is placed by no marker and
compose's own text holds an explicit elision in, or directly after, rows it
typed: an ellipsis as a list element, in a code comment line, or in a line
that says the rest follows (``compose_checks.ELISION_WORDS``). It does not
fire on an ellipsis in prose, in a quoted excerpt, inside a placed block, or
when every listed list step is placed. A fire means the shared repair re-ask,
then the existing salvage (S2).

The motivating reply is trial 35h's round-1-data job A
(``tests/fixtures/trial35h_compose_a.json``, written by
``extract_trial35h_compose.py``): ten typed rows, then
``// ... (full list of all 200 entries follows the exact pattern above)``.
"""
import json
from uuid import uuid4

import pytest

from bench.run_bench import score_classification
from src.envelopes.schema import InboundEnvelope
from src.orchestrator import compose_checks as cc
from src.orchestrator import engine
from src.orchestrator import row_markers as rm
from tests.orchestrator.test_compose_placement_replay import (
    FIX, _key, _repair, _replay, _rows_of,
)

S39_A = ("The reader sees only this answer, and a shortened list is not the answer: put "
         "@@ROWS:classify_emails@@ where the list belongs, and the code writes all 200 of its "
         "rows.")
SPEC = "@@ROWS:classify_emails | file=item, category=bucket, urgency@@"
GOOD = {"response": "Here is the classification for all 200 emails.\n\n" + SPEC}


def _fixture():
    return json.loads((FIX / "trial35h_compose_a.json").read_text(encoding="utf-8"))


def _score(answer):
    return score_classification(answer, _key("a"), ["category", "urgency"], "file")


# --- the motivating reply (trial 35h, round-1 data) ------------------------------------------

def test_fixture_is_the_recorded_elided_reply():
    fx = _fixture()
    text = fx["recorded_replies"][0]["response"]
    assert "// ... (full list of all 200 entries follows the exact pattern above)" in text
    assert fx["recorded_quality"]["items_answered"] == 0
    assert [e["field"] for e in fx["asked_but_not_produced"]] == ["file", "category"]


async def test_recorded_reply_fires_039_with_the_exact_repair(monkeypatch):
    fx = _fixture()
    run = await _replay(fx, [fx["recorded_replies"][0], GOOD], monkeypatch)
    assert run.sent == [_repair(S39_A) + "\n" + run.rows.block]
    assert [c for c in run.codes if c][0] == "BRT-ORC-039"
    assert run.kind == "composed"
    score = _score(run.answer)
    assert score["items_answered"] == 200
    # The stored rows' own figures (round-1 data's labels), placed unchanged.
    assert score["per_field_accuracy"] == {"category": 0.575, "urgency": 0.165}


async def test_elided_twice_is_salvaged_with_the_stored_rows(monkeypatch):
    fx = _fixture()
    rec = fx["recorded_replies"][0]
    run = await _replay(fx, [rec, rec], monkeypatch)
    assert run.kind == "salvaged"
    assert run.answer.startswith(engine.SALVAGE_NOTE)
    assert "follows the exact pattern" not in run.answer          # compose's own text dropped
    assert run.warning and run.warning[0]["code"] == "BRT-ORC-039"
    assert run.warning[0]["codes"] == ["BRT-ORC-039"]
    rows = [r for block in _rows_of(run.answer) for r in block]
    assert len(rows) == 200 and rows[0]["item"] == "job_a/email_001.txt"
    # No marker in the second reply: S2 writes the plain marker's form, whose
    # "item" the grader does not read as "file" (the stated limit of 32e, A6).
    assert _score(run.answer)["items_answered"] == 0


async def test_elided_again_with_the_spec_salvages_the_placed_rows_gradeable(monkeypatch):
    fx = _fixture()
    rec = fx["recorded_replies"][0]["response"]
    again = {"response": rec.replace("\n]\n```", "\n  " + SPEC + "\n]\n```")}
    run = await _replay(fx, [fx["recorded_replies"][0], again], monkeypatch)
    assert run.kind == "salvaged"
    assert "## classify_emails\n```json" in run.answer
    assert "follows the exact pattern" not in run.answer
    score = _score(run.answer)
    assert score["items_answered"] == 200
    # The stored rows' own figures (round-1 data's labels), placed unchanged.
    assert score["per_field_accuracy"] == {"category": 0.575, "urgency": 0.165}


async def test_typed_preview_with_the_list_placed_does_not_fire(monkeypatch):
    fx = _fixture()
    rec = fx["recorded_replies"][0]["response"]
    reply = {"response": rec + "\n\nThe full list:\n\n" + SPEC}
    run = await _replay(fx, [reply], monkeypatch)
    assert run.sent == [] and run.kind == "composed"
    assert _score(run.answer)["items_answered"] == 200


# --- unit cases ---------------------------------------------------------------------------------

def _env(output):
    env = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status="resolved",
                          output=output)
    env._node_type = "apply_to_each"
    env._data_scope = None
    return env


def _agg(step_items, value="billing"):
    results = [{"index": i, "item": it, "output": {"bucket": value}}
               for i, it in enumerate(step_items)]
    return {"results": results, "failures": [], "total": len(results),
            "success_count": len(results), "failure_count": 0}


FILES = [f"email_{n:03d}.txt" for n in range(1, 13)]


def _ctx(**extra):
    results = {"classify": _env(_agg(FILES))}
    for sid, (files, value) in extra.items():
        results[sid] = _env(_agg(files, value))
    steps = rm.list_steps(results, None)
    return steps, cc.ItemModel.build(steps, results)


def _fires(reply, **extra):
    steps, model = _ctx(**extra)
    return [p for p in cc.check(reply, steps, {}, model, [], [], list(steps))
            if p[0] == "BRT-ORC-039"]


ROWS = "\n".join(f'  {{"file": "{f}", "category": "billing"}},' for f in FILES[:3])
BULLETS = "\n".join(f"- {f}: billing" for f in FILES[:3])


@pytest.mark.parametrize("reply", [
    "```json\n[\n" + ROWS + "\n  // ... (the remaining 9 entries follow the same pattern)\n]\n```",
    "```json\n[\n" + ROWS + "\n  ...\n]\n```",
    "```json\n[\n" + ROWS + '\n  "..."\n]\n```',
    "```json\n[\n" + ROWS + "\n  {...}\n]\n```",
    "```json\n[\n" + ROWS + "\n  # … and so on\n]\n```",
    BULLETS + "\n- ...",
    BULLETS + "\n... and 9 more",
    BULLETS + "\n\n(… the rest omitted)",
    "| file | category |\n| --- | --- |\n| email_001.txt | billing |\n| ... | ... |",
    'email_001.txt: billing; email_002.txt: billing; ...',
    ["Rows:", {"file": "email_001.txt", "category": "billing"}, "..."],
    {"results": [{"file": "email_001.txt", "category": "billing"}, "… 11 more"]},
])
def test_c5_fires_on_an_elided_list(reply):
    assert _fires(reply) == [("BRT-ORC-039", (
        "The reader sees only this answer, and a shortened list is not the answer: put "
        "@@ROWS:classify@@ where the list belongs, and the code writes all 12 of its rows."))]


@pytest.mark.parametrize("reply", [
    # prose
    BULLETS + "\nThe notes cover billing, shipping, ... and feedback.",
    "The notes cover billing, shipping, ... and feedback. Every email is classified.",
    # a quoted excerpt
    BULLETS + '\n> "I was charged twice... the remaining balance follows next month"',
    BULLETS + '\nemail_003.txt quotes the customer: "my parcel... remaining items etc."',
    '- email_001.txt: billing (the customer wrote "refund... etc.")',
    # an ellipsis with no typed rows above it
    "Here is the list:\n...\n// ... remaining entries follow",
    # a typed row's own words
    "- email_001.txt: billing, refund, chargeback etc...",
])
def test_c5_does_not_fire_on_prose_or_quotes(reply):
    assert _fires(reply) == []


def test_c5_does_not_fire_inside_a_placed_block():
    results = {"classify": _env(_agg(FILES[:2], "... remaining items follow"))}
    steps = rm.list_steps(results, None)
    model = cc.ItemModel.build(steps, results)
    reply = "All emails:\n\n@@ROWS:classify@@"
    assert cc.check(reply, steps, {}, model, [], [], ["classify"]) == []
    placed, _ = rm.place(reply, steps)
    assert "... remaining items follow" in placed


def test_c5_does_not_fire_when_every_list_step_is_placed():
    reply = ("```json\n[\n" + ROWS + "\n  // ... (the remaining entries follow)\n]\n```\n\n"
             "@@ROWS:classify@@\n\n@@ROWS:urgency@@")
    assert _fires(reply, urgency=(FILES, "3")) == []
    joined = "```json\n[\n" + ROWS + "\n  ...\n]\n```\n\n@@ROWS:classify + urgency@@"
    assert _fires(joined, urgency=(FILES, "3")) == []


def test_c5_names_only_the_unplaced_step():
    other = [f"note_{n:02d}.txt" for n in range(1, 6)]
    reply = ("@@ROWS:classify@@\n\n- note_01.txt: low\n- note_02.txt: low\n- ...")
    assert _fires(reply, notes=(other, "low")) == [("BRT-ORC-039", (
        "The reader sees only this answer, and a shortened list is not the answer: put "
        "@@ROWS:notes@@ where the list belongs, and the code writes all 5 of its rows."))]


def test_c5_does_not_run_on_a_decline():
    steps, model = _ctx()
    reply = BULLETS + "\n- ..."
    assert cc.check(reply, steps, {}, model, [], [], ["classify"], declined=True) == []


def test_elision_words_are_one_short_list():
    assert cc.ELISION_WORDS == ("follows", "remaining", "and so on", "etc", "omitted",
                                "truncated", "N more")
