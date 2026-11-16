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
    """A real build failure: the plan names a skill that does not exist."""
    db_path, mp = env
    node = {"type": "apply_to_each", "id": "n1", "skill_id": "no_such_skill",
            "items": ["a", "b"], "skill_args": {}}
    stub = StubModels(plan_nodes=[node]).install(mp)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-ORC-008"
    assert failure["message"].startswith("BRT-ORC-008: skill render failed")


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

RECORDED_PASSING_ENTRY = '{"result": "fine", "n": [1, 2], "nested": {"a": "b"}, "u": "\\u00e9"}'


@pytest.mark.parametrize("mode", ["plan", "re_plan", "compose"])
def test_passing_step_entry_is_byte_identical(mode):
    """Compared with an entry recorded by hand, not with the helper's own input."""
    from uuid import uuid4
    out = {"result": "fine", "n": [1, 2], "nested": {"a": "b"}, "u": "é"}
    passed = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status="resolved", output=out)
    shown = engine._stage_output_for_prompt(passed, mode)
    assert json.dumps(shown) == RECORDED_PASSING_ENTRY
    assert "failure" not in shown


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

    async def unstamped_failure(task, db_path):
        # A raw step's failure that never went through the lane's failed_envelope:
        # lane_guarded is None, the reason and the output carry the canary.
        return InboundEnvelope(
            envelope_id=task.envelope_id, subtask_id=task.subtask_id, status="failed",
            output={"note": CANARY}, failure_reason=f"BRT-ANS-004: {CANARY} could not be parsed",
        )

    stub = StubModels(plan_nodes=[node]).install(monkeypatch)
    monkeypatch.setattr(engine, "execute", unstamped_failure)
    await run_pipeline("Read the note.", db_path=db_path, documents={"folder": "."})
    await drain_log_writes()
    replan = stub.of_kind("re_plan")[0]
    body = json.dumps(replan.body)
    assert "BRT-ANS-004" in body
    assert CANARY not in body
    failure = _replan_block(stub)["n1"]
    assert failure == {"failure": {"code": "BRT-ANS-004", "doc": "doc_001"}}


# --- BRT-APE-006 guidance survives the 200-character cap -------------------

@pytest.mark.asyncio
async def test_ape_006_guidance_reaches_replan_for_an_80_character_ref(env):
    db_path, mp = env
    name = "reviews_" + "x" * 72
    assert len(name) == 80
    node = {"type": "apply_to_each", "id": "n1", "inputs_ref": name,
            "instruction": STEP, "task_type": "extraction", "tier": "light",
            "output_schema": {"result": "string"}}
    stub = StubModels(plan_nodes=[node]).install(mp)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure["code"] == "BRT-APE-006"
    assert "belongs in `items`" in failure["message"]
    assert "must name a step of this run, not a parent" in failure["message"]
    assert len(failure["message"]) <= 200


def test_ape_005_and_006_texts_fit_with_a_long_name_and_lead_with_guidance():
    from src.orchestrator import apply_to_each as ate
    for name in ("n" * 80, "0f8fad5b-d9cb-469f-a165-70867728950e", "x" * 500):
        for text in (ate.unresolved_ref_message(name), ate.failed_ref_message(name)):
            assert len(text) <= engine._FAILURE_MESSAGE_MAX, text
    assert "belongs in `items`" in ate.unresolved_ref_message("n" * 80)
    assert ate.unresolved_ref_message("abc").index("items") < ate.unresolved_ref_message("abc").index("abc")


# --- a failure the model reported: BRT-ANS-010 ------------------------------

def _model_says_failed(reason):
    async def fake_call(model, system_prompt, user_prompt, max_tokens=None, **kw):
        content = json.dumps({"status": "failed", "failure_reason": reason, "output": {}})
        return content, 10, 10, "stop"
    return fake_call


@pytest.mark.asyncio
async def test_model_reported_failure_gets_ans_010_never_the_code_it_wrote(env):
    from src.agents import executor
    db_path, mp = env
    stub = StubModels(plan_nodes=[_answerer()]).install(mp)
    mp.setattr(executor, "_call_model", _model_says_failed("BRT-ORC-004: I cannot do this"))
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    failure = _replan_block(stub)["n1"]["failure"]
    assert failure == {"code": "BRT-ANS-010", "message": "BRT-ORC-004: I cannot do this"}


def test_model_written_reason_is_never_parsed_as_a_code():
    from src.lane.context import mark_model_reason
    env_ = mark_model_reason(_failed("BRT-APE-006: I made this up"))
    assert engine._stage_output_for_prompt(env_, "re_plan")["failure"]["code"] == "BRT-ANS-010"


def test_model_reason_on_a_raw_step_is_never_parsed_either():
    from src.lane.context import mark_model_reason
    env_ = mark_model_reason(_failed("BRT-ANS-004: leaked text"))
    env_._data_scope = "raw"
    entry = engine._stage_output_for_prompt(env_, "re_plan")
    assert entry == {"failure": {"code": "BRT-LANE-004", "doc": None}}


# --- compose sees the code only ---------------------------------------------

@pytest.mark.asyncio
async def test_compose_request_has_the_failure_code_but_not_the_message(env):
    db_path, mp = env
    stub = StubModels(
        plan_nodes=[_answerer()],
        finish_for=lambda r: "length" if r.kind == "answerer" else "stop",
    ).install(mp)
    await run_pipeline("Summarise.", db_path=db_path)
    await drain_log_writes()
    assert stub.of_kind("re_plan")[0].system.count("model output truncated") >= 1
    compose = stub.of_kind("compose")
    assert compose, "compose was not called"
    assert "BRT-ANS-009" in compose[0].system
    assert "model output truncated" not in compose[0].system
