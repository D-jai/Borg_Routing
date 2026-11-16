"""On-premise lane: the defects two fresh reviewers found (16-Nov-2026).

Each test reproduces a leak or a wrong pass the reviewers showed on the code
of work orders 2.2/2.3, through the real engine and the stand-in model
servers where the path allows it. The lead's rulings: BUILD.md section 8,
rows "2.2 locality/API review" and "2.2 runtime review" (16-Nov-2026).
"""
from __future__ import annotations

import asyncio
import json
import types
import socket
import sqlite3
import threading
import uuid

import pytest
from fastapi import BackgroundTasks

from src.lane.backstop import normalise
from src.orchestrator import engine
from tests.e2e.stub_model_server import StubModels, drain_log_writes, lane_matrix
from tests.e2e.test_privacy_lane import (  # noqa: F401  (fixture)
    PLAN_RAW,
    SUMMARY,
    _error_codes,
    _run,
    lane,
)

# 14 short lines (each far under the 120-character verbatim limit).
CONTACTS = "\n".join(
    f"{name} Quill 555-01{i:02d} acct CANARY{i:04d}"
    for i, name in enumerate(["Ada", "Bram", "Cleo", "Dov", "Eli", "Fay", "Gus",
                              "Hal", "Ivy", "Jon", "Kai", "Lea", "Max", "Ned"])
)
# Long and dense with accents: every 8-word window holds an accented word.
ACCENTED = (
    "Zoë Müller signed the café lease with Renée Bélanger, and Señor Muñoz "
    "owes 4471 for the façade at Ærø; Søren Ångström keeps the crème ledger, "
    "Chloé Dvořák holds the résumé file, Jürgen Strauß the naïve coöperative "
    "fee and Łucja Wróbel the déjà vu clause. ZQ-ACCENT-LONG-CANARY"
)
SHORT_NL = "Vault code orchid 4471.\nKept by the treasurer only, never the desk. ZQ-SHORT-CANARY"
SHORT_ACCENT = "Client Zoë Müller owes 4471 on the café lease. ZQ-ACCENT-CANARY"


def _docs(lane, files: dict) -> None:
    for p in list(lane["docs"].iterdir()):
        if p.is_file():
            p.unlink()
    for name, text in files.items():
        (lane["docs"] / name).write_text(text, encoding="utf-8")


def _copy_whole(text):
    def answer(req):
        if req.is_local:
            return {"client": text, "risk": "high"}
        return {"result": f"done by {req.model}"}
    return answer


def _cloud_with(stub, needle: str) -> list:
    """Cloud requests whose body carries ``needle`` (decoded, and as sent)."""
    return [
        r for r in stub.requests
        if not r.is_local and (
            needle in json.dumps(r.body) or needle in json.dumps(r.body, ensure_ascii=False)
        )
    ]


def _bypass_guard(monkeypatch):
    """Simulate a defect in the output guard: every raw output passes stamped.
    What is left is the backstop, the last check before a request leaves."""
    from src.agents import executor

    monkeypatch.setattr(
        executor, "guard_envelope",
        lambda env, task, ctx: env.model_copy(update={"lane_guarded": True}),
    )


# -- R1: escapes do not defeat the guard or the backstop ----------------------

async def test_r1_a_multiline_document_copied_whole_is_withheld(lane):
    _docs(lane, {"contacts.txt": CONTACTS})
    assert len(json.dumps({"client": CONTACTS, "risk": "high"})) < 1500
    stub, result = await _run(lane, "raw_local", lane_matrix(), answer_for=_copy_whole(CONTACTS))
    assert not _cloud_with(stub, "CANARY0013"), "whole multi-line document reached a cloud model"
    assert "BRT-LANE-004" in stub.of_kind("re_plan")[0].system


@pytest.mark.parametrize("text,canary", [
    (CONTACTS, "CANARY0013"),
    (ACCENTED, "ZQ-ACCENT-LONG-CANARY"),
], ids=["multiline", "accented"])
async def test_r1_the_backstop_reads_decoded_text_behind_a_broken_guard(lane, text, canary):
    _docs(lane, {"note.txt": text})
    _bypass_guard(lane["mp"])
    stub, result = await _run(lane, "raw_local", lane_matrix(), answer_for=_copy_whole(text))
    assert not _cloud_with(stub, canary), "document text reached a cloud model"
    assert "BRT-LANE-006" in _error_codes(lane["db"], result["run_id"])


def test_r1_the_backstop_decodes_escaped_and_nested_json():
    from src.lane.backstop import DocIndex, find_leak

    for text in (CONTACTS, ACCENTED):
        doc = DocIndex("doc_001", text, 120)
        once = "Stage results:\n" + json.dumps({"n1": {"client": text}}, indent=2)
        twice = json.dumps({"wrapped": json.dumps({"client": text})})
        for body in (
            {"messages": [{"role": "system", "content": once}]},
            {"messages": [{"role": "user", "content": twice}]},
            json.dumps({"messages": [{"content": once}]}),
        ):
            assert find_leak(body, [doc], 120, 0.10) is not None, body


def test_r1_engine_prompt_blocks_keep_accents_unescaped():
    """With the lane on (round 2: with it off the block is Phase 1's form)."""
    from src.envelopes.schema import InboundEnvelope
    from src.lane.context import LaneContext, lane_scope
    from src.lane.settings import LaneSettings

    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                          status="resolved", output={"client": "Zoë Müller"})
    ctx = LaneContext(settings=LaneSettings(mode="raw_local"), docs=None, db_path=":memory:")
    with lane_scope(ctx):
        task = _build_n2(env)
    assert "Zoë Müller" in task.context


def _build_n2(env):
    return engine._build_task_from_node(
        {"type": "answerer", "id": "n2", "depends_on": ["n1"], "instruction": "x",
         "output_schema": {"result": "string"}},
        run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="http://127.0.0.1:11434", model_name="ollama/stub/local",
        user_instruction="x", results={"n1": env},
    )


# -- R2: the guard's short-document rule ------------------------------------------

@pytest.mark.parametrize("text,canary", [
    (SHORT_NL, "ZQ-SHORT-CANARY"),
    (SHORT_ACCENT, "ZQ-ACCENT-CANARY"),
], ids=["newline", "accented"])
async def test_r2_a_short_document_copied_whole_is_withheld(lane, text, canary):
    _docs(lane, {"short.txt": text})
    assert 24 <= len(normalise(text)) < 120
    stub, result = await _run(lane, "raw_local", lane_matrix(), answer_for=_copy_whole(text))
    assert not _cloud_with(stub, canary), "short document copied whole reached a cloud model"
    assert "BRT-LANE-004" in stub.of_kind("re_plan")[0].system


# -- R3: a raw failure's code never comes from model text ----------------------------

class _FailingLocal(StubModels):
    """A local model obeying the document: it reports failure with a JSON
    failure_reason whose "code" is the document text."""

    payload = ""

    def _answer(self, url, payload):
        resp = super()._answer(url, payload)
        req = self.requests[-1]
        if req.is_local and req.kind == "answerer":
            content = {"status": "failed", "failure_reason": json.dumps({"code": self.payload})}
            resp._body["choices"][0]["message"]["content"] = json.dumps(content)
        return resp


async def test_r3_document_text_in_a_failure_code_never_travels_upward(lane):
    _docs(lane, {"contacts.txt": CONTACTS})

    class Stub(_FailingLocal):
        payload = CONTACTS * 3

    stub, result = await _run(lane, "raw_local", lane_matrix(), stub_cls=Stub)
    assert not _cloud_with(stub, "CANARY0013"), "failure_reason carried document text upward"
    replan = stub.of_kind("re_plan")[0].system
    assert "doc_001" in replan


def test_r3_the_code_is_a_registry_shaped_match_or_a_fixed_code():
    from src.lane.context import raw_failure_code

    assert raw_failure_code("BRT-ANS-004: could not parse. Raw: secret") == "BRT-ANS-004"
    assert raw_failure_code(json.dumps({"code": "Ada Quill 555"})) == "BRT-LANE-004"
    assert raw_failure_code({"code": "Ada Quill 555"}) == "BRT-LANE-004"
    assert raw_failure_code(None) == "BRT-LANE-004"
    assert raw_failure_code("BRT-ACCT-5550123456789012") == "BRT-LANE-004"


# -- R4: lane_mode off reads nothing else ----------------------------------------------

async def test_r4_off_ignores_every_other_lane_setting(lane):
    from src.db import state

    read: list[str] = []
    real = state.get_config

    async def spy(key, *args, **kwargs):
        read.append(key)
        return await real(key, *args, **kwargs)

    lane["mp"].setattr(state, "get_config", spy)
    lane["mp"].setenv("BORG_LANE_MAX_NGRAM_SHARE", "2")
    lane["mp"].setenv("BORG_LANE_MANIFEST", "titles")
    stub, result = await _run(lane, "off", lane_matrix(), documents=None,
                              plan=[PLAN_RAW[1] | {"depends_on": []}])
    assert result["status"] == "completed", result.get("error")
    assert [k for k in read if k.startswith("lane_")] == ["lane_mode"]


# -- R6: web_search keeps the call guard in all_local --------------------------------------

async def test_r6_web_search_is_refused_at_the_door_in_all_local(lane):
    from src.lane.context import lane_scope, prepare_lane
    from src.lane.errors import LaneError
    from src.tools.web_search import WebSearchTool

    lane["mp"].setenv("BORG_LANE_MODE", "all_local")
    lane["mp"].setenv("TAVILY_API_KEY", "tvly-test")
    stub = StubModels(plan_nodes=[]).install(lane["mp"])
    ctx = await prepare_lane(None, lane["db"])
    with lane_scope(ctx):
        with pytest.raises(LaneError) as info:
            await WebSearchTool().run({"query": "lease rates"})
    assert info.value.code == "BRT-LANE-003"
    assert stub.requests == []


# -- R7: resume and MCP submit_task carry the lane --------------------------------------------

async def _all_local_api(lane):
    from src.api import tasks as tasks_api
    from src.db import schema

    lane["mp"].setenv("BORG_LANE_MODE", "all_local")
    lane["mp"].setattr(tasks_api, "DB_PATH", lane["db"])
    lane["mp"].setattr(schema, "DB_PATH", lane["db"])
    lane["mp"].setattr(engine, "_shared_matrix", lane_matrix())
    plan = [dict(PLAN_RAW[1], tier="light", depends_on=[])]
    return StubModels(plan_nodes=plan).install(lane["mp"])


async def test_r7_a_resumed_run_in_all_local_stays_on_the_machine(lane):
    from src.api import tasks as tasks_api
    from src.db.state import create_run, save_envelope
    from src.envelopes.schema import WrappedTask

    stub = await _all_local_api(lane)
    run_id = await create_run("Summarise the risks.", db_path=lane["db"])
    await save_envelope(WrappedTask(
        envelope_id=uuid.uuid4(), run_id=run_id, subtask_id=uuid.uuid4(), type="answerer",
        current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="https://openrouter.ai/api", model_name="openrouter/stub/cheap",
        instruction="Summarise.", output_schema={"result": "string"},
    ), db_path=lane["db"])
    # A run that stopped mid-step is failed; a pending run with work in flight
    # is refused as still busy (BRT-API-024).
    from src.db.state import update_run_status
    await update_run_status(run_id, "failed", db_path=lane["db"])
    background = BackgroundTasks()
    resp = await tasks_api.resume_run_endpoint(str(run_id), background)
    assert resp.status_code == 202
    await background()
    await drain_log_writes()
    assert stub.requests, "the resume never ran"
    assert [r.url for r in stub.requests if not r.is_local] == []


async def test_r7_mcp_submit_task_in_all_local_stays_on_the_machine(lane):
    from src.db.state import get_run
    from src.mcp import server as mcp_server

    stub = await _all_local_api(lane)
    reply = await mcp_server._tool_submit_task({"instruction": "Summarise the risks."})
    run_id = reply["_meta"]["run_id"]
    await asyncio.gather(*list(mcp_server._BACKGROUND_RUNS), return_exceptions=True)
    await drain_log_writes()
    run = await get_run(uuid.UUID(run_id), db_path=lane["db"])
    assert run["status"] in ("completed", "failed")
    assert stub.requests, "the run never ran"
    assert [r.url for r in stub.requests if not r.is_local] == []


# -- R8: the streamed bypass ignores a proxy for a local target ------------------------------

class _FakeProxy:
    """A real listener standing in for a proxy; records what reaches it."""

    def __init__(self) -> None:
        self.got: list[bytes] = []
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(5)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self._stop = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except (socket.timeout, OSError):
                continue
            try:
                conn.settimeout(1)
                self.got.append(conn.recv(65536)[:200])
                conn.sendall(b"HTTP/1.1 502 Bad\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            except OSError:
                pass
            finally:
                conn.close()

    def close(self) -> None:
        self._stop.set()
        self.thread.join(timeout=2)
        self.sock.close()


async def test_r8_a_streamed_bypass_to_a_local_model_ignores_http_proxy(lane, tmp_path):
    from src.api import chat_completions as cc
    from src.lane.context import lane_scope, prepare_lane
    from src.lane.outbound import call_scope
    from src.registry.capability_matrix import CapabilityMatrix
    from src.registry.models import ModelInfo

    # A port with nothing listening: the direct call fails, which is fine.
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    dead_port = probe.getsockname()[1]
    probe.close()

    proxy = _FakeProxy()
    try:
        for name in ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"):
            lane["mp"].setenv(name, f"http://127.0.0.1:{proxy.port}")
        for name in ("NO_PROXY", "no_proxy"):
            lane["mp"].delenv(name, raising=False)
        lane["mp"].setenv("BORG_LANE_MODE", "raw_local")
        lane["mp"].setattr(cc, "DB_PATH", lane["db"])
        matrix = CapabilityMatrix(path=str(tmp_path / "m.yaml"))
        matrix.upsert(ModelInfo(model_id="local-llm", source="lmstudio",
                                endpoint=f"http://127.0.0.1:{dead_port}", context_length=4096))
        ctx = await prepare_lane(None, lane["db"])
        rid = str(uuid.uuid4())
        with lane_scope(ctx), call_scope(uuid.UUID(rid)):
            try:
                async for _chunk in cc._stream_bypass_relay(
                    matrix, "local-llm", "local-llm",
                    [cc.ChatMessage(role="user", content="SECRET-PROMPT")], rid,
                ):
                    pass
            except Exception:
                pass
        await drain_log_writes()
    finally:
        proxy.close()
    assert proxy.got == [], "a local call went through the proxy"
    with sqlite3.connect(lane["db"]) as conn:
        rows = conn.execute("SELECT left_machine FROM outbound_requests").fetchall()
    assert rows and all(r[0] == 0 for r in rows)


# -- R9: tests that pin the parent walk and the body gating ------------------------------------

def _insert_runs(db, parent_docs: bool):
    parent, child = str(uuid.uuid4()), str(uuid.uuid4())
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO runs(run_id, status, user_instruction, documents) VALUES (?, 'failed', 'p', ?)",
            (parent, json.dumps([{"id": "doc_001"}]) if parent_docs else None),
        )
        conn.execute(
            "INSERT INTO runs(run_id, parent_run_id, status, user_instruction) "
            "VALUES (?, ?, 'failed', 'c')", (child, parent),
        )
    return parent, child


async def test_r9_resuming_a_child_of_a_run_that_read_documents_is_refused(lane):
    from fastapi import HTTPException

    from src.api import tasks as tasks_api

    lane["mp"].setattr(tasks_api, "DB_PATH", lane["db"])
    _parent, child = _insert_runs(lane["db"], parent_docs=True)
    with pytest.raises(HTTPException) as info:
        await tasks_api.resume_run_endpoint(child, BackgroundTasks())
    assert info.value.status_code == 409
    assert info.value.detail["error"] == "BRT-LANE-008"


async def test_r9_run_detail_withholds_outbound_bodies_when_admin_is_not_allowed(lane):
    from src.api import tasks as tasks_api

    lane["mp"].setattr(tasks_api, "DB_PATH", lane["db"])
    run_id = str(uuid.uuid4())
    with sqlite3.connect(lane["db"]) as conn:
        conn.execute("INSERT INTO runs(run_id, status, user_instruction) VALUES (?, 'completed', 'p')",
                     (run_id,))
        conn.execute(
            "INSERT INTO outbound_requests(request_id, run_id, attempt, purpose, url, "
            "left_machine, body, status) VALUES (?,?,1,'model','https://x.test',1,?,'sent')",
            (str(uuid.uuid4()), run_id, json.dumps({"model": "m", "secret": "BODY-TEXT"})),
        )
    loopback = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"), headers={})
    shown = json.loads((await tasks_api.get_run_state(run_id, loopback)).body)
    assert shown["outbound"] and shown["outbound"][0]["body"] is not None
    # no request at all fails closed
    assert all(r["body"] is None for r in json.loads((await tasks_api.get_run_state(run_id)).body)["outbound"])
    lane["mp"].setattr(tasks_api, "lane_admin_allowed", lambda request=None: False)
    hidden = json.loads((await tasks_api.get_run_state(run_id, loopback)).body)
    assert hidden["outbound"] and all(r["body"] is None for r in hidden["outbound"])
    assert "BODY-TEXT" not in json.dumps(hidden)


# -- R10: resume fails closed ------------------------------------------------------------------

async def test_r10_resume_is_refused_when_the_documents_lookup_fails(lane):
    from fastapi import HTTPException

    from src.api import tasks as tasks_api

    async def broken(*args, **kwargs):
        raise RuntimeError("database is locked")

    lane["mp"].setattr(tasks_api, "DB_PATH", lane["db"])
    lane["mp"].setattr(tasks_api, "get_run_documents", broken)
    started: list = []

    async def never(*args, **kwargs):
        started.append(1)
        return []

    lane["mp"].setattr(tasks_api, "resume_run", never)
    _parent, child = _insert_runs(lane["db"], parent_docs=False)
    with pytest.raises(HTTPException) as info:
        await tasks_api.resume_run_endpoint(child, BackgroundTasks())
    assert info.value.status_code in (409, 503)
    assert info.value.detail["error"] == "BRT-LANE-008"
    assert started == []
