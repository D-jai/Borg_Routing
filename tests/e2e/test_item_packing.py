"""Planner ruling 23 (and 23a-23g): packing small apply_to_each items into one call.

Design note: docs/design/item_packing.md. The real expander, executor and
router run against the stand-in server (``stub_model_server.py``); a packed
call is the stand-in's kind ``packed``. Every test here opts in to packing;
``tests/conftest.py`` pins ``BORG_PACK_MAX_ITEMS=0`` for every other test.
"""
from __future__ import annotations

import json
import sqlite3
import uuid

import httpx
import pytest

from src.envelopes.schema import WrappedTask
from src.orchestrator import engine
from src.orchestrator.apply_to_each import expand_and_run
from src.registry.models import ModelInfo, Pricing
from tests.e2e.stub_model_server import (
    StubModels,
    drain_log_writes,
    stub_model,
    three_tier_matrix,
)

OPERATOR = "Sort each of these short notes into kind a, b or c, and tell me how many of each."
TASK = "Give the kind of this note: a, b or c."
SCHEMA = {"kind": "a|b|c"}
GOVERNANCE_HEADER = "# BORG_ROUTE — Governance Layer (Uncompactable)"


def _kind(req, text):
    return {"kind": "a"}


def _answer(req):
    return {"kind": "a"}


@pytest.fixture
async def pk(tmp_path, monkeypatch):
    from src.db.schema import create_all

    db = str(tmp_path / "pack.db")
    await create_all(db)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-pack")
    monkeypatch.setenv("BORG_PACK_MAX_ITEMS", "20")
    for name in ("ANSWERER_MAX_TOKENS", "BORG_LANE_MODE", "BASELINE_MODEL_ID", "BORG_LOCAL_HOSTS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return {"db": db, "mp": monkeypatch}


def _task(items, model="openrouter/stub/cheap", endpoint="https://openrouter.ai/api",
          schema=None, limit=20) -> WrappedTask:
    return WrappedTask(
        envelope_id=uuid.uuid4(), run_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
        type="apply_to_each", current_depth=1, max_depth=5, level_path="1",
        instruction=TASK, governance_instruction=OPERATOR,
        output_schema=schema or SCHEMA,
        dependency_input={"items": items, "concurrency_limit": limit},
        model_endpoint=endpoint, model_name=model,
    )


async def _run(pk, task, **stub_kwargs):
    stub_kwargs.setdefault("pack_item_for", _kind)
    stub_kwargs.setdefault("answer_for", _answer)
    stub = StubModels(plan_nodes=[], **stub_kwargs).install(pk["mp"])
    stub.task = task
    env = await expand_and_run(task, db_path=pk["db"])
    await drain_log_writes()
    return stub, env


def _items(n, width=8):
    return [f"note {i:0{width - 5}d}" for i in range(n)]


def _model_calls(db, run_id) -> list[dict]:
    with sqlite3.connect(db) as conn:
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(
            "SELECT * FROM logs WHERE run_id=? AND event_type='model_call' ORDER BY log_id",
            (str(run_id),),
        )]
    for r in rows:
        r["decision"] = json.loads(r["decision_json"]) if r.get("decision_json") else None
    return rows


def _decisions(db, run_id, mode) -> list[dict]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE run_id=? AND event_type='orchestrator_decision'",
            (str(run_id),),
        ).fetchall()
    out = [json.loads(r[0]) for r in rows if r[0]]
    return [d for d in out if d.get("mode") == mode]


def _error_codes(db) -> list[str]:
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT decision_json FROM logs WHERE event_type='error'").fetchall()
    return [json.loads(r[0]).get("code") for r in rows if r[0]]


def _texts_in(req) -> set:
    return set(req.pack_items.values())


# -- Done when: 200 small items, first pack alone (23, 23f c) -------------------

async def test_200_small_items_make_10_calls_with_the_first_pack_posted_alone(pk):
    items = _items(200)
    stub, env = await _run(pk, _task(items), delay_for=lambda r: 0.005)

    packed = stub.of_kind("packed")
    assert len(packed) == 10
    assert stub.of_kind("answerer") == []
    assert [len(r.pack_ids) for r in packed] == [20] * 10
    # The first pack went alone: it was the only request in flight, and every
    # other request arrived after it had finished.
    first = packed[0]
    assert first.seq == 0 and first.in_flight_at_start == 1
    assert all(r.finished_before_start >= 1 for r in stub.requests[1:])
    # Items keep index order inside and across packs.
    assert list(first.pack_items.values()) == items[:20]

    out = env.output
    assert out["total"] == 200 and out["success_count"] == 200 and out["failures"] == []
    assert [r["index"] for r in out["results"]] == list(range(200))
    assert set(out["results"][0]) == {"index", "item", "output", "confidence"}
    assert out["results"][0]["output"] == {"kind": "a"} and out["results"][0]["confidence"] == 0.9
    assert set(out) == {"results", "failures", "total", "success_count", "failure_count"}


async def test_a_far_too_low_estimate_pays_for_one_cut_call_not_ten(pk):
    # The stand-in writes 300 tokens per item; the estimate is about 24, so 20
    # items look like 960 tokens x2 under the 4,000 cap and really need 6,000.
    stub, env = await _run(pk, _task(_items(200)), packed_tokens_per_item=300)

    packed = stub.of_kind("packed")
    assert {r.max_tokens for r in packed} == {4000}
    cut = [r for r in packed if 300 * len(r.pack_ids) > r.max_tokens]
    assert len(cut) == 1, [len(r.pack_ids) for r in packed]
    assert len(packed[0].pack_ids) == 20 and len(packed[1].pack_ids) == 10
    # Packs after the observation: 300 x 2 = 600 tokens per item under a 4,000 cap.
    assert all(len(r.pack_ids) <= 6 for r in packed[2:])
    assert env.output["success_count"] == 200


async def test_an_observation_that_allows_the_full_20_gets_the_full_20(pk):
    # Free text: expected output per item is the item's own size (149 tokens),
    # x2 headroom -> 13 items fit the 4,000 cap at first. The stand-in then
    # writes 50 tokens for the whole pack, so the rest are packed 20 at a time.
    items = [f"{i:03d} " + "w" * 592 for i in range(60)]
    stub, env = await _run(
        pk, _task(items, schema={"summary": "string"}),
        pack_item_for=lambda r, t: {"summary": "short"},
    )
    packed = stub.of_kind("packed")
    assert [len(r.pack_ids) for r in packed] == [13, 20, 20, 7]
    assert env.output["success_count"] == 60


# -- Done when: reply reading (23, findings 2 and 6) -----------------------------

def _values(req, ids=None):
    return {iid: {"status": "resolved", "confidence": 0.8, "output": {"kind": "b"}}
            for iid in (ids if ids is not None else req.pack_ids)}


async def test_a_reply_missing_two_items_retries_exactly_those_two(pk):
    items = _items(20)
    stub, env = await _run(pk, _task(items), pack_for=lambda r: _values(r, r.pack_ids[2:]))

    singles = stub.of_kind("answerer")
    assert len(singles) == 2
    assert {items[0] in r.system for r in singles} == {True, False}
    assert any(items[1] in r.system for r in singles)
    assert len(stub.of_kind("packed")) == 1
    out = env.output
    assert out["success_count"] == 20
    by_index = {r["index"]: r for r in out["results"]}
    assert by_index[0]["output"] == {"kind": "a"}      # from its single call
    assert by_index[5]["output"] == {"kind": "b"}      # from the pack


@pytest.mark.parametrize("fenced", [False, True])
async def test_unknown_and_duplicated_ids_are_never_accepted(pk, fenced):
    items = _items(5)

    def reply(req):
        ids = req.pack_ids
        good = {"status": "resolved", "confidence": 0.8, "output": {"kind": "b"}}
        body = ", ".join(
            [f'"{ids[0]}": {json.dumps(good)}', f'"{ids[0]}": {json.dumps(good)}',
             f'"zzzzz": {json.dumps(good)}']
            + [f'"{i}": {json.dumps(good)}' for i in ids[2:]]
        )
        text = "{" + body + "}"
        return f"Here you go:\n```json\n{text}\n```\n" if fenced else text

    stub, env = await _run(pk, _task(items), pack_for=reply)
    singles = stub.of_kind("answerer")
    # item 0 (duplicated) and item 1 (its answer only under an unknown id) go alone.
    assert len(singles) == 2
    assert sorted(i for i, t in enumerate(items) if any(t in r.system for r in singles)) == [0, 1]
    out = env.output
    assert {r["index"]: r["output"]["kind"] for r in out["results"]} == {0: "a", 1: "a", 2: "b", 3: "b", 4: "b"}


@pytest.mark.parametrize("shape", ["numbered", "item_n", "other_pack"])
async def test_a_shifted_or_renumbered_reply_accepts_nothing_under_a_wrong_id(pk, shape):
    from src.orchestrator.packing import item_ids

    items = _items(40)
    task = _task(items)
    all_ids = item_ids(task.envelope_id, 40)

    def reply(req):
        if req.pack_ids[0] != all_ids[0]:
            return _values(req)
        if shape == "numbered":
            keys = [str(k + 1) for k in range(len(req.pack_ids))]
        elif shape == "item_n":
            keys = [f"item_{k}" for k in range(len(req.pack_ids))]
        else:  # the next pack's ids: valid in the step, wrong in this pack
            keys = all_ids[20:40]
        return {k: {"status": "resolved", "confidence": 0.8, "output": {"kind": "c"}} for k in keys}

    stub, env = await _run(pk, task, pack_for=reply)
    first = stub.of_kind("packed")[0]
    assert first.pack_ids == all_ids[:20]
    # Nothing from the first reply was accepted: its 20 items all ran alone.
    assert len(stub.of_kind("answerer")) == 20
    kinds = {r["index"]: r["output"]["kind"] for r in env.output["results"]}
    assert all(kinds[i] == "a" for i in range(20))
    assert "c" not in kinds.values()


@pytest.mark.parametrize("reply", [
    {"status": "resolved", "confidence": 0.9, "output": {"kind": "a"}},
    [{"kind": "a"}],
    "not json at all",
])
async def test_a_reply_of_the_wrong_shape_sends_every_item_alone(pk, reply):
    stub, env = await _run(pk, _task(_items(6)), pack_for=lambda r: reply)
    assert len(stub.of_kind("answerer")) == 6
    assert env.output["success_count"] == 6


async def test_a_value_failing_the_schema_is_retried_alone(pk):
    def reply(req):
        out = _values(req)
        out[req.pack_ids[3]] = {"status": "resolved", "confidence": 0.8, "output": {"other": 1}}
        out[req.pack_ids[4]] = {"status": "failed", "confidence": 0.1, "output": {}}  # no reason
        return out

    stub, env = await _run(pk, _task(_items(6)), pack_for=reply)
    assert len(stub.of_kind("answerer")) == 2


async def test_a_declined_item_is_ans010_and_not_retried(pk):
    def reply(req):
        out = _values(req)
        out[req.pack_ids[2]] = {"status": "failed", "confidence": 0.0, "output": {},
                                "failure_reason": "cannot tell"}
        return out

    stub, env = await _run(pk, _task(_items(6)), pack_for=reply)
    assert stub.of_kind("answerer") == []
    fails = env.output["failures"]
    assert [f["index"] for f in fails] == [2]
    assert fails[0]["reason"] == "cannot tell" and fails[0]["model_id"] == "openrouter/stub/cheap"
    assert env._model_reason_indexes == {2}


# -- Done when: oversized item ------------------------------------------------------

async def test_an_oversized_item_runs_alone_and_the_limit_case_is_packed(pk):
    at_limit = "x" * 1601      # serialised 1,603 characters: 400 tokens
    over = "y" * 1602          # serialised 1,604 characters: 401 tokens
    items = ["note 1", at_limit, over, "note 2"]
    stub, env = await _run(pk, _task(items))

    packed = stub.of_kind("packed")
    assert len(packed) == 1
    assert _texts_in(packed[0]) == {"note 1", at_limit, "note 2"}
    singles = stub.of_kind("answerer")
    assert len(singles) == 1 and over in singles[0].system and "# Packed Task" not in singles[0].system
    assert env.output["success_count"] == 4


# -- Done when: cost split ------------------------------------------------------------

async def test_split_costs_sum_exactly_to_each_call(pk):
    stub, env = await _run(pk, _task(_items(45)))
    rows = _model_calls(pk["db"], env_run_id(stub, env))
    assert len(rows) == 45
    calls: dict = {}
    for r in rows:
        p = r["decision"]["pack"]
        calls.setdefault(p["call"], []).append((r, p))
    assert len(calls) == len(stub.of_kind("packed")) == 3
    for parts in calls.values():
        firsts = [p for _r, p in parts if p["first"]]
        assert len(firsts) == 1
        head = firsts[0]
        assert sum(r["tokens_input"] for r, _p in parts) == head["call_tokens_in"]
        assert sum(r["tokens_output"] for r, _p in parts) == head["call_tokens_out"]
        assert sum(p["share_picodollars"] for _r, p in parts) == head["call_picodollars"]
        assert sum(p["share_baseline_picodollars"] for _r, p in parts) == head["call_baseline_picodollars"]
        assert all("call_picodollars" not in p for _r, p in parts if not p["first"])
        for r, p in parts:
            assert r["dollars"] == p["share_picodollars"] / 1e12
    # The step's totals are the sum of its item events.
    assert env.dollars == pytest.approx(sum(r["dollars"] for r in rows), abs=1e-15)
    assert env.tokens_used["input"] == sum(r["tokens_input"] for r in rows)
    assert env.tokens_used["output"] == sum(r["tokens_output"] for r in rows)
    # The usage the stand-in reported is what the shares add up to.
    heads = [p for parts in calls.values() for _r, p in parts if p["first"]]
    assert sum(h["call_tokens_in"] for h in heads) == \
        sum(max(1, len(r.system) // 4) for r in stub.of_kind("packed"))


def env_run_id(stub, env):
    """The run id the expander logged under (the task's)."""
    return stub.task.run_id


async def test_no_baseline_model_means_none_not_zero(pk):
    from src.registry import cost

    async def none(*a, **k):
        return None

    pk["mp"].setattr(cost, "resolve_baseline_model", none)
    stub, env = await _run(pk, _task(_items(5)))
    rows = _model_calls(pk["db"], env_run_id(stub, env))
    assert rows and all(r["baseline_dollars"] is None for r in rows)
    assert all(r["decision"]["pack"]["share_baseline_picodollars"] is None for r in rows)


async def test_cut_calls_are_paid_and_counted_in_the_steps_totals(pk):
    stub, env = await _run(pk, _task(_items(200)), packed_tokens_per_item=300)
    rows = _model_calls(pk["db"], env_run_id(stub, env))
    cut_rows = [r for r in rows if r["decision"]["pack"]["cut"]]
    assert len(cut_rows) == 20 and all(not r["decision"]["pack"]["accepted"] for r in cut_rows)
    assert env.dollars == pytest.approx(sum(r["dollars"] for r in rows), abs=1e-15)
    assert env.tokens_used["output"] == sum(r["tokens_output"] for r in rows)


async def test_the_worst_case_cut_chain_of_a_pack_of_20_is_59_posts(pk):
    stub, env = await _run(
        pk, _task(_items(20)), packed_tokens_per_item=10_000,
        finish_for=lambda r: "length" if r.kind == "answerer" and r.max_tokens == 4000 else "stop",
    )
    assert len(stub.of_kind("packed")) == 19
    assert len(stub.of_kind("answerer")) == 40
    assert len(stub.requests) == 59
    assert env.output["success_count"] == 20


# -- Done when: local-bound calls are never packed (23b) ------------------------------

async def test_a_call_to_a_local_model_is_never_packed(pk):
    matrix = three_tier_matrix()
    matrix.upsert(stub_model("local", 7.0, 0.0, 0.0, source="ollama"))
    pk["mp"].setattr(engine, "_shared_matrix", matrix)
    stub, env = await _run(pk, _task(_items(25), model="ollama/stub/local",
                                     endpoint="http://127.0.0.1:11434"))
    assert stub.of_kind("packed") == []
    assert len(stub.of_kind("answerer")) == 25


async def test_a_priced_row_served_from_a_local_host_is_never_packed(pk):
    matrix = three_tier_matrix()
    matrix.upsert(ModelInfo(
        model_id="custom_box/model", source="custom_key", endpoint="http://localhost:8000",
        context_length=200_000, pricing=Pricing(input_per_token=1e-7, output_per_token=2e-7),
        task_scores={"extraction": 8.0}, checked=True,
    ))
    pk["mp"].setattr(engine, "_shared_matrix", matrix)
    stub, env = await _run(pk, _task(_items(25), model="custom_box/model",
                                     endpoint="http://localhost:8000"))
    assert stub.of_kind("packed") == []
    assert len(stub.of_kind("answerer")) == 25


# -- Done when: a timeout gives single calls, packing off for the step (23c) ----------

async def test_a_first_pack_that_times_out_runs_alone_and_packing_stops(pk):
    def reply(req):
        return httpx.ReadTimeout("slow")

    stub, env = await _run(pk, _task(_items(60)), pack_for=reply)
    assert len(stub.of_kind("packed")) == 1
    assert len(stub.of_kind("answerer")) == 60
    assert env.output["success_count"] == 60


async def test_a_later_pack_that_times_out_stops_packs_not_yet_posted(pk):
    seen = []

    def reply(req):
        seen.append(req)
        if len(seen) == 2:
            return httpx.ReadTimeout("slow")
        return _values(req)

    stub, env = await _run(pk, _task(_items(80), limit=1), pack_for=reply)
    assert len(stub.of_kind("packed")) == 2
    assert len(stub.of_kind("answerer")) == 60
    assert env.output["success_count"] == 80


async def test_a_post_that_raises_sends_its_items_alone(pk):
    calls = []

    def reply(req):
        calls.append(req)
        return httpx.ConnectError("down") if len(calls) == 1 else _values(req)

    stub, env = await _run(pk, _task(_items(8)), pack_for=reply)
    assert len(stub.of_kind("packed")) == 1
    assert len(stub.of_kind("answerer")) == 8
    assert env.output["success_count"] == 8


# -- the rest of the note's tests -------------------------------------------------------

async def test_non_ascii_items_are_sent_unescaped(pk):
    items = ["café naïve", "東京 の メモ", "emoji 🙂 note"]
    stub, env = await _run(pk, _task(items))
    req = stub.of_kind("packed")[0]
    for t in items:
        assert json.dumps(t, ensure_ascii=False) in req.system
    assert "\\u" not in req.system
    assert set(req.pack_items.values()) == set(items)


async def test_the_concurrency_limit_bounds_posts_in_flight(pk):
    stub, env = await _run(pk, _task(_items(100), limit=2), delay_for=lambda r: 0.02)
    assert len(stub.of_kind("packed")) == 5
    assert stub.max_in_flight == 2


@pytest.mark.parametrize("value", ["0", "1"])
async def test_packing_off_makes_one_call_per_item(pk, value):
    pk["mp"].setenv("BORG_PACK_MAX_ITEMS", value)
    stub, env = await _run(pk, _task(_items(30)))
    assert stub.of_kind("packed") == []
    assert len(stub.of_kind("answerer")) == 30
    assert _model_calls(pk["db"], env_run_id(stub, env))[0]["decision"] is None


async def test_an_invalid_setting_turns_packing_off_with_a_warning(pk):
    pk["mp"].setenv("BORG_PACK_MAX_ITEMS", "lots")
    stub, env = await _run(pk, _task(_items(5)))
    assert stub.of_kind("packed") == []
    assert "BRT-APE-007" in _error_codes(pk["db"])


async def test_each_post_writes_a_pack_outcome_with_its_counts(pk):
    def reply(req):
        out = _values(req, req.pack_ids[2:])
        out[req.pack_ids[2]] = {"status": "failed", "confidence": 0.0, "output": {},
                                "failure_reason": "no"}
        out["qqqqq"] = out[req.pack_ids[3]]
        return out

    stub, env = await _run(pk, _task(_items(10)), pack_for=reply)
    outcomes = _decisions(pk["db"], env_run_id(stub, env), "pack_outcome")
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o["step"] == str(env.envelope_id)
    assert (o["items"], o["accepted"], o["declined"], o["missing"], o["unknown_id"], o["alone"]) == \
        (10, 8, 1, 2, 1, 2)
    assert o["cut"] is False and o["timed_out"] is False and o["raised"] is False


async def test_call_counters_count_posts_not_items(pk):
    from bench.run_bench import read_run_tree
    from src.api.locality_view import calls_left_machine

    stub, env = await _run(pk, _task(_items(45)))
    run_id = env_run_id(stub, env)
    assert read_run_tree(pk["db"], str(run_id))["model_calls"] == 3

    with sqlite3.connect(pk["db"]) as conn:
        conn.row_factory = sqlite3.Row
        events = [dict(r) for r in conn.execute("SELECT * FROM logs WHERE run_id=?", (str(run_id),))]
    left = calls_left_machine(lane_mode=None, outbound=[], events=events,
                              matrix=engine._shared_matrix, rules=([], []), envelopes=[])
    assert left == {"count": 3, "source": "locality"}


async def test_cost_summary_counts_posts(pk):
    from src.api.logs import cost_summary

    pk["db"] = "data/borg_route.db"   # the conftest maps it to this test's temp database
    stub, env = await _run(pk, _task(_items(45)))
    body = json.loads((await cost_summary()).body)
    assert body["totals"]["total_calls"] == 3
    assert {r["model_id"]: r["call_count"] for r in body["by_model"]} == {"openrouter/stub/cheap": 3}


def test_item_ids_are_opaque_unique_and_stable():
    from src.orchestrator.packing import item_ids

    step = uuid.UUID("12345678-1234-5678-1234-567812345678")
    ids = item_ids(step, 500)
    assert len(set(ids)) == 500
    assert ids == item_ids(step, 500)
    assert all(len(i) >= 5 and i.isalnum() and i == i.lower() for i in ids)
    assert ids[:3] != item_ids(uuid.uuid4(), 3)
    assert not any(i.startswith("item") or i.isdigit() for i in ids)


@pytest.mark.parametrize("value", ["0", "1"])
def test_with_packing_off_request_bodies_match_the_tree_before_packing(tmp_path, value):
    """The two-tree proof, pinned: the fixed job of scripts/compare_request_bodies.py,
    run in this tree with packing off, sends exactly the bodies the tree before
    packing sent (their sha256, recorded from 773bae1)."""
    import hashlib
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    out = tmp_path / "bodies.json"
    proc = subprocess.run(
        [sys.executable, str(root / "scripts" / "compare_request_bodies.py"),
         "--run", str(root), "--pack", value, "--out", str(out)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    assert proc.returncode == 0, proc.stderr[-2000:]
    bodies = json.loads(out.read_text(encoding="utf-8"))
    pinned = json.loads((root / "tests" / "fixtures" / "packing_off_request_bodies.json")
                        .read_text(encoding="utf-8"))["sha256"]
    assert sorted(hashlib.sha256(b.encode("utf-8")).hexdigest() for b in bodies) == pinned


# -- review round 1 of 40ee228 ------------------------------------------------------

def _grow_at_send(pk, indexes, extra=1700):
    """Item texts that measure small when packs are formed but over 400 tokens
    when the request is built (as a document item can, once JSON-escaped)."""
    from src.orchestrator import packing

    real = packing.StepPacker._item_text

    def grown(self, i):
        text = real(self, i)
        return text + " " + "q" * extra if i in indexes else text

    pk["mp"].setattr(packing.StepPacker, "_item_text", grown)


def _filed_indexes(env) -> list:
    return sorted([r["index"] for r in env.output["results"]] + [f["index"] for f in env.output["failures"]])


@pytest.mark.parametrize("n", [6, 25])
async def test_an_item_that_leaves_at_send_time_is_never_posted_again_after_a_cut(pk, n):
    _grow_at_send(pk, {1})
    stub, env = await _run(pk, _task(_items(n)), packed_tokens_per_item=1000)
    assert _filed_indexes(env) == list(range(n))
    # Item 1 went alone exactly once (the single path sends its own text) and
    # was never in a pack that was posted.
    one = _items(n)[1]
    assert len([r for r in stub.of_kind("answerer") if one in r.system]) == 1
    assert not any(one in t for r in stub.of_kind("packed") for t in r.pack_items.values())
    rows = _model_calls(pk["db"], env_run_id(stub, env))
    assert len([r for r in rows if r["decision"] is None]) == len(stub.of_kind("answerer"))
    assert env.dollars == pytest.approx(sum(r["dollars"] for r in rows), abs=1e-15)


async def test_when_the_whole_first_pack_leaves_at_send_time_the_next_pack_goes_alone_first(pk):
    # Estimate: 13 items per pack (free text, 149 tokens each, x2 under 4,000).
    # Items 0-12 measure over 400 at send time, so the first pack is never
    # posted. The next pack must be the first pack (posted alone, then the
    # rest sized from its observation: 20), not packs formed from the estimate.
    items = [f"{i:03d} " + "w" * 592 for i in range(60)]
    _grow_at_send(pk, set(range(13)))
    stub, env = await _run(
        pk, _task(items, schema={"summary": "string"}),
        pack_item_for=lambda r, t: {"summary": "short"},
        answer_for=lambda r: {"summary": "short"},
    )
    assert [len(r.pack_ids) for r in stub.of_kind("packed")] == [13, 20, 14]
    assert _filed_indexes(env) == list(range(60))


async def test_a_post_that_raises_records_only_an_error_code(pk):
    def reply(req):
        return httpx.ConnectError("https://provider.example/v1 said: secret body text")

    stub, env = await _run(pk, _task(_items(4)), pack_for=reply)
    outcomes = _decisions(pk["db"], env_run_id(stub, env), "pack_outcome")
    assert len(outcomes) == 1
    assert outcomes[0]["error_code"] == "BRT-ANS-002"
    assert "error" not in outcomes[0]
    assert "secret body text" not in json.dumps(outcomes[0])


async def test_retried_singles_carry_their_packing_off_bodies(pk):
    import random

    def seeded():
        rng = random.Random(7)
        pk["mp"].setattr(uuid, "uuid4", lambda: uuid.UUID(int=rng.getrandbits(128), version=4))

    step = uuid.UUID(int=1, version=4)
    items = _items(20)

    def task():
        t = _task(items)
        return t.model_copy(update={"envelope_id": step, "run_id": step, "subtask_id": step})

    seeded()
    off_stub = StubModels(plan_nodes=[], answer_for=_answer).install(pk["mp"])
    pk["mp"].setenv("BORG_PACK_MAX_ITEMS", "0")
    await expand_and_run(task(), db_path=pk["db"])
    off_bodies = {next(t for t in items if t in r.system): r.body for r in off_stub.of_kind("answerer")}

    seeded()
    pk["mp"].setenv("BORG_PACK_MAX_ITEMS", "20")
    stub = StubModels(plan_nodes=[], answer_for=_answer, pack_item_for=_kind,
                      pack_for=lambda r: _values(r, r.pack_ids[2:])).install(pk["mp"])
    await expand_and_run(task(), db_path=pk["db"])
    await drain_log_writes()
    singles = stub.of_kind("answerer")
    assert len(singles) == 2
    for r in singles:
        text = next(t for t in items if t in r.system)
        assert r.body == off_bodies[text]


async def test_a_baseline_without_prices_gives_zero_as_on_the_single_path(pk):
    from src.registry import cost

    no_price = ModelInfo(model_id="openrouter/stub/free", source="openrouter",
                         endpoint="https://openrouter.ai/api", context_length=200_000,
                         pricing=None, task_scores={"reasoning": 9.9}, checked=True)

    async def baseline(*a, **k):
        return no_price

    pk["mp"].setattr(cost, "resolve_baseline_model", baseline)
    stub, env = await _run(pk, _task(_items(5)))
    rows = _model_calls(pk["db"], env_run_id(stub, env))
    assert rows and all(r["baseline_dollars"] == 0.0 for r in rows)
    assert all(r["decision"]["pack"]["share_baseline_picodollars"] == 0 for r in rows)


async def test_the_semaphore_is_held_only_around_the_http_post(pk):
    from src.logging import writer
    from src.orchestrator import packing

    seen = {}
    real_run = packing.StepPacker.run

    async def run(self):
        seen["packer"] = self
        return await real_run(self)

    held = []
    real_write = writer.write_event

    async def write(event_type, *a, **k):
        if event_type == "model_call" and "packer" in seen and isinstance(k.get("decision"), dict) \
                and "pack" in k["decision"]:
            held.append(seen["packer"].semaphore.locked())
        return await real_write(event_type, *a, **k)

    pk["mp"].setattr(packing.StepPacker, "run", run)
    pk["mp"].setattr(writer, "write_event", write)
    stub, env = await _run(pk, _task(_items(6), limit=1))
    assert held and not any(held)
