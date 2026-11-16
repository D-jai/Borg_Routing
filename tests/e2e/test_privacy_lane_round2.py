"""On-premise lane: the gaps the round-2 reviewer found (16-Nov-2026).

The lead's ruling: BUILD.md section 8, row "Lane round-2 review". Each test
reproduces a probe the reviewer showed passing document text (or a
model-chosen failure code) toward a cloud model, or a Phase 1 difference
with the lane off.
"""
from __future__ import annotations

import json
import uuid

import pytest

from src.lane.backstop import DocIndex, find_leak
from src.orchestrator import engine
from tests.e2e.stub_model_server import lane_matrix, three_tier_matrix
from tests.e2e.test_privacy_lane import _run, lane  # noqa: F401  (fixture)
from tests.e2e.test_privacy_lane_review import CONTACTS, _cloud_with, _docs, _FailingLocal

WORDS = CONTACTS.split()
ZWSP = "​"
SHY = "­"


def _esc(text: str, depth: int) -> str:
    """``text`` JSON-escaped ``depth`` times, without the outer quotes."""
    for _ in range(depth):
        text = json.dumps(text)[1:-1]
    return text


def _answer(value):
    def answer(req):
        if req.is_local:
            return {"client": value, "risk": "high"}
        return {"result": f"done by {req.model}"}
    return answer


def _leak(body) -> object:
    return find_leak(body, [DocIndex("doc_001", CONTACTS, 120)], 120, 0.10)


# -- 1: a failure reason the local model wrote never travels upward -----------------

async def test_q1_a_model_written_failure_code_becomes_lane_004(lane):
    _docs(lane, {"contacts.txt": CONTACTS})

    class Stub(_FailingLocal):
        def _answer(self, url, payload):
            resp = super()._answer(url, payload)
            req = self.requests[-1]
            if req.is_local and req.kind == "answerer":
                content = {"status": "failed", "failure_reason": "BRT-QUILL-555013"}
                resp._body["choices"][0]["message"]["content"] = json.dumps(content)
            return resp

    stub, result = await _run(lane, "raw_local", lane_matrix(), stub_cls=Stub)
    for needle in ("QUILL", "555013"):
        assert not _cloud_with(stub, needle), f"{needle!r} from a model-written code reached a cloud model"
    assert "BRT-LANE-004" in stub.of_kind("re_plan")[0].system


async def test_q1_a_runtime_failure_code_still_passes(lane):
    """A code the runtime wrote (a truncated local answer: BRT-ANS-009) is
    shown upward as is."""
    _docs(lane, {"contacts.txt": CONTACTS})
    stub, result = await _run(
        lane, "raw_local", lane_matrix(),
        finish_for=lambda req: "length" if req.is_local else "stop",
    )
    replan = stub.of_kind("re_plan")[0].system
    assert "BRT-ANS-009" in replan


def test_q1_the_guard_tells_model_reasons_from_runtime_reasons():
    from src.envelopes.schema import InboundEnvelope, WrappedTask  # noqa: F401
    from src.lane.context import LaneContext, guard_envelope, mark_model_reason
    from src.lane.settings import LaneSettings

    ctx = LaneContext(settings=LaneSettings(mode="raw_local"), docs=None, db_path=":memory:")
    task = engine._build_task_from_node(
        {"type": "answerer", "id": "n1", "instruction": "x", "output_schema": {"r": "string"}},
        run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="http://127.0.0.1:11434", model_name="ollama/stub/local",
        user_instruction="x", results={},
    )

    def env(reason):
        return InboundEnvelope(envelope_id=task.envelope_id, subtask_id=task.subtask_id,
                               status="failed", output={}, failure_reason=reason)

    runtime = guard_envelope(env("BRT-ANS-009: model output truncated"), task, ctx)
    assert json.loads(runtime.failure_reason)["code"] == "BRT-ANS-009"
    model = guard_envelope(mark_model_reason(env("BRT-ANS-009: looks like ours")), task, ctx)
    assert json.loads(model.failure_reason)["code"] == "BRT-LANE-004"


def test_q1_raw_failure_code_reads_only_a_leading_code():
    from src.lane.context import raw_failure_code

    assert raw_failure_code("BRT-ANS-003: bad envelope. Raw: {'x': 'BRT-QUILL-1'}") == "BRT-ANS-003"
    assert raw_failure_code("model said: BRT-ABERNATHY-3 cannot") == "BRT-LANE-004"
    assert raw_failure_code("BRT-QUILL-555013") == "BRT-QUILL-555013"  # shape alone; source decides


# -- 2: un-escape to a fixed point ----------------------------------------------------

@pytest.mark.parametrize("depth", [4, 8])
def test_q2_contacts_escaped_many_times_inside_prose_are_caught(depth):
    assert _leak({"m": "Context: " + _esc(CONTACTS, depth) + " end"}) is not None


async def test_q2_escape_depth_four_never_reaches_a_cloud_model(lane):
    _docs(lane, {"contacts.txt": CONTACTS})
    stub, result = await _run(lane, "raw_local", lane_matrix(),
                              answer_for=_answer("note: " + _esc(CONTACTS, 4)))
    assert not _cloud_with(stub, "CANARY0013")


# -- 3: format characters (Unicode Cf) are deleted before normalising ----------------

@pytest.mark.parametrize("mark", [ZWSP, SHY], ids=["zero_width", "soft_hyphen"])
def test_q3_a_format_character_after_every_letter_is_caught(mark):
    spaced = "".join(c + mark if c.isalpha() else c for c in CONTACTS)
    assert _leak({"m": spaced}) is not None


async def test_q3_zero_width_copy_never_reaches_a_cloud_model(lane):
    _docs(lane, {"contacts.txt": CONTACTS})
    spaced = "".join(c + ZWSP if c.isalpha() else c for c in CONTACTS)
    stub, result = await _run(lane, "raw_local", lane_matrix(), answer_for=_answer(spaced))
    hits = [r for r in stub.requests if not r.is_local
            and "CANARY0013" in json.dumps(r.body, ensure_ascii=False).replace(ZWSP, "")]
    assert not hits


# -- 4: a values-only line catches a copy split one word per value -------------------

def test_q4_one_word_per_value_under_separate_keys_is_caught():
    assert _leak({"m": {f"k{i}": w for i, w in enumerate(WORDS)}}) is not None


async def test_q4_word_per_key_copy_never_reaches_a_cloud_model(lane):
    _docs(lane, {"contacts.txt": CONTACTS})
    stub, result = await _run(lane, "raw_local", lane_matrix(),
                              answer_for=_answer({f"k{i}": w for i, w in enumerate(WORDS)}))
    assert not _cloud_with(stub, "CANARY0013")


def test_q4_one_word_per_key_name_stays_a_stated_limit():
    assert _leak({"m": {w + f"_{i}": i for i, w in enumerate(WORDS)}}) is None


# -- 5: ensure_ascii as in Phase 1 with the lane off; surrogates cleaned when on -----

def test_q5_lane_off_prompt_json_is_phase_one_form():
    from src.lane.context import prompt_json

    value = {"n1": {"client": "Zoë Müller"}}
    assert prompt_json(value) == json.dumps(value)
    assert prompt_json(value, indent=2) == json.dumps(value, indent=2)
    assert engine._safe_sub("{x}", {"x": value}) == json.dumps(value)


async def test_q5_lane_off_dependent_step_sees_the_phase_one_block(lane):
    plan = [
        {"type": "answerer", "id": "n1", "instruction": "Name the client.",
         "task_type": "extraction", "tier": "light", "output_schema": {"client": "string"}},
        {"type": "answerer", "id": "n2", "depends_on": ["n1"], "instruction": "Greet the client.",
         "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}},
    ]
    out = {"client": "Zoë Müller"}
    stub, result = await _run(lane, "off", three_tier_matrix(), plan=plan, documents=None,
                              answer_for=lambda req: out)
    assert result["status"] == "completed", result.get("error")
    step = stub.step_request("Greet the client.")
    assert json.dumps({"n1": out}, indent=2) in step.system + step.user
    assert "Zoë" not in step.system + step.user


async def test_q5_lane_on_a_lone_surrogate_does_not_break_the_planner_call(lane):
    from tests.e2e.stub_model_server import StubModels

    class Encoding(StubModels):
        def _answer(self, url, payload):
            json.dumps(payload, ensure_ascii=False).encode("utf-8")   # what httpx does
            return super()._answer(url, payload)

    stub, result = await _run(lane, "raw_local", lane_matrix(), stub_cls=Encoding,
                              answer_for=_answer("Ada \ud800 Quill"))
    assert stub.of_kind("re_plan"), "the planner was never called again"
    assert result["status"] == "completed", result.get("error")
