"""Planner ruling 35e: every model call is priced and logged, rejected replies too.

In bench trial 32g, job C's first compose reply was rejected (BRT-ORC-029) and
its ``orchestrator_decision`` event never reached ``logs``, so the run's cost
and the receipt left out a paid call. The reply parsed as JSON that is not an
object (a list or a bare string); building the event read ``.get`` on it, the
AttributeError was swallowed with the event, and the reply went on to 029.

The real engine, executor and router run against the scripted stand-in model
server. Each test checks the receipt against the stand-in's own record: the
digest's ``cost_dollars`` equals the price of every request the stand-in
answered, so a call the log leaves out fails the test.
"""
import json
import sqlite3

import httpx
import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

# Per-token prices of the stand-in models (three_tier_matrix).
PRICES = {
    "stub/top": (5e-6, 2.5e-5),
    "stub/mid": (1e-6, 3e-6),
    "stub/cheap": (1e-7, 2e-7),
}
SMALL = ["Kettle: boils fast", "Toaster: burnt everything", "Blender: loud but fine"]
GOOD = {"response": "Second reply, the final answer."}


def _seq(*replies):
    """compose_for answering the n-th compose call with ``replies[n]``."""
    calls = []

    def compose_for(req):
        calls.append(req)
        return replies[min(len(calls), len(replies)) - 1]
    return compose_for


def _rate(items=SMALL):
    return {"type": "apply_to_each", "id": "rate", "instruction": "Rate this review.",
            "task_type": "extraction", "tier": "light", "items": items,
            "output_schema": {"sentiment": "string"}}


def _plain(node_id="plain", instruction="Say hello."):
    return {"type": "answerer", "id": node_id, "instruction": instruction,
            "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}}


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "priced.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-priced")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.delenv("ORCHESTRATOR_MAX_TOKENS", raising=False)
    monkeypatch.delenv("BASELINE_MODEL_ID", raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _stub_dollars(stub):
    """What every request the stand-in answered cost, from its own record.
    A request it failed (``unbilled``) or answered with no usage reports no
    tokens, so it costs nothing."""
    total = 0.0
    for req in stub.requests:
        if getattr(req, "unbilled", False):
            continue
        pin, pout = PRICES[req.model.removeprefix("openrouter/")]
        total += max(1, len(req.system) // 4) * pin + stub.completion_tokens * pout
    return total


def _events(db_path, run_id):
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT event_type, tokens_input, tokens_output, dollars, decision_json "
            "FROM logs WHERE run_id=? ORDER BY log_id", (run_id,)).fetchall()
    return [(et, tin, tout, d, json.loads(dj) if dj else {}) for et, tin, tout, d, dj in rows]


def _priced(db_path, run_id, mode):
    return [e for e in _events(db_path, run_id)
            if e[0] == "orchestrator_decision" and e[4].get("mode") == mode
            and e[3] is not None]


def _digest_cost(db_path, run_id):
    with sqlite3.connect(db_path) as conn:
        return conn.execute("SELECT cost_dollars FROM run_digests WHERE run_id=?",
                            (run_id,)).fetchone()[0]


async def _run(stub, mp, db_path, message="Say hello."):
    stub.install(mp)
    result = await run_pipeline(message, db_path=db_path)
    await drain_log_writes()
    return result


def _assert_receipt_counts_every_call(stub, result, db_path):
    """The digest (the receipt's cost) and the run's reply both equal the
    price of every request the stand-in answered."""
    expected = _stub_dollars(stub)
    run_id = result["run_id"]
    assert _digest_cost(db_path, run_id) == pytest.approx(expected, rel=1e-9)
    assert result["cost_dollars"] == pytest.approx(expected, rel=1e-9)


def _call_id_of(db_path, run_id, parsed):
    """The ``call_id`` of the one priced compose reply that parsed to ``parsed``."""
    hits = [e[4]["call_id"] for e in _priced(db_path, run_id, "compose")
            if e[4].get("parsed") == parsed]
    assert len(hits) == 1, (parsed, hits)
    return hits[0]


def _rejections(db_path, run_id, code):
    return [e[4] for e in _events(db_path, run_id)
            if e[4].get("code") == code and e[4].get("rejected") is True]


def _assert_rejection_names_the_reply(db_path, run_id, code, rejected_parsed):
    """Every rejection event with ``code`` names the reply it rejected:
    ``rejected_call`` is the ``call_id`` of the priced reply that parsed to
    ``rejected_parsed``, never another compose call's."""
    want = _call_id_of(db_path, run_id, rejected_parsed)
    rejections = _rejections(db_path, run_id, code)
    assert rejections, f"no rejection event with {code}"
    assert [r["rejected_call"] for r in rejections] == [want] * len(rejections)


# --- 029: no final answer, then a good one ----------------------------------------------

@pytest.mark.parametrize("first", [
    ["1. 14 March 2027", "2. 412"],          # a JSON list (trial 32g, job C)
    "1. 14 March 2027\n2. 412",              # a bare JSON string
    {"action": "compose"},                   # an object with no final text
], ids=["list", "string", "object"])
@pytest.mark.asyncio
async def test_029_rejected_reply_is_priced_logged_and_counted(env, first):
    db_path, mp = env
    raw = json.dumps(first)                  # sent as is: a str reply is the raw content
    stub = StubModels(plan_nodes=[_plain()], compose_for=_seq(raw, GOOD))
    result = await _run(stub, mp, db_path)
    assert result["status"] == "completed", result.get("error")
    assert result["response"] == GOOD["response"]
    run_id = result["run_id"]
    assert len(stub.of_kind("compose")) == 2
    composes = _priced(db_path, run_id, "compose")
    assert len(composes) == 2, [e[4] for e in _events(db_path, run_id)]
    assert all(e[3] > 0 for e in composes)
    if not isinstance(first, dict):
        typed = [e[4].get("reply_type") for e in composes if e[4].get("reply_type")]
        assert typed == [type(first).__name__]
    _assert_rejection_names_the_reply(db_path, run_id, "BRT-ORC-029", first)
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.asyncio
async def test_two_replies_with_no_final_answer_are_both_priced_then_salvaged(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_plain()], compose_for=_seq('["a"]', '["b"]'))
    result = await _run(stub, mp, db_path)
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    run_id = result["run_id"]
    assert len(_priced(db_path, run_id, "compose")) == 2
    _assert_rejection_names_the_reply(db_path, run_id, "BRT-ORC-029", ["a"])
    _assert_rejection_names_the_reply(db_path, run_id, "BRT-ORC-030", ["b"])
    _assert_receipt_counts_every_call(stub, result, db_path)


# --- the shared repair re-ask (ruling 32e): 032, 037, 038, 040, 041 -----------------------

LONG = "line " * 600                         # a 3,000-character step output


class _FailToaster(StubModels):
    """The Toaster item is reported failed by the model itself."""

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        req = self.requests[-1]
        if req.kind == "answerer" and "Toaster" in req.system:
            resp._body["choices"][0]["message"]["content"] = json.dumps(
                {"status": "failed", "confidence": 0.0, "output": {},
                 "failure_reason": "I cannot read this review."})
        return resp


REPAIR_CASES = {
    "032": (StubModels, [_rate()], {"response": "@@ROWS:nope@@"}),
    "037": (StubModels, [_rate()], {"response": "@@ROWS:rate | @@"}),
    "038": (StubModels, [_rate()], {"response": '[{"item": "x"}, @@ROWS:rate@@]'}),
    "040": (StubModels, [_plain("n1", "Write the files.")],
            {"response": "All files are in the stage result (n1)."}),
    "041": (_FailToaster, [_rate()],
            {"response": '[{"item": "Toaster: burnt everything", "sentiment": "positive"}]'}),
}


@pytest.mark.parametrize("code", sorted(REPAIR_CASES))
@pytest.mark.asyncio
async def test_repair_reask_rejected_reply_is_priced_and_counted(env, code):
    db_path, mp = env
    cls, nodes, first = REPAIR_CASES[code]
    answer = (lambda r: {"result": LONG}) if code == "040" else (lambda r: {"sentiment": "positive"})
    stub = cls(plan_nodes=nodes, answer_for=answer, compose_for=_seq(first, GOOD))
    result = await _run(stub, mp, db_path, "Rate each.")
    assert result["status"] == "completed", result.get("error")
    run_id = result["run_id"]
    codes = [e[4].get("code") for e in _events(db_path, run_id)]
    assert f"BRT-ORC-{code}" in codes, codes
    assert len(stub.of_kind("compose")) == 2
    composes = _priced(db_path, run_id, "compose")
    assert len(composes) == 2
    _assert_rejection_names_the_reply(db_path, run_id, f"BRT-ORC-{code}", first)
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.asyncio
async def test_repair_reask_rejected_twice_both_priced_then_salvaged(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate()], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq({"response": "@@ROWS:nope@@"},
                                       {"response": "@@rows:nope@@"}))
    result = await _run(stub, mp, db_path, "Rate each.")
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    run_id = result["run_id"]
    assert len(_priced(db_path, run_id, "compose")) == 2
    first = _call_id_of(db_path, run_id, {"response": "@@ROWS:nope@@"})
    second = _call_id_of(db_path, run_id, {"response": "@@rows:nope@@"})
    by_kind = {("salvage" if r.get("severity") == "warning" else "reask"): r["rejected_call"]
               for r in _rejections(db_path, run_id, "BRT-ORC-032")}
    assert by_kind == {"reask": first, "salvage": second}
    _assert_receipt_counts_every_call(stub, result, db_path)


# --- the cut-off pointer retry (ruling 21, BRT-ORC-018) ----------------------------------

@pytest.mark.asyncio
async def test_pointer_retry_prices_both_attempts(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate()], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq(('{"response": "[{\\"item\\": \\"Ket', "length"),
                                       {"response": "@@ROWS:rate@@"}))
    result = await _run(stub, mp, db_path, "Rate each.")
    run_id = result["run_id"]
    assert len(stub.of_kind("compose")) == 2
    composes = _priced(db_path, run_id, "compose")
    # One event for both attempts: its tokens are their sum.
    assert len(composes) == 1 and composes[0][4]["code"] == "BRT-ORC-018"
    sent = sum(max(1, len(r.system) // 4) for r in stub.of_kind("compose"))
    assert composes[0][1] == sent
    assert composes[0][2] == 2 * stub.completion_tokens
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.asyncio
async def test_pointer_retry_cut_again_prices_both_attempts(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_rate()], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_seq(('{"response": "cut', "length"),
                                       ('{"response": "cut again', "length")))
    result = await _run(stub, mp, db_path, "Rate each.")
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    assert len(stub.of_kind("compose")) == 2
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.asyncio
async def test_failed_length_retry_still_prices_the_first_attempt(env):
    db_path, mp = env
    calls = []

    def compose_for(req):
        calls.append(req)
        if len(calls) == 1:
            return ('{"response": "cut', "length")
        return ValueError("BRT-ORC-001: stand-in transport failure")

    stub = StubModels(plan_nodes=[_plain()], compose_for=compose_for)
    result = await _run(stub, mp, db_path)
    run_id = result["run_id"]
    composes = _priced(db_path, run_id, "compose")
    assert len(composes) == 1 and composes[0][4].get("length_retry_failed") is True
    # The failed second attempt returned no usage: only the first is billed.
    first = calls[0]
    pin, pout = PRICES["stub/top"]
    assert composes[0][3] == pytest.approx(
        max(1, len(first.system) // 4) * pin + stub.completion_tokens * pout, rel=1e-9)


# --- the JSON-only retry (BRT-ORC-002) in plan, re_plan and compose -----------------------

@pytest.mark.parametrize("mode", ["plan", "re_plan", "compose"])
@pytest.mark.asyncio
async def test_json_retry_prices_the_unparseable_reply(env, mode):
    db_path, mp = env
    seen = {"n": 0}

    def once_prose(good):
        def reply(req):
            seen["n"] += 1
            return "Sure! Here is my answer, in prose." if seen["n"] == 1 else good
        return reply

    kwargs = {}
    if mode == "plan":
        kwargs["plan_content_for"] = once_prose(
            {"plan": {"nodes": [_plain()]}, "rationale": "scripted plan"})
    elif mode == "re_plan":
        kwargs["replan_for"] = once_prose({"action": "compose", "rationale": "done"})
    else:
        kwargs["compose_for"] = once_prose(GOOD)
    stub = StubModels(plan_nodes=[_plain()], **kwargs)
    result = await _run(stub, mp, db_path)
    assert result["status"] == "completed", result.get("error")
    run_id = result["run_id"]
    events = _priced(db_path, run_id, mode)
    assert len(events) == 2
    # Fire-and-forget log writes: row order is not guaranteed.
    first = [e for e in events if not e[4].get("retry")]
    retry = [e for e in events if e[4].get("retry") is True]
    assert len(first) == 1 and first[0][4]["parse_error"] and len(retry) == 1
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.parametrize("mode", ["plan", "re_plan"])
@pytest.mark.asyncio
async def test_a_non_object_plan_or_replan_reply_is_still_priced(env, mode):
    """A plan or re_plan reply that parses to a list is logged and priced,
    whatever the run then does with it."""
    db_path, mp = env
    kwargs = ({"plan_content_for": lambda r: ["not", "a", "plan"]} if mode == "plan"
              else {"replan_for": lambda r: ["not", "an", "action"]})
    stub = StubModels(plan_nodes=[_plain()], **kwargs)
    result = await _run(stub, mp, db_path)
    run_id = result["run_id"]
    events = _priced(db_path, run_id, mode)
    assert len(events) == 1 and events[0][4]["reply_type"] == "list"
    assert isinstance(events[0][4]["parsed"], list)


# --- review round 1: the salvage after a failed repair re-ask names the reply --------------

def _http_500():
    request = httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    return httpx.HTTPStatusError("500", request=request,
                                 response=httpx.Response(500, request=request))


class _Unbilled:
    """compose_for: the first compose reply, then ``fail(req)`` for every
    later call; a request answered with an exception is marked unbilled."""

    def __init__(self, first, fail):
        self.first, self.fail, self.calls = first, fail, 0

    def __call__(self, req):
        self.calls += 1
        if self.calls == 1:
            return self.first
        reply = self.fail(req)
        if isinstance(reply, Exception):
            req.unbilled = True
        return reply


@pytest.mark.parametrize("fail", ["http_500", "brt_orc_002"])
@pytest.mark.asyncio
async def test_salvage_after_a_failed_repair_reask_names_the_salvaged_reply(env, fail):
    db_path, mp = env
    first = {"response": "@@ROWS:nope@@"}
    reply = (lambda r: _http_500()) if fail == "http_500" else (lambda r: "Sure, in prose.")
    stub = StubModels(plan_nodes=[_rate()], answer_for=lambda r: {"sentiment": "ok"},
                      compose_for=_Unbilled(first, reply))
    result = await _run(stub, mp, db_path, "Rate each.")
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    run_id = result["run_id"]
    # 500: one re-ask post; 002: the re-ask and its JSON-only retry, both priced.
    assert len(stub.of_kind("compose")) == (2 if fail == "http_500" else 3)
    want = _call_id_of(db_path, run_id, first)
    rejections = _rejections(db_path, run_id, "BRT-ORC-032")
    assert sorted(r.get("severity") or "" for r in rejections) == ["", "warning"]
    assert all(r["rejected_call"] == want for r in rejections)
    _assert_receipt_counts_every_call(stub, result, db_path)


# --- review round 1: null content, null usage, a non-object answerer reply ----------------

class _Edited(StubModels):
    """Edits a reply's body after the stand-in builds it: ``edit(req, body)``."""

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        edit = getattr(self, "edit", None)
        if edit is not None:
            edit(self.requests[-1], resp._body)
        return resp


def _nth_of_kind(kind, n, change):
    """An edit applying ``change(req, body)`` to the n-th request of a kind (1-based)."""
    seen = {"n": 0}

    def edit(req, body):
        if req.kind == kind:
            seen["n"] += 1
            if seen["n"] == n:
                change(req, body)
    return edit


def _null_content(finish=None):
    def change(req, body):
        body["choices"][0]["message"]["content"] = None
        if finish:
            body["choices"][0]["finish_reason"] = finish
    return change


def _null_usage(req, body):
    body["usage"] = None
    req.unbilled = True                      # no usage: nothing can be priced


def _content(text):
    def change(req, body):
        body["choices"][0]["message"]["content"] = text
    return change


async def _edited_run(env, edit, message="Say hello.", **kwargs):
    db_path, mp = env
    kwargs.setdefault("plan_nodes", [_plain()])
    stub = _Edited(**kwargs)
    stub.edit = edit
    result = await _run(stub, mp, db_path, message)
    return stub, result, db_path


@pytest.mark.asyncio
async def test_null_compose_content_is_priced_then_json_retried(env):
    stub, result, db = await _edited_run(env, _nth_of_kind("compose", 1, _null_content()),
                                         compose_text="Final.")
    assert result["status"] == "completed", result.get("error")
    assert result["response"] == "Final."
    events = _priced(db, result["run_id"], "compose")
    assert len(events) == 2
    assert [e[4].get("content_null") for e in events].count(True) == 2  # notes cover the call
    _assert_receipt_counts_every_call(stub, result, db)


@pytest.mark.asyncio
async def test_null_compose_content_cut_then_retry_fails_prices_the_first_attempt(env):
    db_path, mp = env
    calls = []

    def compose_for(req):
        calls.append(req)
        if len(calls) == 1:
            return ("placeholder", "length")
        req.unbilled = True
        return ValueError("BRT-ORC-001: stand-in transport failure")

    stub = _Edited(plan_nodes=[_plain()], compose_for=compose_for)
    stub.edit = _nth_of_kind("compose", 1, _null_content())
    result = await _run(stub, mp, db_path)
    events = _priced(db_path, result["run_id"], "compose")
    assert len(events) == 1
    assert events[0][4]["length_retry_failed"] is True and events[0][4]["content_null"] is True
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.asyncio
async def test_null_content_on_the_json_retry_is_priced(env):
    db_path, mp = env
    stub = _Edited(plan_nodes=[_plain()],
                   compose_for=_seq("Sure, in prose.", "placeholder"))
    stub.edit = _nth_of_kind("compose", 2, _null_content())
    result = await _run(stub, mp, db_path)
    # Both attempts unparseable: the compose call fails and finished work is salvaged.
    assert result["response"].startswith(engine.SALVAGE_NOTE)
    events = _priced(db_path, result["run_id"], "compose")
    assert len(events) == 2
    retry = [e for e in events if e[4].get("retry")]
    assert len(retry) == 1 and retry[0][4]["content_null"] is True
    _assert_receipt_counts_every_call(stub, result, db_path)


@pytest.mark.parametrize("kind", ["plan", "compose"])
@pytest.mark.asyncio
async def test_null_usage_on_an_orchestrator_call_never_crashes(env, kind):
    stub, result, db = await _edited_run(env, _nth_of_kind(kind, 1, _null_usage),
                                         compose_text="Final.")
    assert result["status"] == "completed", result.get("error")
    events = _priced(db, result["run_id"], kind)
    assert len(events) == 1
    _et, tin, tout, dollars, decision = events[0]
    assert (tin, tout, dollars) == (0, 0, 0.0) and decision["usage_missing"] is True
    _assert_receipt_counts_every_call(stub, result, db)


def _answerer_events(db_path, run_id):
    return [e for e in _events(db_path, run_id) if e[0] == "model_call"]


@pytest.mark.asyncio
async def test_non_object_answerer_reply_is_priced_then_the_step_fails(env):
    stub, result, db = await _edited_run(
        env, _nth_of_kind("answerer", 1,
                          _content('[{"file": "a.txt", "category": "billing"}]')),
        compose_text="Final.")
    assert result["status"] == "completed", result.get("error")
    calls = _answerer_events(db, result["run_id"])
    assert len(calls) == 1 and calls[0][3] > 0
    assert calls[0][4] == {"code": "BRT-ANS-003", "reply_type": "list"}
    _assert_receipt_counts_every_call(stub, result, db)


@pytest.mark.parametrize("finish", [None, "length"])
@pytest.mark.asyncio
async def test_null_answerer_content_is_priced_then_the_step_fails(env, finish):
    def edit(req, body):
        if req.kind == "answerer":
            _null_content(finish)(req, body)

    stub, result, db = await _edited_run(env, edit, compose_text="Final.")
    assert result["status"] == "completed", result.get("error")
    calls = _answerer_events(db, result["run_id"])
    assert len(calls) == 1 and calls[0][3] > 0
    if finish == "length":
        assert calls[0][4]["code"] == "BRT-ANS-009"
    _assert_receipt_counts_every_call(stub, result, db)


@pytest.mark.asyncio
async def test_null_answerer_usage_never_crashes(env):
    stub, result, db = await _edited_run(env, _nth_of_kind("answerer", 1, _null_usage),
                                         compose_text="Final.")
    assert result["status"] == "completed", result.get("error")
    calls = _answerer_events(db, result["run_id"])
    assert len(calls) == 1 and (calls[0][1], calls[0][2], calls[0][3]) == (0, 0, 0.0)
    _assert_receipt_counts_every_call(stub, result, db)


@pytest.mark.asyncio
async def test_null_packed_content_is_priced_and_its_items_run_alone(env):
    db_path, mp = env
    mp.setenv("BORG_PACK_MAX_ITEMS", "20")
    stub, result, db = await _edited_run(env, _nth_of_kind("packed", 1, _null_content()),
                                         message="Rate each.", plan_nodes=[_rate()],
                                         answer_for=lambda r: {"sentiment": "ok"},
                                         compose_text="Final.")
    assert result["status"] == "completed", result.get("error")
    assert len(stub.of_kind("packed")) == 1
    assert len(stub.of_kind("answerer")) == len(SMALL)       # each item ran alone
    packed = [e for e in _answerer_events(db, result["run_id"]) if "pack" in e[4]]
    assert len(packed) == len(SMALL) and sum(e[3] for e in packed) > 0
    expected = _stub_dollars(stub)
    assert _digest_cost(db, result["run_id"]) == pytest.approx(expected, rel=1e-6)
