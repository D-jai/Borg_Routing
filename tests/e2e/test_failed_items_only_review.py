"""failed_items_only, review round 1 (planner ruling 32d(i)): the lane, item
identity for readers, the ignored-flag note, retry of a retry, the zero-item
step through the engine loop, and packing."""
import json
import sqlite3
import uuid

import pytest

from src.envelopes.schema import InboundEnvelope
from src.lane.context import LaneContext
from src.orchestrator import engine, row_markers
from src.orchestrator.apply_to_each import expand_and_run
from tests.e2e.stub_model_server import StubModels, lane_matrix, three_tier_matrix
from tests.e2e.test_item_packing import _items as pack_items, pk, _run as pack_run  # noqa: F401
from tests.e2e.test_privacy_lane import (  # noqa: F401  (``lane`` is a fixture)
    CANARY_ACCOUNT,
    CANARY_BIRCH,
    EXTRACT,
    PLAN_RAW,
    _run,
    lane,
)

RETRY = EXTRACT + " RETRY-PASS"
N1 = dict(PLAN_RAW[0])
R1 = {"type": "apply_to_each", "id": "r1", "inputs_ref": "n1", "failed_items_only": True,
      "instruction": RETRY, "task_type": "extraction", "tier": "light",
      "output_schema": {"client": "string", "risk": "string"}}
MODES = [("raw_local", lane_matrix), ("by_reference", three_tier_matrix)]


def _events(db, code):
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT decision_json FROM logs WHERE event_type='error'").fetchall()
    out = [json.loads(r[0] or "{}") for r in rows]
    return [d for d in out if d.get("code") == code]


def _answerer(acme_fails):
    """Acme's note answers without 'risk' (an item failure) while acme_fails()."""
    def answer(req):
        if EXTRACT in req.system:
            if CANARY_ACCOUNT in req.system:
                if acme_fails(req):
                    return {"client": "Acme Holdings"}
                return {"client": "Acme Holdings", "risk": "high"}
            return {"client": "Birch Logistics", "risk": "low"}
        return {"result": "done"}
    return answer


@pytest.fixture
def spy(monkeypatch):
    seen: dict = {}
    real = engine._fail_if_no_document_step

    async def wrapper(results, run_id, db_path):
        seen["results"] = dict(results)
        return await real(results, run_id, db_path)

    monkeypatch.setattr(engine, "_fail_if_no_document_step", wrapper)
    return seen


# -- 1. the lane ---------------------------------------------------------------------

@pytest.mark.parametrize("mode,matrix", MODES)
async def test_lane_retry_loads_each_failed_document_for_its_child(lane, spy, mode, matrix):
    first_try = {"done": False}

    def acme_fails(req):
        if RETRY in req.system:
            return False          # the retry succeeds
        first_try["done"] = True
        return True

    stub, result = await _run(lane, mode, matrix(), plan=[N1, R1],
                              answer_for=_answerer(acme_fails))
    assert result["status"] == "completed", result.get("error")
    retry_calls = [r for r in stub.of_kind("answerer") if RETRY in r.system]
    assert len(retry_calls) == 1, "only the failed document is retried"
    assert CANARY_ACCOUNT in retry_calls[0].system, "the document text was loaded for its child"
    assert CANARY_BIRCH not in retry_calls[0].system
    env = spy["results"]["r1"]
    assert env.output["success_count"] == 1 and env.output["results"][0]["item"] == "doc_001"
    assert env._doc_label == "doc_001"


@pytest.mark.parametrize("mode,matrix", MODES)
async def test_lane_retry_failing_again_reports_part_ids_not_doc_none(lane, spy, mode, matrix):
    # Both documents fail every time: no step finishes, so BRT-ORC-034 lists
    # the failure codes of n1 and r1, each with its document ids.
    def bad(req):
        return {"client": "x"} if EXTRACT in req.system else {"result": "done"}

    stub, result = await _run(lane, mode, matrix(), plan=[N1, R1], answer_for=bad)
    assert result["status"] == "failed"
    events = _events(lane["db"], "BRT-ORC-034")
    assert len(events) == 1
    codes = events[0]["step_codes"]
    assert len(codes) >= 2 and all(isinstance(c, dict) for c in codes), codes
    assert all(c["doc"] for c in codes), codes
    assert {c["doc"] for c in codes} == {"doc_001,doc_002"}, codes


# -- 5. zero items, through the engine loop ------------------------------------------

@pytest.mark.parametrize("mode,matrix", MODES)
async def test_lane_zero_item_retry_is_a_finished_guarded_step(lane, spy, mode, matrix):
    stub, result = await _run(lane, mode, matrix(), plan=[N1, R1],
                              answer_for=_answerer(lambda req: False))
    assert result["status"] == "completed", result.get("error")
    assert _events(lane["db"], "BRT-ORC-034") == []
    env = spy["results"]["r1"]
    assert env.status == "resolved" and env.output["total"] == 0
    # The value the normal path computes with nothing failed: True for a raw
    # step, None otherwise (in raw_local a step reading a guarded step is not raw).
    raw = env._data_scope == "raw"
    assert env.lane_guarded is (True if raw else None)
    if mode == "by_reference":
        assert raw and env.lane_guarded is True
    assert engine._step_finished(env)
    assert not [r for r in stub.of_kind("answerer") if RETRY in r.system]
    steps = row_markers.list_steps(spy["results"], LaneContext(
        settings=lane_settings(mode), docs=None, db_path=":memory:"))
    assert "- r1: 0 rows" in row_markers.step_lines(steps)


def lane_settings(mode):
    from src.lane.settings import LaneSettings
    return LaneSettings(mode=mode)


# -- 2. identity for readers -----------------------------------------------------------

def _agg(items, failing):
    results = [{"index": i, "item": it, "output": {"bucket": "work"}, "confidence": 0.9}
               for i, it in enumerate(items) if i not in failing]
    failures = [{"index": i, "item": items[i], "reason": "BRT-ANS-004: bad"} for i in sorted(failing)]
    env = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(), status="resolved",
                          output={"results": results, "failures": failures, "total": len(items),
                                  "success_count": len(results), "failure_count": len(failures)})
    env._node_type = "apply_to_each"
    return env


def _node(**extra):
    return {"id": "r", "type": "apply_to_each", "inputs_ref": "first", "failed_items_only": True,
            "instruction": "x", "output_schema": {}, **extra}


def _build(node, results):
    return engine._build_task_from_node(
        node, run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="http://127.0.0.1:11434", model_name="ollama/stub/local",
        user_instruction="x", results=results)


async def _retry_env(first, db, node=None, fail_local=()):
    task = _build(node or _node(), {"first": first})
    from unittest.mock import patch

    async def dispatch(child, db_path):
        idx = int(child.trigger_reason.split("item_")[1])
        if idx in fail_local:
            return InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=child.subtask_id,
                                   status="failed", output={}, failure_reason="BRT-ANS-004: bad")
        return InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=child.subtask_id,
                               status="resolved", output={"bucket": "home"}, confidence=0.9)

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=dispatch):
        env = await expand_and_run(task, db_path=db)
    env._node_type = "apply_to_each"
    return env


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all
    path = str(tmp_path / "r.db")
    await create_all(path)
    return path


async def test_rows_for_a_retry_names_rows_by_the_original_index(db):
    items = [{"text": t} for t in "abcd"]
    first = _agg(items, {2, 3})
    retry = await _retry_env(first, db, fail_local={1})
    rows = row_markers.rows_for(retry, None)
    assert [r["item"] for r in rows] == [2, 3]
    assert "error" not in rows[0] and "error" in rows[1]


async def test_stage_results_and_salvage_views_show_the_original_index(db):
    first = _agg([{"text": t} for t in "abcd"], {2, 3})
    retry = await _retry_env(first, db)
    shown = engine._stage_output_for_prompt(retry)
    assert [r["original_index"] for r in shown["results"]] == [2, 3]
    text = engine._salvage_finished_results({"r": retry})
    rows = json.loads(text[text.index("["):text.rindex("]") + 1])
    assert [r["index"] for r in rows] == [2, 3]


# -- 3. no silent ignore ---------------------------------------------------------------

async def test_flag_on_a_state_op_runs_all_items_with_a_note_and_a_warning(db):
    state = InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=uuid.uuid4(), status="resolved",
                            output={"result": ["a", "b", "c"]})
    task = _build(_node(inputs_ref="s"), {"s": state})
    task = task.model_copy(update={"run_id": uuid.uuid4()})
    from unittest.mock import patch

    async def dispatch(child, db_path):
        return InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=child.subtask_id,
                               status="resolved", output={"bucket": "home"}, confidence=0.9)

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=dispatch):
        env = await expand_and_run(task, db_path=db)
    from tests.e2e.stub_model_server import drain_log_writes
    await drain_log_writes()
    assert env.output["total"] == 3
    assert "failed_items_only ignored: s is not a per-item step; all items run" in env.output["notes"]
    assert _events(db, "BRT-APE-008")


@pytest.mark.parametrize("flag", ["true", "True", "TRUE"])
async def test_the_flag_as_a_string_counts(db, flag):
    first = _agg([f"n{i}" for i in range(6)], {1, 4})
    task = _build(_node(failed_items_only=flag), {"first": first})
    assert task.dependency_input["items"] == ["n1", "n4"]
    task = _build({"id": "r", "type": "apply_to_each", "inputs_ref": "first", "instruction": "x",
                   "output_schema": {}, "skill_args": {"failed_items_only": flag}}, {"first": first})
    assert task.dependency_input["items"] == ["n1", "n4"]


# -- 4. retry of a retry ---------------------------------------------------------------

async def test_a_retry_of_a_retry_points_back_to_the_first_step(db):
    first = _agg([f"n{i}" for i in range(10)], {1, 4, 5, 9})
    retry1 = await _retry_env(first, db, fail_local={2})
    assert [r["original_index"] for r in retry1.output["failures"]] == [5]
    task = _build({**_node(inputs_ref="retry1")}, {"retry1": retry1})
    assert task.dependency_input["items"] == ["n5"]
    assert task.dependency_input["original_indexes"] == [5]


# -- 6. packing ------------------------------------------------------------------------

async def test_packed_retry_entries_carry_the_right_original_index(pk):
    items = pack_items(12)
    first = _agg(items, {1, 4, 5, 9, 10})
    node = {"id": "r", "type": "apply_to_each", "inputs_ref": "first", "failed_items_only": True,
            "instruction": "Give the kind of this note: a, b or c.", "output_schema": {"kind": "a|b|c"}}
    task = engine._build_task_from_node(
        node, run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="https://openrouter.ai/api", model_name="openrouter/stub/cheap",
        user_instruction="Sort the notes.", results={"first": first})
    assert task.dependency_input["items"] == [items[i] for i in (1, 4, 5, 9, 10)]

    def reply(req):
        out = {iid: {"status": "resolved", "confidence": 0.8, "output": {"kind": "b"}}
               for iid in req.pack_ids}
        out[req.pack_ids[1]] = {"status": "failed", "confidence": 0.0, "output": {},
                                "failure_reason": "cannot tell"}
        return out

    stub, env = await pack_run(pk, task, pack_for=reply)
    assert stub.of_kind("packed"), "the retry was packed"
    assert [r["original_index"] for r in env.output["results"]] == [1, 5, 9, 10]
    assert [f["original_index"] for f in env.output["failures"]] == [4]
    assert env.output["failures"][0]["index"] == 1
