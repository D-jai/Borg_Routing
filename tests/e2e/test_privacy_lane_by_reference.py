"""Work order 2.4: lane_mode ``by_reference``.

Documents are handed over exactly as in ``raw_local`` (``BORG_DOCS_ROOT``,
the planner sees a manifest only, text loaded inside the step that reads it,
parts for long documents), but any checked model may read them, the output
guard and the content backstop do not run, and every request is still in
``outbound_requests``. It makes no privacy promise.

The second half proves ``off``, ``raw_local`` and ``all_local`` decide as
before; the full proof is the unchanged privacy suites
(``test_privacy_lane.py``, ``test_privacy_lane_review.py``,
``test_privacy_lane_round2.py``).
"""
from __future__ import annotations

import json
import sqlite3

import pytest

from src.lane.documents import load_documents
from tests.e2e.stub_model_server import lane_matrix, three_tier_matrix
from tests.e2e.test_privacy_lane import (  # noqa: F401  (``lane`` is a fixture)
    ACME_TEXT,
    BIRCH_TEXT,
    CANARIES,
    CANARY_ACCOUNT,
    CANARY_BIRCH,
    EXTRACT,
    OPERATOR,
    PLAN_RAW,
    SUMMARY,
    _error_codes,
    _ledger,
    _route_of,
    _run,
    lane,
)

_OLD_PROMISE = "steps that read them run on the operator's own machines"


def _cloud_extract(req):
    """A cloud model answering the per-document step."""
    if EXTRACT in req.system:
        if CANARY_ACCOUNT in req.system:
            return {"client": "Acme Holdings", "risk": "high"}
        return {"client": "Birch Logistics", "risk": "low"}
    return {"result": f"done by {req.model}"}


def _lane_codes(db_path, run_id) -> list[str]:
    return [c for c in _error_codes(db_path, run_id) if c and c.startswith("BRT-LANE-")]


def _item_requests(stub) -> list:
    return [r for r in stub.of_kind("answerer") if EXTRACT in r.system]


# -- by_reference -------------------------------------------------------------

async def test_by_reference_documents_are_read_by_a_cloud_model(lane):
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=_cloud_extract)
    assert result["status"] == "completed", result.get("error")

    # One call per document, each on a cloud model, each carrying its own text.
    items = _item_requests(stub)
    assert len(items) == 2
    assert all(not r.is_local for r in items)
    acme = next(r for r in items if CANARY_ACCOUNT in r.system)
    birch = next(r for r in items if CANARY_BIRCH in r.system)
    assert "relationship desk" in acme.system and CANARY_BIRCH not in acme.system
    assert "warehouse contract" in birch.system and CANARY_ACCOUNT not in birch.system

    # No step was refused for reading documents.
    assert _lane_codes(lane["db"], result["run_id"]) == []
    assert "local_only" not in _route_of(lane["db"], "n1")

    # Every request is in the ledger, with left_machine true.
    rows = _ledger(lane["db"])
    assert len(rows) == len(stub.requests)
    unmatched = list(rows)
    for req in stub.requests:
        hit = next(
            (row for row in unmatched
             if json.loads(row["body"]) == req.body and row["url"] == req.url),
            None,
        )
        assert hit is not None, f"no ledger row for the {req.kind} call to {req.url}"
        assert hit["left_machine"] == 1
        assert hit["status"] == "sent"
        unmatched.remove(hit)


async def test_by_reference_planner_gets_a_manifest_only_and_honest_wording(lane):
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=_cloud_extract)
    plan_req = stub.of_kind("plan")[0]
    assert "doc_001" in plan_req.system and "acme_note.txt" in plan_req.system
    for canary in CANARIES:
        assert canary not in plan_req.system
    assert "relationship desk" not in plan_req.system
    assert "warehouse contract" not in plan_req.system
    # The manifest must not claim the text stays on the machine.
    assert _OLD_PROMISE not in plan_req.system
    assert "may be cloud models" in plan_req.system


async def test_by_reference_long_documents_go_in_parts(lane):
    # 300 characters: the Acme note goes in two parts, the Birch note whole.
    lane["mp"].setenv("BORG_LANE_PART_CHARS", "300")
    expected = load_documents({"folder": "."}, part_chars=300)
    parts = [p for d in expected.documents for p in d.parts]
    assert len(parts) == 3, "the Acme note should be split into two parts"
    assert "doc_001#2" in [p.part_id for p in parts]

    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=_cloud_extract)
    assert result["status"] == "completed", result.get("error")
    items = _item_requests(stub)
    assert len(items) == len(parts), (
        f"{len(items)} item requests for {len(parts)} parts; "
        f"errors: {_error_codes(lane['db'], result['run_id'])}"
    )
    assert all(not r.is_local for r in items)
    for part in parts:
        hits = [r for r in items if part.text in r.system]
        assert len(hits) == 1, f"part {part.part_id} not sent exactly once"
    assert all(r["left_machine"] == 1 for r in _ledger(lane["db"]))


async def test_by_reference_copy_checks_do_not_block(lane):
    """The output guard and the content backstop do not run: a step may copy
    the document and a later cloud step receives the copy."""
    def copying(req):
        if EXTRACT in req.system:
            text = ACME_TEXT if CANARY_ACCOUNT in req.system else BIRCH_TEXT
            return {"client": text[:300], "risk": "high"}
        return {"result": "summary"}

    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=copying)
    assert result["status"] == "completed", result.get("error")
    summary = stub.step_request(SUMMARY)
    assert CANARY_ACCOUNT in summary.system
    codes = _lane_codes(lane["db"], result["run_id"])
    assert "BRT-LANE-004" not in codes and "BRT-LANE-006" not in codes


async def test_by_reference_pasted_document_is_not_refused(lane):
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=_cloud_extract,
                              instruction=OPERATOR + "\n\n" + ACME_TEXT.upper())
    assert result["status"] == "completed", result.get("error")
    assert "BRT-LANE-006" not in _lane_codes(lane["db"], result["run_id"])


@pytest.mark.parametrize("files", [["../outside.txt"], ["sub/../../outside.txt"]])
async def test_by_reference_paths_outside_the_root_are_refused(lane, files):
    (lane["tmp"] / "outside.txt").write_text("outside", encoding="utf-8")
    (lane["docs"] / "sub").mkdir(exist_ok=True)
    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              documents={"files": files})
    assert result["status"] == "failed"
    assert "BRT-LANE-001" in result["error"]
    assert stub.requests == []


async def test_by_reference_is_a_valid_setting():
    from src.api.admin import _EFFECTIVE_SETTINGS
    from src.lane.settings import validate_value

    assert validate_value("lane_mode", "by_reference") is None
    assert validate_value("lane_mode", "by_ref") is not None
    spec = next(r for r in _EFFECTIVE_SETTINGS if r["key"] == "lane_mode")
    assert "by_reference" in spec["choices"]


async def test_by_reference_run_is_not_resumed(lane):
    from fastapi import BackgroundTasks, HTTPException

    from src.api import tasks as tasks_api

    stub, result = await _run(lane, "by_reference", three_tier_matrix(),
                              answer_for=_cloud_extract)
    lane["mp"].setattr(tasks_api, "DB_PATH", lane["db"])
    # Only a failed run is offered "Run again": mark this one failed.
    with sqlite3.connect(lane["db"]) as conn:
        conn.execute("UPDATE runs SET status='failed' WHERE run_id=?", (str(result["run_id"]),))
    with pytest.raises(HTTPException) as info:
        await tasks_api.resume_run_endpoint(str(result["run_id"]), BackgroundTasks())
    assert info.value.detail["error"] == "BRT-LANE-008"


# -- the other modes decide as before ----------------------------------------------

@pytest.mark.parametrize("mode", ["raw_local", "all_local"])
async def test_raw_local_and_all_local_keep_their_wording_and_local_reads(lane, mode):
    plan = PLAN_RAW if mode == "raw_local" else [dict(PLAN_RAW[0]), dict(PLAN_RAW[1], tier="light")]
    stub, result = await _run(lane, mode, lane_matrix(), plan=plan)
    assert result["status"] == "completed", result.get("error")
    assert _OLD_PROMISE in stub.of_kind("plan")[0].system
    items = _item_requests(stub)
    assert len(items) == 2 and all(r.is_local for r in items)
    assert _route_of(lane["db"], "n1").get("local_only") is True
    for req in stub.requests:
        if not req.is_local:
            assert not any(c in json.dumps(req.body) for c in CANARIES)


async def test_raw_local_with_a_cloud_only_pool_still_refuses(lane):
    """The same run that by_reference completes is refused in raw_local."""
    stub, result = await _run(lane, "raw_local", three_tier_matrix(),
                              answer_for=_cloud_extract)
    assert _item_requests(stub) == []
    assert "BRT-LANE-002" in _error_codes(lane["db"], result["run_id"])


async def test_raw_local_backstop_still_refuses_a_pasted_document(lane):
    stub, result = await _run(lane, "raw_local", lane_matrix(),
                              instruction=OPERATOR + "\n\n" + ACME_TEXT.upper())
    assert result["status"] == "failed"
    assert "BRT-LANE-006" in result["error"]


async def test_off_still_refuses_documents(lane):
    stub, result = await _run(lane, "off", three_tier_matrix())
    assert result["status"] == "failed"
    assert "BRT-LANE-005" in result["error"]
    assert stub.requests == []
