"""On-premise lane: the defects two fresh reviewers found (16-Nov-2026).

Each test reproduces a leak or a wrong pass the reviewers showed on the code
of work orders 2.2/2.3, through the real engine and the stand-in model
servers where the path allows it. The lead's rulings: BUILD.md section 8,
rows "2.2 locality/API review" and "2.2 runtime review" (16-Nov-2026).
"""
from __future__ import annotations

import json
import uuid

import pytest

from src.lane.backstop import normalise
from src.orchestrator import engine
from tests.e2e.stub_model_server import StubModels, lane_matrix
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
    from src.envelopes.schema import InboundEnvelope

    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
                          status="resolved", output={"client": "Zoë Müller"})
    task = engine._build_task_from_node(
        {"type": "answerer", "id": "n2", "depends_on": ["n1"], "instruction": "x",
         "output_schema": {"result": "string"}},
        run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="http://127.0.0.1:11434", model_name="ollama/stub/local",
        user_instruction="x", results={"n1": env},
    )
    assert "Zoë Müller" in task.context


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
