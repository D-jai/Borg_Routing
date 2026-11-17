"""Work order 2.4 review (BUILD.md section 8, "2.4 review (5; ..."): the fixes.

- Many parts at once: ledger writes are serialised per database and retried
  on "database is locked", so concurrent sends never fail closed by accident
  (BRT-LANE-007), in every lane mode.
- In ``by_reference`` a raw step's failure reaches the planner by the
  non-raw rules (code + first 200 characters, BRT-ANS-010 for a failure the
  model reported), never a lane code from a guard that did not run; model
  text is never parsed for a code in any mode. ``raw_local`` is unchanged.
- In ``by_reference`` the document size still counts toward routing.
- The Settings help text and the Overview name ``by_reference`` honestly.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.envelopes.schema import InboundEnvelope
from src.lane.context import mark_model_reason
from src.lane.documents import load_documents
from src.orchestrator import engine
from dataclasses import dataclass
from typing import Callable, Optional

from tests.e2e.stub_model_server import StubModels, _StubResponse, lane_matrix, three_tier_matrix
from tests.e2e.test_privacy_lane import (  # noqa: F401  (``lane`` is a fixture)
    CANARY_ACCOUNT,
    EXTRACT,
    PLAN_RAW,
    _error_codes,
    _ledger,
    _route_of,
    _run,
    lane,
)
from tests.e2e.test_privacy_lane_by_reference import _cloud_extract, _item_requests

ROOT = Path(__file__).resolve().parents[2]


@dataclass
class _EnvelopeStub(StubModels):
    """StubModels whose step answers may be a whole envelope (a failure)."""
    envelope_for: Optional[Callable] = None

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        req = self.requests[-1]
        env = self.envelope_for(req) if (self.envelope_for and req.kind == "answerer") else None
        if env is None:
            return resp
        return _StubResponse({
            "choices": [{"message": {"content": json.dumps(env)}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 10},
        })

# -- 4. many parts, concurrency 20, every lane mode ----------------------------

_MANY_PART_CHARS = 22   # about 35 parts across the two notes
_LOOP_BLOCK_SECONDS = 0.3


def _plan_for(mode: str) -> list[dict]:
    plan = [dict(PLAN_RAW[0], concurrency_limit=20), dict(PLAN_RAW[1])]
    if mode == "all_local":
        plan[1]["tier"] = "light"
    return plan


@pytest.mark.parametrize("loop", range(5))
@pytest.mark.parametrize("mode", ["raw_local", "all_local", "by_reference"])
async def test_many_parts_concurrent_sends_never_fail_the_ledger(lane, mode, loop):
    # Every send builds its own httpx client, and loading the CA bundle
    # blocks the event loop (about 1.5 s each on this machine) while other
    # steps hold an open write: that is what made ledger writes fail with
    # "database is locked". Keep the block, shorter, so the test is quick
    # and still fails on the old code.
    import ssl
    import time

    lane["mp"].setattr(ssl.SSLContext, "load_verify_locations",
                       lambda *a, **k: time.sleep(_LOOP_BLOCK_SECONDS))
    lane["mp"].setenv("BORG_LANE_PART_CHARS", str(_MANY_PART_CHARS))
    expected = load_documents({"folder": "."}, part_chars=_MANY_PART_CHARS)
    parts = [p for d in expected.documents for p in d.parts]
    assert 30 <= len(parts) <= 45, len(parts)

    if mode == "by_reference":
        stub, result = await _run(lane, mode, three_tier_matrix(),
                                  plan=_plan_for(mode), answer_for=_cloud_extract)
    else:
        stub, result = await _run(lane, mode, lane_matrix(), plan=_plan_for(mode))
    codes = _error_codes(lane["db"], result["run_id"])
    assert "BRT-LANE-007" not in codes, codes
    assert result["status"] == "completed", (result.get("error"), codes)
    assert len(_item_requests(stub)) == len(parts), codes
    rows = _ledger(lane["db"])
    assert len(rows) == len(stub.requests)
    assert all(r["status"] == "sent" for r in rows)


async def test_ledger_retries_a_locked_database_then_writes(lane, monkeypatch):
    """A transient "database is locked" is retried; the row is written."""
    import sqlite3

    from src.lane import outbound

    calls = {"n": 0}
    real = outbound._insert_once

    async def flaky(db_path, row):
        calls["n"] += 1
        if calls["n"] < 3:
            raise sqlite3.OperationalError("database is locked")
        await real(db_path, row)

    monkeypatch.setattr(outbound, "_insert_once", flaky)
    monkeypatch.setattr(outbound, "_RETRY_BASE_SECONDS", 0.0)
    row = {
        "request_id": "r-1", "run_id": None, "envelope_id": None, "attempt": 1,
        "purpose": "test", "destination": None, "url": "http://x", "left_machine": 0,
        "body": "{}",
    }
    await outbound._insert_row(lane["db"], row)
    assert calls["n"] == 3
    assert [r["request_id"] for r in _ledger(lane["db"])] == ["r-1"]


async def test_ledger_still_fails_closed_when_the_lock_never_clears(lane, monkeypatch):
    import sqlite3

    from src.lane import outbound

    calls = {"n": 0}

    async def locked(db_path, row):
        calls["n"] += 1
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(outbound, "_insert_once", locked)
    monkeypatch.setattr(outbound, "_RETRY_BASE_SECONDS", 0.0)
    with pytest.raises(sqlite3.OperationalError):
        await outbound._insert_row(lane["db"], {"request_id": "r-2"})
    assert calls["n"] == outbound._RETRY_TRIES


async def test_every_lane_and_log_connection_sets_a_busy_timeout(lane):
    from src.db.connect import BUSY_TIMEOUT_MS, connect

    async with connect(lane["db"]) as db:
        cur = await db.execute("PRAGMA busy_timeout")
        assert (await cur.fetchone())[0] == BUSY_TIMEOUT_MS
    assert BUSY_TIMEOUT_MS >= 30000
    for rel in ("src/lane/outbound.py", "src/logging/writer.py", "src/db/state.py",
                "src/db/scratch.py"):
        text = (ROOT / rel).read_text(encoding="utf-8")
        assert "aiosqlite.connect(" not in text, f"{rel} opens a connection without the busy timeout"


# -- 3. failures in by_reference reach the planner by the non-raw rules ---------

def _failed(reason: str, *, from_model: bool, scope: str = "raw") -> InboundEnvelope:
    from uuid import uuid4

    env = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status="failed",
                          output={}, failure_reason=reason)
    if from_model:
        mark_model_reason(env)
    env._data_scope = scope
    env._doc_label = "doc_001"
    return env


async def _in_mode(mode: str, lane, fn):
    from src.lane.context import lane_scope, prepare_lane

    lane["mp"].setenv("BORG_LANE_MODE", mode)
    ctx = await prepare_lane({"folder": "."}, lane["db"])
    with lane_scope(ctx):
        return fn()


async def test_by_reference_model_reported_answerer_failure_is_ans_010(lane):
    text = "BRT-LANE-001: I could not read " + "x" * 400
    env = _failed(text, from_model=True)
    shown = await _in_mode("by_reference", lane, lambda: engine._stage_output_for_prompt(env))
    assert shown["failure"]["code"] == "BRT-ANS-010"
    assert shown["failure"]["message"] == text[:200]
    assert "doc" not in shown["failure"]


async def test_by_reference_runtime_answerer_failure_keeps_its_code_and_message(lane):
    text = "BRT-ANS-009: the answer was cut off " + "y" * 400
    env = _failed(text, from_model=False)
    shown = await _in_mode("by_reference", lane, lambda: engine._stage_output_for_prompt(env))
    assert shown["failure"] == {"code": "BRT-ANS-009", "message": text[:200]}


async def test_raw_local_answerer_failure_unchanged(lane):
    env = _failed("BRT-ANS-009: the model said " + CANARY_ACCOUNT, from_model=True)
    shown = await _in_mode("raw_local", lane, lambda: engine._stage_output_for_prompt(env))
    assert shown == {"failure": {"code": "BRT-LANE-004", "doc": "doc_001"}}
    env = _failed("BRT-ANS-009: truncated", from_model=False)
    shown = await _in_mode("raw_local", lane, lambda: engine._stage_output_for_prompt(env))
    assert shown == {"failure": {"code": "BRT-ANS-009", "doc": "doc_001"}}


def _child(scope: str = "raw"):
    from types import SimpleNamespace

    return SimpleNamespace(data_scope=scope, documents=["doc_001#1"])


@pytest.mark.parametrize("mode", ["off", "raw_local", "by_reference"])
async def test_item_failure_never_parses_model_text_for_a_code(lane, mode):
    from src.orchestrator.apply_to_each import _failure_reason

    text = "BRT-LANE-001: the model wrote this " + "z" * 400
    env = _failed(text, from_model=True)
    scope = "raw" if mode != "off" else None
    if mode == "off":
        reason = _failure_reason(_child(scope), env.failure_reason, env)
    else:
        reason = await _in_mode(mode, lane,
                                lambda: _failure_reason(_child(scope), env.failure_reason, env))
    if mode == "off":
        # Lane off: the reason passes up as the text it is, never parsed.
        assert reason == text
    elif mode == "raw_local":
        # A model-written failure that skipped the guard: the fixed lane code.
        assert reason == {"code": "BRT-LANE-004", "doc": "doc_001#1"}
    else:
        assert reason["code"] == "BRT-ANS-010"
        assert reason["message"] == text[:200]


async def test_by_reference_item_runtime_failure_keeps_its_code(lane):
    from src.orchestrator.apply_to_each import _failure_reason

    reason = await _in_mode("by_reference", lane,
                            lambda: _failure_reason(_child(), "BRT-APE-002: boom " + "q" * 300))
    assert reason == {"code": "BRT-APE-002", "message": ("BRT-APE-002: boom " + "q" * 300)[:200]}


async def test_by_reference_failed_item_reaches_the_planner_as_ans_010(lane):
    """End to end: a cloud item model reports failure; re_plan sees
    BRT-ANS-010 and the model's message, never BRT-LANE-004."""
    def failing(req):
        if EXTRACT in req.system and CANARY_ACCOUNT in req.system:
            return {"status": "failed", "output": {},
                    "failure_reason": "BRT-LANE-001 cannot parse this note"}
        return None

    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=_cloud_extract, stub_cls=_EnvelopeStub,
                              envelope_for=failing)
    replan = stub.of_kind("re_plan")
    assert replan, "no re_plan request"
    system = replan[0].system
    assert "BRT-ANS-010" in system
    assert "cannot parse this note" in system
    assert "BRT-LANE-004" not in system
    assert '"code": "BRT-LANE-001"' not in system


async def test_raw_local_failed_item_still_shows_lane_004_and_doc(lane):
    def failing(req):
        if req.is_local and CANARY_ACCOUNT in req.system:
            return {"status": "failed", "output": {},
                    "failure_reason": "BRT-ANS-009 cannot parse " + CANARY_ACCOUNT}
        return None

    stub, result = await _run(lane, "raw_local", lane_matrix(),
                              stub_cls=_EnvelopeStub, envelope_for=failing)
    system = stub.of_kind("re_plan")[0].system
    assert "BRT-LANE-004" in system and "doc_001" in system
    assert CANARY_ACCOUNT not in system and "cannot parse" not in system


# -- 5. document size counts toward routing in by_reference ---------------------

async def test_by_reference_big_document_skips_a_small_context_model(lane):
    """The cheapest cloud model has a context too small for the document; the
    step must go to a model whose context fits it."""
    big = "Client note for Acme Holdings. " + ("The lease is late again. " * 2000)
    (lane["docs"] / "acme_note.txt").write_text(big + CANARY_ACCOUNT, encoding="utf-8")
    lane["mp"].setenv("BORG_LANE_PART_CHARS", "200000")
    matrix = three_tier_matrix()
    small = matrix._models["openrouter/stub/cheap"]
    small.context_length = 2000
    plan = [dict(PLAN_RAW[0])]
    plan[0]["tier"] = "light"
    stub, result = await _run(lane, "by_reference", matrix, plan=plan,
                              answer_for=_cloud_extract)
    items = _item_requests(stub)
    assert items, _error_codes(lane["db"], result["run_id"])
    acme = [r for r in items if CANARY_ACCOUNT in r.system]
    assert acme and all("cheap" not in r.model for r in acme), [r.model for r in acme]


async def test_by_reference_small_document_may_use_the_small_context_model(lane):
    """Control: with a document that fits, the cheap model is chosen, so the
    test above is about size, not about the cheap model being unusable."""
    matrix = three_tier_matrix()
    matrix._models["openrouter/stub/cheap"].context_length = 2000
    plan = [dict(PLAN_RAW[0])]
    stub, result = await _run(lane, "by_reference", matrix, plan=plan,
                              answer_for=_cloud_extract)
    items = _item_requests(stub)
    assert items and all("cheap" in r.model for r in items), [r.model for r in items]


# -- 2. Settings help text and the Overview -------------------------------------

def test_lane_mode_help_text_names_all_four_modes():
    from src.api.admin import _EFFECTIVE_SETTINGS

    spec = next(r for r in _EFFECTIVE_SETTINGS if r["key"] == "lane_mode")
    help_text = spec["help_text"]
    for mode in ("off", "raw_local", "all_local", "by_reference"):
        assert mode in help_text, mode
    assert "no privacy promise" in help_text


async def test_effective_config_serves_the_lane_mode_help_text(tmp_path, monkeypatch):
    from src.api import admin
    from src.db.schema import create_all

    db = str(tmp_path / "c.db")
    await create_all(db)
    monkeypatch.setattr(admin, "DB_PATH", db)
    response = await admin.get_effective_config()
    rows = json.loads(response.body)["settings"]
    row = next(r for r in rows if r["key"] == "lane_mode")
    assert "by_reference" in row["help_text"] and "no privacy promise" in row["help_text"]


def test_overview_shows_by_reference_as_a_warning_not_a_tick():
    html = (ROOT / "ui" / "assets" / "pages" / "admin_overview.js").read_text(encoding="utf-8")
    assert "by_reference:" in html and "no privacy promise" in html
    # The tick is given only to raw_local and all_local.
    assert "mode !== 'off' && mode !== 'by_reference'" in html


# -- 1. the user guide states the promise per mode ------------------------------

def _guide_section(title: str, level: str = "##") -> str:
    text = (ROOT / "docs" / "user-guide.md").read_text(encoding="utf-8")
    start = text.index(f"{level} {title}\n")
    rest = text[start + len(title) + len(level) + 2:]
    ends = [i for i in (rest.find("\n## "), rest.find(f"\n{level} ")) if i >= 0]
    return rest[:min(ends)] if ends else rest


def test_user_guide_lane_section_opens_with_the_promise_per_mode():
    intro = _guide_section("On-premise lane").split("\n### ")[0]
    assert "`raw_local` and `all_local`" in intro
    assert "`by_reference` makes **no privacy promise**" in intro


def test_user_guide_scopes_stay_local_and_not_promised_to_name_by_reference():
    lane_text = _guide_section("On-premise lane")
    assert "In `raw_local` and `all_local`, when a step must stay local" in lane_text
    not_promised = _guide_section("What is not promised", "###")
    assert "by_reference" in not_promised and "no privacy promise" in not_promised


# -- round-2 review (5): failures raised while a raw step runs ---------------------
#
# In by_reference a LaneError (or another runtime error) raised while a raw
# step runs reaches plan/re_plan as {"code", "doc", "message"}: its own code,
# the document ids and the first 200 characters, never BRT-ORC-004 or
# BRT-APE-002. raw_local/all_local keep code + doc only.

_SUMMARY_NODE = {"type": "answerer", "id": "n2", "depends_on": ["n1"],
                 "instruction": "Say which client is high risk.", "task_type": "reasoning",
                 "tier": "mid", "output_schema": {"result": "string"}}
PLAN_RAW_ANSWERER = [
    {"type": "answerer", "id": "n1", "documents": ["doc_001"], "instruction": EXTRACT,
     "task_type": "extraction", "tier": "light",
     "output_schema": {"client": "string", "risk": "string"}},
    _SUMMARY_NODE,
]
PLAN_RAW_ITEMS = [dict(PLAN_RAW[0]), _SUMMARY_NODE]


def _break_extract_ledger_writes(lane) -> None:
    """The ledger row of every request carrying the extract step fails to be
    written (not a "locked" error, so it is not retried): BRT-LANE-007."""
    import sqlite3

    from src.lane import outbound

    real = outbound._insert_once

    async def broken(db_path, row):
        if row.get("purpose") == "answerer" and EXTRACT in (row.get("body") or ""):
            raise sqlite3.DatabaseError("disk I/O error")
        await real(db_path, row)

    lane["mp"].setattr(outbound, "_insert_once", broken)


def _failure_shown(system: str, code: str) -> dict:
    """The {"failure": {...}} entry with ``code`` in a re_plan system prompt."""
    import re

    m = re.search(r'\{\s*"code":\s*"' + re.escape(code) + r'"[^{}]*\}', system)
    assert m, f"{code} not shown to re_plan"
    return json.loads(m.group(0))


async def test_by_reference_ledger_failure_in_a_raw_answerer_reaches_re_plan_as_lane_007(lane):
    _break_extract_ledger_writes(lane)
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              plan=PLAN_RAW_ANSWERER, answer_for=_cloud_extract)
    replan = stub.of_kind("re_plan")
    assert replan, "no re_plan request"
    system = replan[0].system
    shown = _failure_shown(system, "BRT-LANE-007")
    assert shown["doc"].startswith("doc_001")
    assert shown["message"].startswith("BRT-LANE-007: the ledger row")
    assert len(shown["message"]) <= 200
    assert "BRT-ORC-004" not in system


async def test_raw_local_ledger_failure_in_a_raw_answerer_stays_code_and_doc(lane):
    _break_extract_ledger_writes(lane)
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=PLAN_RAW_ANSWERER)
    system = stub.of_kind("re_plan")[0].system
    shown = _failure_shown(system, "BRT-LANE-007")
    assert set(shown) == {"code", "doc"} and shown["doc"].startswith("doc_001")
    assert "could not be written" not in system


async def test_by_reference_runtime_error_in_a_raw_answerer_keeps_its_code_and_message(lane):
    from src.agents import executor

    real = executor._execute_answerer
    text = "BRT-ANS-003: could not build InboundEnvelope from model response: " + "w" * 300

    async def boom(task, db_path):
        if EXTRACT in (task.instruction or ""):
            raise ValueError(text)
        return await real(task, db_path)

    lane["mp"].setattr(executor, "_execute_answerer", boom)
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              plan=PLAN_RAW_ANSWERER, answer_for=_cloud_extract)
    system = stub.of_kind("re_plan")[0].system
    shown = _failure_shown(system, "BRT-ANS-003")
    assert shown["message"] == text[:200]
    assert shown["doc"].startswith("doc_001")
    assert "BRT-ORC-004" not in system


async def test_by_reference_ledger_failure_in_raw_items_reaches_re_plan_as_lane_007(lane):
    _break_extract_ledger_writes(lane)
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              plan=PLAN_RAW_ITEMS, answer_for=_cloud_extract)
    system = stub.of_kind("re_plan")[0].system
    shown = _failure_shown(system, "BRT-LANE-007")
    assert shown["doc"].startswith("doc_00")
    assert shown["message"].startswith("BRT-LANE-007: the ledger row")
    assert "BRT-APE-002" not in system


async def test_by_reference_executor_raises_code_doc_and_message(lane):
    """The executor itself (not only the engine) hands the shape upward."""
    from src.agents.executor import execute
    from src.lane.context import lane_scope, prepare_lane
    from src.lane.errors import RawStepError

    _break_extract_ledger_writes(lane)
    lane["mp"].setenv("BORG_LANE_MODE", "by_reference")
    lane["mp"].setattr(engine, "_shared_matrix", three_tier_matrix())
    StubModels(plan_nodes=[], answer_for=_cloud_extract).install(lane["mp"])
    from tests.e2e.test_privacy_lane import _raw_task

    ctx = await prepare_lane({"folder": "."}, lane["db"])
    task = await _raw_task(lane, "openrouter/stub/top", "https://openrouter.ai/api")
    with lane_scope(ctx):
        with pytest.raises(RawStepError) as info:
            await execute(task, db_path=lane["db"])
    assert info.value.failure == {
        "code": "BRT-LANE-007", "doc": "doc_001",
        "message": info.value.failure["message"],
    }
    assert info.value.failure["message"].startswith("BRT-LANE-007: the ledger row")


# -- round-2 review (5): ledger locks and the write budget -------------------------

def _ledger_row(request_id: str) -> dict:
    return {
        "request_id": request_id, "run_id": None, "envelope_id": None, "attempt": 1,
        "purpose": "test", "destination": None, "url": "http://x", "left_machine": 0,
        "body": "{}",
    }


def test_ledger_locks_do_not_keep_event_loops_alive(tmp_path):
    import asyncio
    import gc
    import weakref

    from src.db.schema import create_all
    from src.lane import outbound

    db = str(tmp_path / "locks.db")
    asyncio.run(create_all(db))
    loops: list = []

    async def two_writers(i: int) -> None:
        loops.append(weakref.ref(asyncio.get_running_loop()))
        await asyncio.gather(*(outbound._insert_row(db, _ledger_row(f"r{i}-{k}"))
                               for k in range(2)))

    for i in range(50):
        asyncio.run(two_writers(i))
    gc.collect()
    alive = [r for r in loops if r() is not None]
    assert alive == [], f"{len(alive)} closed event loops still referenced"


async def test_one_ledger_write_fails_closed_within_its_budget(lane):
    import asyncio
    import sqlite3
    import time

    from src.lane import outbound
    from src.lane.context import lane_scope, prepare_lane
    from src.lane.errors import LaneError

    lane["mp"].setattr(outbound, "_WRITE_BUDGET_SECONDS", 1.0)
    lane["mp"].setattr(outbound, "_TRY_TIMEOUT_SECONDS", 0.3)
    lane["mp"].setenv("BORG_LANE_MODE", "raw_local")
    ctx = await prepare_lane(None, lane["db"])
    holder = sqlite3.connect(lane["db"], isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")   # another connection holds the write lock
    try:
        t0 = time.monotonic()
        with lane_scope(ctx):
            with pytest.raises(LaneError) as info:
                await asyncio.wait_for(outbound.record_start(
                    "http://127.0.0.1:1/v1", {}, left_machine=False, purpose="test",
                    destination=None,
                ), timeout=10)
        elapsed = time.monotonic() - t0
    finally:
        holder.rollback()
        holder.close()
    assert info.value.code == "BRT-LANE-007"
    assert elapsed <= 1.0 + 0.5, elapsed
    assert outbound._WRITE_BUDGET_SECONDS == 1.0


def test_the_ledger_write_budget_is_30_seconds_and_every_try_fits():
    from src.lane import outbound

    assert outbound._WRITE_BUDGET_SECONDS == 30.0
    backoff = sum(outbound._RETRY_BASE_SECONDS * 2 ** i for i in range(outbound._RETRY_TRIES - 1))
    assert outbound._RETRY_TRIES * outbound._TRY_TIMEOUT_SECONDS + backoff <= 30.0
