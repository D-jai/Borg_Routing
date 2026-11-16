"""Work order 1.11 (planner ruling 3): failure reasons reach re_plan.

A failed step's entry in the stage_results block of the re_plan request
carries ``{"failure": {"code", "message"}}`` (code from the start of the
failure reason, message its first 200 characters). A raw lane step keeps the
lane rule: code and document ids, never message text. A passing step's entry
is unchanged.

The real engine, executor and router run against the scripted stand-in model
server and a temp database. Nothing here needs a key or the network.
"""
import json

import pytest

from src.db.schema import create_all
from src.envelopes.schema import InboundEnvelope
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, lane_matrix, three_tier_matrix

STEP = "Summarise the quarter."


@pytest.fixture
async def env(tmp_path, monkeypatch):
    db_path = str(tmp_path / "failure_reasons.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-failure")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return db_path, monkeypatch


def _answerer(**extra):
    node = {"type": "answerer", "id": "n1", "instruction": STEP,
            "task_type": "extraction", "tier": "light",
            "output_schema": {"result": "string"}}
    node.update(extra)
    return node


def _replan_block(stub) -> dict:
    """The stage_results block of the first re_plan request, parsed."""
    replans = stub.of_kind("re_plan")
    assert replans, "re_plan was not called"
    text = replans[0].system
    start = text.index('{\n  "n1"')
    block, _ = json.JSONDecoder().raw_decode(text[start:])
    return block


# --- a failing answerer step: truncation (BRT-ANS-009) --------------------

@pytest.mark.asyncio
async def test_truncated_answerer_shows_code_and_message_in_replan(env):
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_answerer()],
        finish_for=lambda r: "length" if r.kind == "answerer" else "stop",
    ).install(mp)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-ANS-009"
    assert failure["message"].startswith("BRT-ANS-009: model output truncated")
    assert len(failure["message"]) <= 200


# --- BRT-ORC-004: the step raised while running ---------------------------

@pytest.mark.asyncio
async def test_step_failure_orc_004_shows_code_and_message_in_replan(env):
    db_path, mp = env
    stub = StubModels(plan_nodes=[_answerer()]).install(mp)

    async def boom(task, db_path):
        raise RuntimeError("the worker fell over")

    mp.setattr(engine, "execute", boom)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure == {"code": "BRT-ORC-004", "message": "BRT-ORC-004: the worker fell over"}


# --- BRT-ORC-008: the step could not be built -----------------------------

@pytest.mark.asyncio
async def test_step_failure_orc_008_shows_code_and_message_in_replan(env):
    db_path, mp = env
    node = {"type": "apply_to_each", "id": "n1", "inputs_ref": "documents",
            "instruction": STEP, "task_type": "extraction", "tier": "light",
            "output_schema": {"result": "string"}}
    stub = StubModels(plan_nodes=[node]).install(mp)
    mp.setattr(engine, "_build_task_from_node",
               _raise_orc_008, raising=True)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-ORC-008"
    assert "the step names documents" in failure["message"]


def _raise_orc_008(**kwargs):
    raise ValueError("BRT-ORC-008: the step names documents, but this run has none")


# --- the cap and the fallback ---------------------------------------------

def _failed(reason, output=None, lane_guarded=None):
    from uuid import uuid4
    return InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status="failed",
                           output=output or {}, failure_reason=reason, lane_guarded=lane_guarded)


def test_message_longer_than_200_characters_is_cut_at_200():
    entry = engine._stage_output_for_prompt(_failed("BRT-ANS-009: " + "x" * 500))
    assert len(entry["failure"]["message"]) == 200
    assert entry["failure"]["message"] == ("BRT-ANS-009: " + "x" * 500)[:200]
    assert entry["failure"]["code"] == "BRT-ANS-009"


def test_reason_without_a_code_gets_the_fixed_fallback_code():
    entry = engine._stage_output_for_prompt(_failed("something odd happened"))
    assert entry["failure"] == {"code": "BRT-ORC-004", "message": "something odd happened"}


def test_failure_is_merged_with_existing_output():
    entry = engine._stage_output_for_prompt(
        _failed("BRT-APE-006: 'reviews' is not a step id", output={"results": [], "total": 0})
    )
    assert entry["results"] == [] and entry["total"] == 0
    assert entry["failure"]["code"] == "BRT-APE-006"


# --- a passing step is unchanged ------------------------------------------

def test_passing_step_entry_is_byte_identical():
    from uuid import uuid4
    out = {"result": "fine", "n": [1, 2], "nested": {"a": "b"}}
    passed = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status="resolved", output=out)
    shown = engine._stage_output_for_prompt(passed)
    assert json.dumps(shown) == json.dumps(out)


# --- a raw lane step: code and document id only ---------------------------

CANARY = "FAILMSG-CANARY-4417"
NOTE = ("Client note for Acme Holdings. Exposure grew in the third quarter after two "
        "late payments on the equipment lease, and the treasurer has resigned.")


@pytest.mark.asyncio
async def test_raw_step_failure_message_stays_out_of_replan_but_code_goes(tmp_path, monkeypatch):
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "acme_note.txt").write_text(NOTE, encoding="utf-8")
    db_path = str(tmp_path / "raw_fail.db")
    await create_all(db_path)
    for name in ("BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
                 "BORG_LANE_MAX_OUTPUT_CHARS", "BORG_LANE_MAX_VERBATIM_CHARS",
                 "BORG_LANE_MAX_NGRAM_SHARE", "BORG_LANE_PART_CHARS"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_DOCS_ROOT", str(docs))
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-failure")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", lane_matrix())
    node = {"type": "apply_to_each", "id": "n1", "inputs_ref": "documents",
            "instruction": "Give the client name.", "task_type": "extraction",
            "tier": "light", "output_schema": {"client": "string"}}

    async def boom(task, db_path):
        raise ValueError(f"BRT-ANS-004: {CANARY} could not be parsed")

    stub = StubModels(plan_nodes=[node]).install(monkeypatch)
    monkeypatch.setattr(engine, "execute", boom)
    await run_pipeline("Read the note.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    replan = stub.of_kind("re_plan")[0]
    body = json.dumps(replan.body)
    assert "BRT-ANS-004" in body
    assert CANARY not in body
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure == {"code": "BRT-ANS-004", "doc": "doc_001"}
