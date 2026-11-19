"""Work order 4.5: the committed replay exports (demo/runs/*.json) hold no canary, no raw
document text, no key and no absolute local path; the lane run carries the non-admin lane
redaction; every answer has the length its run's floor was priced on."""
import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
EXPORTS = sorted((ROOT / "demo" / "runs").glob("job*_*.json"))
NOTES = ROOT / "demo" / "data" / "job3_notes"
CANARY = "CANARY-7f3a91-ZEBRA-LANTERN"


def _mod():
    spec = importlib.util.spec_from_file_location("export_run", ROOT / "scripts" / "export_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _documents():
    return {f"job3_notes/{p.name}": p.read_text(encoding="utf-8") for p in sorted(NOTES.glob("*.txt"))}


def test_the_three_ruling_24_runs_are_exported_and_the_dropped_ones_are_not():
    names = [p.name for p in EXPORTS]
    assert names == ["job1_c882e3c9.json", "job2_6c83c9c7.json", "job3_e31a5330.json"]


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_an_export_holds_no_canary_document_text_key_or_path(path):
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["label"] == "demo run" and doc["format"] == "borg_route.replay/1"
    assert CANARY not in path.read_text(encoding="utf-8")
    assert _mod().problems(doc, _documents(), [CANARY]) == []


def test_the_checker_would_catch_a_note_line_and_the_canary():
    """Check the checker: the same check flags an export that holds them."""
    mod = _mod()
    docs = _documents()
    line = next(l for t in docs.values() for l in t.splitlines() if len(l) >= 40)
    codes = {c for c, _ in mod.problems({"answer": "x " + line}, docs, [])}
    assert codes == {"BRT-RPL-011"}
    codes = {c for c, _ in mod.problems({"answer": "ref " + CANARY}, {}, [])}
    assert codes == {"BRT-RPL-010"}


def test_the_lane_run_export_is_what_a_non_admin_sees_with_the_lane_on():
    from src.api.tasks import _redact_events

    doc = json.loads((ROOT / "demo" / "runs" / "job3_e31a5330.json").read_text(encoding="utf-8"))
    assert doc["lane_mode"] == "raw_local" and doc["redaction"] == "lane_viewer"
    events = [{k: v for k, v in ev.items() if k != "t_ms"} for ev in doc["view"]["events"]]
    assert _redact_events(events) == events
    assert all(o["body"] is None for o in doc["view"]["outbound"])


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_an_answer_has_the_length_its_floor_was_priced_on(path):
    mod = _mod()
    doc = json.loads(path.read_text(encoding="utf-8"))
    docs = json.loads(doc["view"]["run"].get("documents") or "[]")
    want = mod.floor_answer_chars(doc["view"]["digest"], doc["prompt"],
                                  sum(d["chars"] for d in docs), mod.load_matrix())
    assert want is not None and abs(len(doc["answer"]) - want) < 0.5


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_no_committed_export_carries_a_benchmark_figure(path):
    """Lead ruling (review item 4): the round-1 ratio is nulled at export."""
    doc = json.loads(path.read_text(encoding="utf-8"))
    assert doc["view"]["digest"]["direct_floor_ratio"] is None
    assert "round-1" not in path.read_text(encoding="utf-8").lower()


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_the_recorded_date_is_the_toronto_date(path):
    from datetime import datetime

    from src.api.replay import toronto_date

    doc = json.loads(path.read_text(encoding="utf-8"))
    started = datetime.fromisoformat(doc["view"]["run"]["created_at"])
    assert doc["recorded_on"] == toronto_date(started).isoformat()
