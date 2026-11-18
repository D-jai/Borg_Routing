"""failed_items_only (planner ruling 32d(i)): a retry apply_to_each reads only
the FAILED items of the apply_to_each step it points at, with their original
inputs, in original order (bench round 2 job D: classify_notes, 29 results and
31 failures; the retry re-ran the 29 that had passed).
"""
import uuid
from unittest.mock import patch

import pytest

from src.db.schema import create_all
from src.envelopes.schema import InboundEnvelope
from src.orchestrator import engine
from src.orchestrator.apply_to_each import expand_and_run

BUCKETS = ["work", "home", "other"]


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "foi.db")
    await create_all(path)
    return path


def _ok(task):
    return InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=task.subtask_id,
                           status="resolved", output={"bucket": "work"}, confidence=0.9)


def _bad(task):
    return InboundEnvelope(envelope_id=uuid.uuid4(), subtask_id=task.subtask_id,
                           status="failed", output={}, failure_reason="reply missing key 'bucket'")


def _dispatch(fail_items, seen):
    """Stub model: fails the chosen items (by their text), records every item run."""
    async def mock(child, db_path):
        seen.append(child.context)
        return _bad(child) if child.context in {str(i) for i in fail_items} else _ok(child)
    return mock


def _build(node, results):
    return engine._build_task_from_node(
        node, run_id=uuid.uuid4(), current_depth=1, max_depth=5, level_path="1a",
        model_endpoint="http://127.0.0.1:11434", model_name="ollama/stub/local",
        user_instruction="x", results=results,
    )


def _first_node(items):
    return {"id": "classify_notes", "type": "apply_to_each", "items": items,
            "instruction": "Classify.", "output_schema": {"bucket": "string"}}


async def _run(task, db, fail_items, seen):
    with patch("src.orchestrator.apply_to_each._dispatch_child", new=_dispatch(fail_items, seen)):
        return await expand_and_run(task, db_path=db)


async def _first(items, fail_items, db):
    seen: list = []
    env = await _run(_build(_first_node(items), {}), db, fail_items, seen)
    assert env.output["failure_count"] == len(fail_items)
    return env


ITEMS = [f"note {i}" for i in range(10)]
FAILING = ["note 1", "note 4", "note 5", "note 9"]


async def test_skill_step_retries_only_failed_items_in_order(db):
    first = await _first(ITEMS, FAILING, db)
    node = {"id": "retry_failed_notes", "type": "apply_to_each", "skill_id": "classify_into_buckets",
            "skill_args": {"buckets": BUCKETS, "items_ref": "classify_notes", "failed_items_only": True}}
    task = _build(node, {"classify_notes": first})
    assert task.dependency_input["items"] == FAILING
    seen: list = []
    out = await _run(task, db, [], seen)
    assert seen == FAILING
    assert out.output["total"] == 4 and out.output["success_count"] == 4
    assert [r["item"] for r in out.output["results"]] == FAILING


async def test_retried_results_keep_the_original_item_identity(db):
    first = await _first(ITEMS, FAILING, db)
    node = {"id": "r", "type": "apply_to_each", "inputs_ref": "classify_notes",
            "failed_items_only": True, "instruction": "Classify.", "output_schema": {"bucket": "string"}}
    out = await _run(_build(node, {"classify_notes": first}), db, [], [])
    assert [r["item"] for r in out.output["results"]] == FAILING
    assert [r["original_index"] for r in out.output["results"]] == [1, 4, 5, 9]


async def test_node_level_flag_and_skill_args_flag_agree(db):
    first = await _first(ITEMS, FAILING, db)
    at_node = _build({"id": "a", "type": "apply_to_each", "inputs_ref": "classify_notes",
                      "failed_items_only": True, "instruction": "x", "output_schema": {}},
                     {"classify_notes": first})
    in_args = _build({"id": "b", "type": "apply_to_each", "inputs_ref": "classify_notes",
                      "skill_args": {"failed_items_only": True}, "instruction": "x", "output_schema": {}},
                     {"classify_notes": first})
    assert at_node.dependency_input["items"] == FAILING
    assert in_args.dependency_input["items"] == FAILING


async def test_no_failures_resolves_with_zero_items_and_says_so(db):
    first = await _first(ITEMS, [], db)
    node = {"id": "r", "type": "apply_to_each", "skill_id": "classify_into_buckets",
            "skill_args": {"buckets": BUCKETS, "items_ref": "classify_notes", "failed_items_only": True}}
    seen: list = []
    out = await _run(_build(node, {"classify_notes": first}), db, [], seen)
    assert out.status == "resolved" and seen == []
    assert out.output["total"] == 0 and out.output["results"] == []
    assert any("no failed items" in n for n in out.output["notes"])


async def test_without_the_flag_the_old_behaviour_stays(db):
    first = await _first(ITEMS, FAILING, db)
    # Pinned: no flag reads the step's "results", i.e. the items that passed.
    plain = _build({"id": "r", "type": "apply_to_each", "inputs_ref": "classify_notes",
                    "instruction": "x", "output_schema": {}}, {"classify_notes": first})
    assert plain.dependency_input["items"] == first.output["results"]
    off = _build({"id": "r", "type": "apply_to_each", "inputs_ref": "classify_notes",
                  "failed_items_only": False, "instruction": "x", "output_schema": {}},
                 {"classify_notes": first})
    assert off.dependency_input["items"] == first.output["results"]
    assert "original_indexes" not in plain.dependency_input


async def test_document_items_keep_their_handles(db):
    handles = [f"doc_00{d}#{p}" for d in (1, 2, 3) for p in (1, 2)]
    failing = ["doc_001#2", "doc_003#1"]
    first = await _first(handles, failing, db)
    node = {"id": "r", "type": "apply_to_each", "inputs_ref": "classify_notes",
            "failed_items_only": True, "instruction": "x", "output_schema": {}}
    task = _build(node, {"classify_notes": first})
    assert task.dependency_input["items"] == failing
    out = await _run(task, db, [], [])
    assert [r["item"] for r in out.output["results"]] == failing


async def test_dict_items_keep_their_original_value(db):
    items = [{"id": f"n{i}", "text": f"t{i}"} for i in range(5)]
    first = await _first(items, [items[2], items[3]], db)
    task = _build({"id": "r", "type": "apply_to_each", "inputs_ref": "classify_notes",
                   "failed_items_only": True, "instruction": "x", "output_schema": {}},
                  {"classify_notes": first})
    assert task.dependency_input["items"] == [items[2], items[3]]
