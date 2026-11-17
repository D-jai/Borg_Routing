"""Skill steps never run blind and never run on data no model can hold.

Round-2 review of 1ba958f (16-Nov-2026, BUILD.md section 8, "1ba958f round-2
review (4)"):

1. ``_node_reads_documents`` counted only ``items_ref``/``inputs_ref``, so a
   verify_claim step with ``evidence_ref: "documents"`` was not raw, its
   documents were never loaded, and the model judged the claim with no
   evidence. Every skill ``*_ref`` arg now counts: the step is raw, its
   documents are loaded (raw_local: on the local model; by_reference: on any
   checked model), or it fails loudly.
2. An answerer skill's context data (an earlier step's output, numbered) was
   not counted in the router's token estimate. It is now; when no checked
   model's context holds it (input plus output), the step fails before any
   model call with BRT-REG-020. Nothing is truncated.

The real engine, executor and router run against the scripted stand-in, with
a temp database and a temp docs root.
"""
import json
import sqlite3

import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import (
    StubModels,
    drain_log_writes,
    lane_matrix,
    three_tier_matrix,
)
from tests.e2e.test_privacy_lane import (  # noqa: F401  (lane is a fixture)
    CANARIES,
    CANARY_ACCOUNT,
    CANARY_BIRCH,
    _error_codes,
    _run,
    lane,
)

VERIFY = "Assess whether this claim is supported"
GATHER = "Gather the evidence."


def _verdict(req):
    if VERIFY in req.system:
        return {"verdict": "insufficient", "confidence": 0.4,
                "cited_evidence_ids": [], "explanation": "e"}
    return {"result": "done"}


def _verify_plan(tier: str) -> list[dict]:
    return [{"type": "answerer", "id": "n1", "skill_id": "verify_claim", "skill_version": 1,
             "task_type": "reasoning", "tier": tier,
             "skill_args": {"claim": "Acme is high risk.", "evidence_ref": "documents"}}]


def _verify_calls(stub) -> list:
    return [r for r in stub.of_kind("answerer") if VERIFY in r.system]


# -- 1. evidence_ref "documents" ---------------------------------------------------

def test_every_skill_ref_arg_naming_documents_reads_documents():
    """Unit: any ``*_ref`` arg set to "documents" marks the node as reading them."""
    for key in ("items_ref", "evidence_ref", "failed_items_ref", "inputs_ref"):
        node = {"skill_id": "x", "skill_args": {key: "documents"}}
        assert engine._node_reads_documents(node), key
    assert not engine._node_reads_documents(
        {"skill_id": "x", "skill_args": {"evidence_ref": "n1", "claim": "documents"}})


async def test_raw_local_verify_claim_evidence_documents_loaded_on_the_local_model(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=_verify_plan("light"),
                              answer_for=_verdict)
    calls = _verify_calls(stub)
    assert len(calls) == 1, _error_codes(lane["db"], result["run_id"])
    assert calls[0].is_local, calls[0].model
    # The documents reached the step: it did not judge the claim blind.
    assert CANARY_ACCOUNT in calls[0].system and CANARY_BIRCH in calls[0].system
    for r in stub.requests:
        if not r.is_local:
            body = json.dumps(r.body)
            assert not [c for c in CANARIES if c in body], (r.kind, r.model)


async def test_raw_local_verify_claim_documents_without_a_fitting_local_model_fails_loudly(lane):
    """Mid tier: the local model is below the floor, so the step is refused
    (BRT-LANE-002) and never sent to a cloud model with or without evidence."""
    stub, result = await _run(lane, "raw_local", lane_matrix(), plan=_verify_plan("mid"),
                              answer_for=_verdict)
    assert _verify_calls(stub) == []
    assert "BRT-LANE-002" in _error_codes(lane["db"], result["run_id"])


async def test_by_reference_verify_claim_evidence_documents_loaded(lane):
    stub, result = await _run(lane, "by_reference", lane_matrix(), plan=_verify_plan("mid"),
                              answer_for=_verdict)
    calls = _verify_calls(stub)
    assert len(calls) == 1, _error_codes(lane["db"], result["run_id"])
    assert not calls[0].is_local   # any checked model may serve it in by_reference
    assert CANARY_ACCOUNT in calls[0].system and CANARY_BIRCH in calls[0].system
    with sqlite3.connect(lane["db"]) as conn:
        scopes = [r[0] for r in conn.execute(
            "SELECT outbound_payload FROM envelopes WHERE type='answerer'")]
    assert any('"data_scope": "raw"' in (s or "") or '"data_scope":"raw"' in (s or "")
               for s in scopes), scopes


def test_doc_tokens_counts_every_part_for_an_answerer_skill():
    """verify_claim reads all its documents in one call: their whole size counts."""

    class _Lane:
        def chars_of(self, parts):
            return sum({"a": 4000, "b": 8000}[p] for p in parts)

    node = {"skill_id": "verify_claim", "skill_args": {"evidence_ref": "documents"}}
    answerer = {"envelope_type": "answerer"}
    per_item = {"envelope_type": "apply_to_each"}
    assert engine._doc_tokens(node, ["a", "b"], _Lane(), answerer) == 3000
    assert engine._doc_tokens(node, ["a", "b"], _Lane(), per_item) == 2000


# -- 2. context data counts toward the size, and too big fails loudly ----------------

@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "skill_data.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-skill-data")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    return db_path, monkeypatch


def _gather_then_verify(tier: str = "mid") -> list[dict]:
    return [
        {"type": "answerer", "id": "n1", "instruction": GATHER,
         "task_type": "extraction", "tier": "light", "output_schema": {"items": "list"}},
        {"type": "answerer", "id": "n2", "skill_id": "verify_claim", "skill_version": 1,
         "task_type": "reasoning", "tier": tier,
         "skill_args": {"claim": "The claim holds.", "evidence_ref": "n1"}},
    ]


def _route_of(db_path: str, node_id: str) -> dict:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='orchestrator_decision'"
        ).fetchall()
    for (raw,) in rows:
        d = json.loads(raw)
        if d.get("mode") == "route" and d.get("node_id") == node_id:
            return d
    raise AssertionError(f"no route record for {node_id}")


async def test_too_much_context_data_fails_with_reg_020_and_no_call(env):
    db_path, mp = env
    mp.setattr(engine, "_shared_matrix", three_tier_matrix())   # every context 200k
    entry = "evidence line " * 50                                # ~700 chars
    entries = [f"{i}: {entry}" for i in range(3000)]             # ~2.1M chars

    def answer(req):
        if VERIFY in req.system:
            return _verdict(req)
        return {"items": entries}

    stub = StubModels(plan_nodes=_gather_then_verify(), answer_for=answer).install(mp)
    result = await run_pipeline("Check the claim.", db_path=db_path)
    await drain_log_writes()
    assert _verify_calls(stub) == [], "the step must fail before any model call"
    with sqlite3.connect(db_path) as conn:
        codes = [json.loads(r[0]).get("code") for r in conn.execute(
            "SELECT decision_json FROM logs WHERE event_type='error'")]
    assert "BRT-REG-020" in codes, codes
    replan = " ".join(r.system + r.user for r in stub.of_kind("re_plan"))
    assert "BRT-REG-020" in replan
    assert result.get("status") is not None


async def test_modest_context_data_is_counted_and_skips_a_small_context_model(env):
    """The cheap model clears the light floor but holds only 2000 tokens; the
    data (~3500 tokens) is in the estimate, so the step goes to a model that
    holds it, and the logged estimate reflects the data's size."""
    db_path, mp = env
    matrix = three_tier_matrix()
    matrix._models["openrouter/stub/cheap"].context_length = 2000
    mp.setattr(engine, "_shared_matrix", matrix)
    entries = [f"{i}: " + "evidence line " * 50 for i in range(20)]
    data_chars = sum(len(e) for e in entries)

    def answer(req):
        if VERIFY in req.system:
            return _verdict(req)
        return {"items": entries}

    stub = StubModels(plan_nodes=_gather_then_verify("light"), answer_for=answer).install(mp)
    result = await run_pipeline("Check the claim.", db_path=db_path)
    await drain_log_writes()
    calls = _verify_calls(stub)
    assert len(calls) == 1, result
    assert "cheap" not in calls[0].model, calls[0].model
    assert entries[0] in calls[0].system and entries[-1] in calls[0].system  # not truncated
    route = _route_of(db_path, "n2")
    assert route["est_input_tokens"] >= data_chars // 4, route["est_input_tokens"]


async def test_small_context_data_may_use_the_small_context_model(env):
    """Control: with little data the cheap model is chosen, so the test above
    is about size, not about the cheap model being unusable."""
    db_path, mp = env
    matrix = three_tier_matrix()
    matrix._models["openrouter/stub/cheap"].context_length = 2000
    mp.setattr(engine, "_shared_matrix", matrix)

    def answer(req):
        if VERIFY in req.system:
            return _verdict(req)
        return {"items": ["one short line", "another short line"]}

    stub = StubModels(plan_nodes=_gather_then_verify("light"), answer_for=answer).install(mp)
    await run_pipeline("Check the claim.", db_path=db_path)
    await drain_log_writes()
    calls = _verify_calls(stub)
    assert len(calls) == 1 and "cheap" in calls[0].model, [c.model for c in calls]
