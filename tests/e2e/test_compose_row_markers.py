"""Planner ruling 21: rows placed by code (docs/design/compose_row_markers.md).

Compose names a finished list-holding step with ``@@ROWS:<id>@@`` (or
``@@ROWS_TABLE:<id>@@``); the engine writes the stored rows in. The real
engine, executor and router run against the scripted stand-in model server
(``compose_for`` scripts each compose call). The 500-token check proves the
engine side only: how often a real model chooses the marker is not measured
here.
"""
import json
import re
from pathlib import Path

import httpx
import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import (
    StubModels, drain_log_writes, lane_matrix, three_tier_matrix,
)

COMPOSE = "Mode: compose. Reply with the final answer."
# Ruling 32e's block (docs/design/compose_placement.md 2.3) replaces ruling
# 21's header; its fixed lines, as the note writes them.
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
REASK = ('Mode: compose. Your last reply had no final answer. Reply with '
         '{"response": "<the final answer>"} only.')
POINTER = ("Mode: compose. Your last reply was cut off at the length limit. Do not type out a "
           "step's rows or long output: put @@ROWS:<step id>@@ or @@OUTPUT:<step id>@@ where they "
           "belong and the code writes them in. "
           'Reply with {"response": "<the final answer>"} only.')
NO_STEP = "A marker names no step the code can place. Use only the step ids listed below."
MISSPELLED = ("A marker is misspelled. Write it exactly as shown below, with two @ signs at "
              "each end.")


def _block(line, no_result=0):
    return (INTRO + "\n" + ROWS_BULLET + "\nSteps with rows:\n" + line + "\n"
            "Items with no result (a step failed them and no other step gave them a value): "
            f"{no_result}. Never give them a value: say they have no result.")


def _repair(*sentences):
    """Ruling 32e's repair re-ask (it replaces ruling 21's 032 re-ask)."""
    return "\n".join(["Mode: compose. Your last reply cannot be used as it is:"]
                     + [f"- {s}" for s in sentences]
                     + ['Fix these and reply with {"response": "<the final answer>"} only.'])


ITEMS = [f"review {i:03d}" for i in range(200)]
FAIL_ITEM = "review 007"
LINE_200 = "- rate: 200 rows, input order, 1 failed; fields: item, sentiment, error"
BLOCK_200 = _block(LINE_200, no_result=1)
SMALL = ["Kettle: boils fast", "Toaster: burnt everything", "Blender: loud but fine"]
LINE_3 = "- rate: 3 rows, input order, 0 failed; fields: item, sentiment"
BLOCK_3 = _block(LINE_3)
INVALID = _repair(NO_STEP)


class RowStub(StubModels):
    """A per-item stand-in: the item named in the prompt gets ``s<NNN>``;
    ``FAIL_ITEM`` is reported failed by the model itself."""

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        req = self.requests[-1]
        if req.kind == "answerer" and FAIL_ITEM in req.system:
            resp._body["choices"][0]["message"]["content"] = json.dumps(
                {"status": "failed", "confidence": 0.0, "output": {},
                 "failure_reason": "I cannot read this review."})
        return resp


def _sentiment(req):
    m = re.search(r"review (\d{3})", req.system)
    if m:
        return {"sentiment": f"s{m.group(1)}"}
    return {"sentiment": "positive"}


def _rate(items=ITEMS):
    return {"type": "apply_to_each", "id": "rate", "instruction": "Rate this review.",
            "task_type": "extraction", "tier": "light", "items": items,
            "output_schema": {"sentiment": "string"}}


def _plain():
    return {"type": "answerer", "id": "plain", "instruction": "Say hello.",
            "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}


def _seq(*replies):
    """compose_for answering the n-th compose call with ``replies[n]``."""
    calls = []

    def compose_for(req):
        calls.append(req)
        return replies[min(len(calls), len(replies)) - 1]
    return compose_for


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "row_markers.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-rows")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.delenv("ORCHESTRATOR_MAX_TOKENS", raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


async def _decisions(db_path, run_id):
    import aiosqlite  # noqa: PLC0415
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT decision_json FROM logs WHERE run_id=? ORDER BY log_id", (run_id,),
        )).fetchall()
    return [json.loads(r[0]) for r in rows if r[0]]


async def _stored_envelopes(db_path, run_id):
    """(type, node id, inbound payload) of every stored envelope of the run."""
    import aiosqlite  # noqa: PLC0415
    async with aiosqlite.connect(db_path) as db:
        rows = await (await db.execute(
            "SELECT type, outbound_payload, inbound_payload FROM envelopes WHERE run_id=?",
            (run_id,),
        )).fetchall()
    return [(t, (json.loads(out) if out else {}).get("node_id"), inbound)
            for t, out, inbound in rows]


async def _codes(db_path, run_id):
    return [d.get("code") for d in await _decisions(db_path, run_id)]


async def _answer_kind(db_path, run_id):
    from src.db.state import get_run  # noqa: PLC0415
    from uuid import UUID  # noqa: PLC0415
    return (await get_run(UUID(run_id), db_path=db_path)).get("answer_kind")


def _rows(text):
    blocks = re.findall(r"```json\n(.*?)\n```", text, re.DOTALL)
    assert len(blocks) == 1, text[:400]
    return json.loads(blocks[0])


# --- 1. 200 rows -------------------------------------------------------------

@pytest.mark.asyncio
async def test_200_rows_placed_in_order_with_a_short_compose_reply(env):
    db_path, mp = env
    reply = {"response": "Classified below.\n@@ROWS:rate@@"}
    stub = RowStub(plan_nodes=[_rate()], answer_for=_sentiment,
                   compose_for=_seq(reply)).install(mp)
    result = await run_pipeline("Rate each review.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    # The compose reply the engine needed: under 500 characters, so under
    # 500 tokens with any tokenizer.
    assert len(json.dumps(reply)) < 500
    text = result["response"]
    assert text.startswith("Classified below.\n```json\n[\n{")
    rows = _rows(text)
    assert [r["item"] for r in rows] == ITEMS
    assert rows[7] == {"item": FAIL_ITEM, "error": "BRT-ANS-010"}
    assert rows[0] == {"item": "review 000", "sentiment": "s000"}
    assert rows[199] == {"item": "review 199", "sentiment": "s199"}
    assert "@@" not in text
    composes = stub.of_kind("compose")
    assert len(composes) == 1
    assert composes[0].user == COMPOSE + "\n" + BLOCK_200
    assert await _answer_kind(db_path, result["run_id"]) == "composed"
    placed = [d for d in await _decisions(db_path, result["run_id"]) if "rows_placed" in d]
    assert placed and placed[0]["rows_placed"] == {"rate": 200}
    # The model's own failure is BRT-ANS-010 above, yet no stored output
    # carries the private origin flag.
    stored = await _stored_envelopes(db_path, result["run_id"])
    assert stored and not any("_from_model" in (inbound or "") for _, _, inbound in stored)
    # The planner's view is unchanged: no block outside compose.
    for kind in ("plan", "re_plan"):
        for r in stub.of_kind(kind):
            assert "@@" not in r.user and "@@ROWS" not in r.system


# --- 2. gate ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_no_list_step_leaves_compose_and_reply_untouched(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain()],
                      compose_for=_seq({"response": "See @@ROWS:x@@ here."})).install(mp)
    result = await run_pipeline("Say hello.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "See @@ROWS:x@@ here."
    composes = stub.of_kind("compose")
    assert len(composes) == 1 and composes[0].user == COMPOSE
    assert "BRT-ORC-032" not in await _codes(db_path, result["run_id"])


# --- 3. rendering -------------------------------------------------------------

@pytest.mark.asyncio
async def test_table_form_on_its_own_lines(env):
    db_path, mp = env
    items = ["a|b", "c", "d"]
    stub = StubModels(plan_nodes=[_rate(items)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": "Table: @@ROWS_TABLE:rate@@ Done."})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == (
        "Table:\n\n| item | sentiment |\n| --- | --- |\n| a\\|b | ok |\n| c | ok |\n| d | ok |\n\nDone."
    )
    assert stub.of_kind("compose")[0].user == COMPOSE + "\n" + _block(
        "- rate: 3 rows, input order, 0 failed; fields: item, sentiment")


@pytest.mark.asyncio
async def test_object_answer_leaf_marker_becomes_the_rows_list(env):
    db_path, mp = env
    StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
               compose_for=_seq({"response": {"reviews": "@@ROWS:rate@@"}})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    data = json.loads(result["response"])
    assert data == {"reviews": [{"item": s, "sentiment": "ok"} for s in SMALL]}


# --- 4. invalid and near-miss markers -----------------------------------------

BAD = [("@@ROWS:nope@@", NO_STEP), ("@@ROWS:plain@@", NO_STEP), ("@@Row:rate@@", MISSPELLED),
       ("@@ROWS rate@@", MISSPELLED), ("@@ROWS:rate@", MISSPELLED)]


@pytest.mark.parametrize("bad, sentence", BAD)
@pytest.mark.asyncio
async def test_invalid_marker_is_asked_once_then_placed(env, bad, sentence):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL), _plain()],
                      answer_for=lambda r: ({"result": "hi"} if "Say hello" in r.system
                                            else {"sentiment": "ok"}),
                      compose_for=_seq({"response": f"List: {bad}"},
                                       {"response": "List:\n@@ROWS:rate@@"})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    composes = stub.of_kind("compose")
    assert len(composes) == 2
    assert composes[1].user == _repair(sentence) + "\n" + BLOCK_3
    assert [r["item"] for r in _rows(result["response"])] == SMALL
    assert "@@" not in result["response"]
    codes = await _codes(db_path, result["run_id"])
    assert codes.count("BRT-ORC-032") == 1
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.asyncio
async def test_invalid_marker_twice_is_salvaged_with_032(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": "@@ROWS:nope@@"},
                                       {"response": "@@rows:nope@@"})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("compose")) == 2
    text = result["response"]
    assert text.startswith(engine.SALVAGE_NOTE)
    assert "@@" not in text.lower()
    decisions = [d for d in await _decisions(db_path, result["run_id"])
                 if d.get("code") == "BRT-ORC-032"]
    # The two events are fire-and-forget log writes, so their row order is not
    # guaranteed: assert one re-ask event and one warning, in either order.
    assert sorted(d.get("severity") or "" for d in decisions) == ["", "warning"]
    assert await _answer_kind(db_path, result["run_id"]) == "salvaged"


@pytest.mark.asyncio
async def test_padded_and_any_case_marker_is_valid(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": "@@ rows : rate @@"})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("compose")) == 1
    assert [r["item"] for r in _rows(result["response"])] == SMALL


@pytest.mark.asyncio
async def test_marker_quoted_from_the_operator_stays_text(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": "You asked about @@ROWS:example@@."})).install(mp)
    result = await run_pipeline("Explain what @@ROWS:example@@ means.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == "You asked about @@ROWS:example@@."
    assert len(stub.of_kind("compose")) == 1
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.asyncio
async def test_declined_rationale_gets_rows_too(env):
    db_path, mp = env
    StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
               compose_for=_seq({"action": "fail", "rationale": "Only these:\n@@ROWS:rate@@"})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert "@@" not in result["response"]
    assert [r["item"] for r in _rows(result["response"])] == SMALL
    assert await _answer_kind(db_path, result["run_id"]) == "declined"


@pytest.mark.asyncio
async def test_marker_as_a_dict_key_takes_the_032_path(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": {"@@ROWS:rate@@": "x"}},
                                       {"response": "List:\n@@ROWS:rate@@"})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    composes = stub.of_kind("compose")
    assert len(composes) == 2 and composes[1].user == INVALID + "\n" + BLOCK_3
    assert "@@" not in result["response"]
    assert [r["item"] for r in _rows(result["response"])] == SMALL
    assert (await _codes(db_path, result["run_id"])).count("BRT-ORC-032") == 1


@pytest.mark.asyncio
async def test_worker_text_quoted_by_compose_stays_text(env):
    db_path, mp = env
    said = 'use @@rows "x" here'
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": said},
                      compose_for=_seq({"response": f"The reviewers wrote: {said}. That is all."})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == f"The reviewers wrote: {said}. That is all."
    assert len(stub.of_kind("compose")) == 1
    assert "BRT-ORC-032" not in await _codes(db_path, result["run_id"])


@pytest.mark.parametrize("step_id", ['ra"te', "ra\\te"])
@pytest.mark.asyncio
async def test_step_id_with_quote_or_backslash_in_a_dict_answer(env, step_id):
    db_path, mp = env
    node = {**_rate(SMALL), "id": step_id}
    stub = StubModels(plan_nodes=[node], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": {"rows": f"@@ROWS:{step_id}@@"}})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("compose")) == 1
    rows = json.loads(result["response"])["rows"]
    assert [r["item"] for r in rows] == SMALL


@pytest.mark.asyncio
async def test_reask_without_a_list_step_is_byte_identical(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain()],
                      compose_for=_seq({"action": "compose"}, {"response": "Second."})).install(mp)
    result = await run_pipeline("Say hello.", db_path=db_path)
    await drain_log_writes()
    composes = stub.of_kind("compose")
    assert len(composes) == 2
    assert composes[0].user == engine._MODE_USER_MESSAGES["compose"]
    assert composes[1].user == engine._COMPOSE_REASK
    assert result["response"] == "Second."


# --- 5. retyping still works --------------------------------------------------

@pytest.mark.asyncio
async def test_retyped_rows_are_accepted(env):
    db_path, mp = env
    typed = json.dumps([{"item": s, "sentiment": "ok"} for s in SMALL])
    StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
               compose_for=_seq({"response": typed})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert result["response"] == typed
    assert not [d for d in await _decisions(db_path, result["run_id"]) if "rows_placed" in d]
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


# --- 6. cut-off ---------------------------------------------------------------

@pytest.mark.parametrize("cap, retry_cap", [(6000, 12000), (16000, 16000)])
@pytest.mark.asyncio
async def test_cut_off_compose_retry_points_to_the_marker(env, cap, retry_cap):
    db_path, mp = env
    mp.setenv("ORCHESTRATOR_MAX_TOKENS", str(cap))
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq(('{"response": "[{\\"item\\": \\"Ket', "length"),
                                       {"response": "@@ROWS:rate@@"})).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    composes = stub.of_kind("compose")
    assert len(composes) == 2
    assert composes[0].max_tokens == cap
    assert composes[1].max_tokens == retry_cap
    assert composes[1].user == POINTER + "\nSteps with rows:\n" + LINE_3
    assert [r["item"] for r in _rows(result["response"])] == SMALL
    events = [d for d in await _decisions(db_path, result["run_id"])
              if d.get("code") == "BRT-ORC-018"]
    assert events and events[0].get("retry_message") == "rows_pointer"


@pytest.mark.asyncio
async def test_pointer_retry_cut_again_skips_the_json_retry_and_salvages(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq(('{"response": "cut', "length"),
                                       ('{"response": "cut again', "length"))).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("compose")) == 2
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    assert await _answer_kind(db_path, result["run_id"]) == "salvaged"


@pytest.mark.asyncio
async def test_pointer_retry_cut_again_but_parseable_takes_the_r2_path(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq(('{"response": "cut', "length"),
                                       ({"response": "@@ROWS:rate@@"}, "length"))).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("compose")) == 2
    assert [r["item"] for r in _rows(result["response"])] == SMALL
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.parametrize("cap, posts", [(6000, 2), (16000, 1)])
@pytest.mark.asyncio
async def test_cut_off_without_a_list_step_is_unchanged(env, cap, posts):
    db_path, mp = env
    mp.setenv("ORCHESTRATOR_MAX_TOKENS", str(cap))
    stub = StubModels(plan_nodes=[_plain()],
                      compose_for=_seq(({"response": "Short."}, "length"),
                                       {"response": "Whole."})).install(mp)
    result = await run_pipeline("Say hello.", db_path=db_path)
    await drain_log_writes()
    composes = stub.of_kind("compose")
    assert len(composes) == posts
    assert all(c.user == COMPOSE for c in composes)
    if posts == 2:
        assert composes[1].max_tokens == 12000
    assert result["response"] in ("Short.", "Whole.")


@pytest.mark.asyncio
async def test_worst_case_is_nine_compose_posts(env):
    """First, 029 re-ask and 032 re-ask; each cut, pointer-retried and
    JSON-retried: 3 x 3 posts."""
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate(SMALL)], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq(
                          ("garbage", "length"), ("not json", "stop"), {"action": "compose"},
                          ("garbage", "length"), ("not json", "stop"), {"response": "@@ROWS:nope@@"},
                          ("garbage", "length"), ("not json", "stop"), {"response": "@@ROWS:rate@@"},
                      )).install(mp)
    result = await run_pipeline("Rate each.", db_path=db_path)
    await drain_log_writes()
    composes = stub.of_kind("compose")
    assert len(composes) == 9
    assert composes[3].user == REASK + "\n" + BLOCK_3
    assert composes[6].user == INVALID + "\n" + BLOCK_3
    assert [r["item"] for r in _rows(result["response"])] == SMALL
    codes = await _codes(db_path, result["run_id"])
    assert "BRT-ORC-029" in codes and "BRT-ORC-032" in codes
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


# --- 7. lane (ruling 21b) -------------------------------------------------------

PASSAGE = ("The quarterly reconciliation for account forty two shows a duplicated "
           "wire transfer of eleven thousand dollars that nobody has explained yet.")


def _lane_env(tmp_path, monkeypatch, mode):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "email_001.txt").write_text(PASSAGE + " Please call me back.", encoding="utf-8")
    (docs / "email_002.txt").write_text("My parcel never arrived, the tracking number is dead.",
                                        encoding="utf-8")
    for name in ("BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
                 "BORG_LANE_MAX_OUTPUT_CHARS", "BORG_LANE_MAX_VERBATIM_CHARS",
                 "BORG_LANE_MAX_NGRAM_SHARE", "BORG_LANE_PART_CHARS", "ORCHESTRATOR_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("BORG_LANE_MODE", mode)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-rows-lane")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix",
                        lane_matrix(9.5) if mode == "all_local" else lane_matrix())


def _classify():
    return {"type": "apply_to_each", "id": "classify", "skill_id": "classify_into_buckets",
            "skill_version": 1, "task_type": "extraction", "tier": "light",
            "skill_args": {"buckets": ["billing", "shipping"], "items_ref": "documents"}}


def _bucket(req):
    return {"bucket": "billing", "confidence": 0.9, "defer_reason": ""}


@pytest.mark.asyncio
async def test_guard_withholds_a_placed_block_holding_a_document_passage(tmp_path, monkeypatch):
    from src.orchestrator import row_markers  # noqa: PLC0415
    _lane_env(tmp_path, monkeypatch, "raw_local")
    real = row_markers.rows_for

    def forced(env, lane):
        rows = real(env, lane)
        if rows:
            rows[0] = {**rows[0], "defer_reason": PASSAGE}
        return rows
    monkeypatch.setattr(row_markers, "rows_for", forced)
    db_path = str(tmp_path / "guard.db")
    await create_all(db_path)
    StubModels(plan_nodes=[_classify()], answer_for=_bucket,
               compose_for=_seq({"response": "Rows:\n@@ROWS:classify@@"})).install(monkeypatch)
    result = await run_pipeline("Classify each email.", db_path=db_path,
                                documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "failed"
    assert "BRT-LANE-009" in str(result.get("error"))
    assert "duplicated" not in (result["response"] or "")


@pytest.mark.asyncio
async def test_same_passage_in_compose_text_behaves_as_today(tmp_path, monkeypatch):
    _lane_env(tmp_path, monkeypatch, "raw_local")
    db_path = str(tmp_path / "guard_own_text.db")
    await create_all(db_path)
    stub = StubModels(plan_nodes=[_classify()], answer_for=_bucket,
                      compose_for=_seq({"response": PASSAGE + "\n@@ROWS:classify@@"})).install(monkeypatch)
    result = await run_pipeline("Classify each email.", db_path=db_path,
                                documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    text = result["response"]
    assert text.startswith(PASSAGE + "\n```json\n")
    rows = _rows(text)
    # Ruling 32e: rows carry the names the manifest showed compose, never
    # document text (ruling 21's "part ids only" pin, widened to names).
    assert [r["item"] for r in rows] == ["email_001.txt", "email_002.txt"]
    assert "parcel" not in text and "Please call me back" not in text
    assert [r for r in stub.of_kind("answerer") if r.is_local]


@pytest.mark.asyncio
async def test_ids_manifest_rows_carry_part_ids_only(tmp_path, monkeypatch):
    _lane_env(tmp_path, monkeypatch, "raw_local")
    monkeypatch.setenv("BORG_LANE_MANIFEST", "ids")
    db_path = str(tmp_path / "guard_ids.db")
    await create_all(db_path)
    stub = StubModels(plan_nodes=[_classify()], answer_for=_bucket,
                      compose_for=_seq({"response": "@@ROWS:classify@@"})).install(monkeypatch)
    result = await run_pipeline("Classify each email.", db_path=db_path,
                                documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    rows = _rows(result["response"])
    assert [r["item"] for r in rows] == ["doc_001", "doc_002"]
    assert "email_00" not in stub.of_kind("compose")[0].user
    assert "fields: item (document id)" in stub.of_kind("compose")[0].user


@pytest.mark.asyncio
async def test_all_local_composed_answer_without_markers_is_unchanged(tmp_path, monkeypatch):
    _lane_env(tmp_path, monkeypatch, "all_local")
    db_path = str(tmp_path / "all_local.db")
    await create_all(db_path)
    StubModels(plan_nodes=[_classify()], answer_for=_bucket,
               compose_for=_seq({"response": "Two emails, both billing."})).install(monkeypatch)
    result = await run_pipeline("Classify each email.", db_path=db_path,
                                documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    assert result["response"] == "Two emails, both billing."
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


# --- 8. replay of stage-1 job A -------------------------------------------------

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "stage1_job_a_replay.json"
JOB_A_DATA = Path(__file__).resolve().parents[2] / "bench" / "data"


class ReplayStub(StubModels):
    """Answers from the recording, plus two supplied replies (the fourth
    re_plan and the marker reply); any further unrecorded request fails
    loudly."""

    def __init__(self, rec, compose_for):
        super().__init__(plan_nodes=[], compose_for=compose_for)
        self.rec = rec
        self.replans = list(rec["orchestrator_replies"]["re_plan"])
        self.final_replan_used = False
        self.urgency_instruction = next(
            n["instruction"] for n in rec["orchestrator_replies"]["re_plan"][0]["next_nodes"]
            if n["id"] == "extract_urgency")
        self.plan_content_for = lambda req: rec["orchestrator_replies"]["plan"][0]
        self.replan_for = self._replan
        self.answer_for = self._item

    def _replan(self, req):
        if self.replans:
            return self.replans.pop(0)
        # The recorded run never reached a fourth re_plan (R1 skipped the
        # re-sent steps and went straight to compose); the planner's recorded
        # last decision, {"action": "compose"}, answers it once.
        assert not self.final_replan_used, "unrecorded re_plan request"
        self.final_replan_used = True
        return self.rec["orchestrator_replies"]["compose"][0]

    def _item(self, req):
        m = re.search(r"\b(doc_\d{3})\b", req.system)
        assert m, "answerer request names no recorded document"
        step = "extract_urgency" if self.urgency_instruction in req.system else "classify_emails"
        doc = m.group(1)
        if doc in self.rec["items"][step]:
            return self.rec["items"][step][doc]
        assert doc in self.rec["failures"][step], f"unrecorded item {step}/{doc}"
        # The recording keeps only this item's failure (BRT-ANS-005, a missing
        # defer_reason); an output without it reproduces that failure.
        return {"bucket": "defer", "confidence": 0.0}


@pytest.mark.asyncio
async def test_replayed_stage1_job_a_completes_with_full_rows(tmp_path, monkeypatch):
    """Stage-1 job A replayed from its recording (plan, three re_plans, the
    compose reply and every item's output), plus two supplied replies (the
    fourth re_plan and the marker reply): the recorded run never reached a
    fourth re_plan, and it never got a compose re-ask.

    Follows ruling 32c: the recorded plan puts output_schema {file, category,
    urgency} on its classify_into_buckets node, which the skill does not
    return, so the first plan is refused with BRT-ORC-035 (no model call) and a
    scripted first re_plan repeats the plan. The second hit runs the step as
    the skill step it is, and the recorded re_plans follow."""
    rec = json.loads(FIXTURE.read_text(encoding="utf-8"))
    db_path = str(tmp_path / "replay_a.db")
    await create_all(db_path)
    for name in ("BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
                 "BORG_LANE_PART_CHARS", "ORCHESTRATOR_MAX_TOKENS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(JOB_A_DATA))
    monkeypatch.setenv("BORG_LANE_MODE", rec["lane_mode"])
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-replay")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    recorded_compose = rec["orchestrator_replies"]["compose"][0]
    stub = ReplayStub(rec, _seq(recorded_compose,
                                {"response": "@@ROWS:extract_urgency@@"})).install(monkeypatch)
    # Ruling 32c: the first plan is refused; a scripted first re_plan repeats
    # the recorded plan, so the second hit runs classify_emails (renamed _2)
    # without the fields. The recording's re_plans then follow as recorded.
    stub.replans.insert(0, {"action": "continue", "rationale": "same plan again",
                            "next_nodes": rec["orchestrator_replies"]["plan"][0]["plan"]["nodes"]})
    result = await run_pipeline(rec["instruction"], db_path=db_path,
                                documents={"folder": rec["folder"]})
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    # The run's per-item steps, read from its stored envelopes, are the
    # recording's.
    stored = await _stored_envelopes(db_path, result["run_id"])
    resolved = {nid for kind, nid, inbound in stored
                if kind == "apply_to_each" and json.loads(inbound or "{}").get("status") == "resolved"}
    # classify_emails was refused first (BRT-ORC-035), so its repeat ran as
    # classify_emails_2 (R1's rename of a re-sent id).
    assert resolved == {"classify_emails_2" if n == "classify_emails" else n for n in rec["items"]}
    # Every stored step id is a recorded one, or R1's rename of a re-sent one.
    replies = rec["orchestrator_replies"]
    recorded = {n["id"] for n in replies["plan"][0]["plan"]["nodes"]}
    recorded |= {n["id"] for r in replies["re_plan"] for n in (r.get("next_nodes") or [])}
    for _, nid, _ in stored:
        if nid is not None:
            assert re.sub(r"_\d+$", "", nid) in recorded, nid
    assert not stub.replans, "a re_plan reply was not used"
    composes = stub.of_kind("compose")
    assert len(composes) == 2 and composes[1].user.startswith(REASK + "\n" + INTRO)
    assert ("- extract_urgency: 200 rows, input order, 0 failed; fields: item (file name), "
            "urgency, category") in composes[1].user
    rows = _rows(result["response"])
    docs = sorted(rec["items"]["extract_urgency"])
    # Ruling 32e: the rows carry the documents' names as the manifest showed them.
    names = [f"job_a/email_{d[4:]}.txt" for d in docs]
    assert [r["item"] for r in rows] == names
    for doc, row in zip(docs, rows):
        assert {k: v for k, v in row.items() if k != "item"} == rec["items"]["extract_urgency"][doc]
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


# --- 9. ruling 32e: whole outputs, lane and post counts --------------------------------------

LONG = "LONGREPORT " + "w" * 2489                     # 2,500 characters


def _read(node_id="read"):
    return {"type": "answerer", "id": node_id, "instruction": "Report on the email.",
            "documents": ["doc_001"], "task_type": "extraction", "tier": "light",
            "output_schema": {"report": "string"}}


def _force_passage(monkeypatch):
    """A long raw output holding a document passage the output guard missed
    (the ruling-21b forced-row pattern): the text guard must withhold it."""
    from src.orchestrator import row_markers  # noqa: PLC0415
    real = row_markers.long_outputs

    def forced(results, steps=None, lane=None):
        found = real(results, steps, lane)
        for o in found.values():
            for k in list(o.fields):
                o.fields[k] = o.fields[k] + " " + PASSAGE
        return found
    monkeypatch.setattr(row_markers, "long_outputs", forced)


@pytest.mark.asyncio
async def test_placed_raw_output_holding_a_passage_gives_lane_009(tmp_path, monkeypatch):
    _lane_env(tmp_path, monkeypatch, "raw_local")
    monkeypatch.setenv("BORG_LANE_MAX_OUTPUT_CHARS", "10000")      # the setting raised
    _force_passage(monkeypatch)
    db_path = str(tmp_path / "out_guard.db")
    await create_all(db_path)
    StubModels(plan_nodes=[_read()], answer_for=lambda r: {"report": LONG},
               compose_for=_seq({"response": "Report:\n@@OUTPUT:read@@"})).install(monkeypatch)
    result = await run_pipeline("Report on the email.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "failed"
    assert "BRT-LANE-009" in str(result.get("error"))
    assert "duplicated" not in (result["response"] or "")


@pytest.mark.asyncio
async def test_s1_salvage_of_a_raw_long_field_gets_the_same_guard(tmp_path, monkeypatch):
    _lane_env(tmp_path, monkeypatch, "raw_local")
    monkeypatch.setenv("BORG_LANE_MAX_OUTPUT_CHARS", "10000")
    _force_passage(monkeypatch)
    db_path = str(tmp_path / "s1_guard.db")
    await create_all(db_path)
    pointer = {"response": "The report is in the step result (read)."}
    StubModels(plan_nodes=[_read()], answer_for=lambda r: {"report": LONG},
               compose_for=_seq(pointer, pointer)).install(monkeypatch)
    result = await run_pipeline("Report on the email.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    assert result["status"] == "failed"
    assert "BRT-LANE-009" in str(result.get("error"))


@pytest.mark.asyncio
async def test_output_marker_on_a_held_back_step_is_032_and_its_text_goes_nowhere(
        tmp_path, monkeypatch):
    _lane_env(tmp_path, monkeypatch, "raw_local")              # default 1,500-character cap
    db_path = str(tmp_path / "held_back.db")
    await create_all(db_path)
    # A finished document step (classify) keeps the run from BRT-ORC-034.
    stub = StubModels(plan_nodes=[_classify(), _read()],
                      answer_for=lambda r: ({"report": LONG} if "Report on" in r.system
                                            else _bucket(r)),
                      compose_for=_seq({"response": "@@OUTPUT:read@@"},
                                       {"response": "The report could not be shown."})).install(monkeypatch)
    result = await run_pipeline("Look at the emails.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    assert result["response"] == "The report could not be shown."
    composes = stub.of_kind("compose")
    assert len(composes) == 2 and composes[1].user.startswith(_repair(NO_STEP))
    assert (await _codes(db_path, result["run_id"])).count("BRT-ORC-032") == 1
    for req in stub.requests:
        if req.kind in ("plan", "re_plan", "compose"):
            assert "LONGREPORT" not in json.dumps(req.body)
    assert "LONGREPORT" not in result["response"]


@pytest.mark.asyncio
async def test_long_output_run_worst_case_is_nine_compose_posts(env):
    """An answerer run with a 2,500-character output now gets the block, so the
    pointer retry always runs: 3 invocations x 3 posts (6 without the JSON retry)."""
    db_path, mp = env
    plain = {"type": "answerer", "id": "plain", "instruction": "Write the report.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}
    stub = StubModels(plan_nodes=[plain], answer_for=lambda r: {"result": LONG},
                      compose_for=_seq(
                          ("garbage", "length"), ("not json", "stop"), {"action": "compose"},
                          ("garbage", "length"), ("not json", "stop"), {"response": "@@OUTPUT:nope@@"},
                          ("garbage", "length"), ("not json", "stop"), {"response": "@@OUTPUT:plain@@"},
                      )).install(mp)
    result = await run_pipeline("Write the report.", db_path=db_path)
    await drain_log_writes()
    composes = stub.of_kind("compose")
    assert len(composes) == 9
    assert composes[1].user.startswith(POINTER + "\nSteps with a long output:\n- plain: 2,500")
    assert result["response"] == LONG
    assert await _answer_kind(db_path, result["run_id"]) == "composed"


@pytest.mark.asyncio
async def test_long_output_run_without_cuts_makes_six_posts_at_most(env):
    db_path, mp = env
    plain = {"type": "answerer", "id": "plain", "instruction": "Write the report.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}
    stub = StubModels(plan_nodes=[plain], answer_for=lambda r: {"result": LONG},
                      compose_for=_seq(
                          ("garbage", "length"), {"action": "compose"},
                          ("garbage", "length"), {"response": "See (plain)."},
                          ("garbage", "length"), {"response": "Report:\n@@OUTPUT:plain@@"},
                      )).install(mp)
    result = await run_pipeline("Write the report.", db_path=db_path)
    await drain_log_writes()
    assert len(stub.of_kind("compose")) == 6
    assert result["response"] == "Report:\n\n" + LONG


@pytest.mark.asyncio
async def test_plan_and_re_plan_bodies_are_byte_identical_with_32e_on(env):
    """The block reaches compose only: with list steps and a long output, the
    plan and re_plan request bodies equal those of the same run with 32e's
    context turned off (run ids masked)."""
    db_path, mp = env
    plain = {"type": "answerer", "id": "plain", "instruction": "Write the report.",
             "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}
    uuid = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

    async def bodies():
        stub = StubModels(plan_nodes=[_rate(SMALL), plain],
                          answer_for=lambda r: ({"result": LONG} if "Write the report" in r.system
                                                else {"sentiment": "ok"}),
                          compose_for=_seq({"response": "Done."})).install(mp)
        result = await run_pipeline("Rate each and report.", db_path=db_path)
        await drain_log_writes()
        assert result["status"] == "completed", result.get("error")
        sent = [uuid.sub("<id>", json.dumps(r.body, sort_keys=True))
                for r in stub.requests if r.kind in ("plan", "re_plan")]
        return sent, stub.of_kind("compose")[0].user

    on, compose_on = await bodies()
    real = engine._RowContext.for_run
    mp.setattr(engine._RowContext, "for_run",
               classmethod(lambda cls, *a, **k: cls({}, set(), None)))
    off, compose_off = await bodies()
    mp.setattr(engine._RowContext, "for_run", real)
    assert on and on == off
    assert compose_on.startswith(COMPOSE + "\n" + INTRO) and compose_off == COMPOSE
