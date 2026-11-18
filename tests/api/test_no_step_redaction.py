"""Ruling 26a review: a non-admin viewer with the lane on still sees the
BRT-ORC-033 event's code, failed-step count and step codes (codes and
document ids only), and nothing else from it."""
import json

from src.api.tasks import _redact_events


def _event(decision):
    return {"event_type": "error", "decision_json": json.dumps(decision)}


def test_brt_orc_033_keeps_code_count_and_step_codes_only():
    ev = _event({"code": "BRT-ORC-033", "severity": "warning",
                 "msg": "every planned step failed; quoted document text",
                 "failed_steps": 2,
                 "step_codes": ["BRT-ORC-004", {"code": "BRT-LANE-002", "doc": "doc_001,doc_002"}]})
    kept = json.loads(_redact_events([ev])[0]["decision_json"])
    assert kept == {"code": "BRT-ORC-033", "failed_steps": 2,
                    "step_codes": ["BRT-ORC-004", {"code": "BRT-LANE-002", "doc": "doc_001,doc_002"}]}


def test_a_step_code_that_is_not_a_code_never_leaves():
    ev = _event({"code": "BRT-ORC-033", "failed_steps": "3 secret",
                 "step_codes": ["BRT-ORC-004: the client said orchid", {"code": "BRT-LANE-002",
                                "doc": "orchid fourteen"}, 7]})
    kept = json.loads(_redact_events([ev])[0]["decision_json"])
    assert "orchid" not in json.dumps(kept) and "secret" not in json.dumps(kept)
    assert kept["code"] == "BRT-ORC-033" and "failed_steps" not in kept
    assert kept["step_codes"] == [{"code": "BRT-LANE-002"}]


def test_other_error_events_are_still_dropped():
    ev = _event({"code": "BRT-ORC-034", "msg": "doc text", "step_codes": ["BRT-ORC-004"]})
    assert _redact_events([ev])[0]["decision_json"] is None


def test_codes_and_document_ids_must_have_their_real_shape():
    ev = _event({"code": "BRT-ORC-033", "failed_steps": 2,
                 "step_codes": ["BRT-LANE-ACME-CORP", {"code": "BRT-LANE-002", "doc": "Smith"},
                                {"code": "BRT-LANE-002", "doc": "doc_001#2,doc_014"}]})
    kept = json.loads(_redact_events([ev])[0]["decision_json"])
    text = json.dumps(kept)
    assert "ACME" not in text and "Smith" not in text
    assert kept["step_codes"] == [{"code": "BRT-LANE-002"},
                                  {"code": "BRT-LANE-002", "doc": "doc_001#2,doc_014"}]


def test_a_step_failure_event_keeps_its_code_and_step_id_only():
    """Round 2: the receipt counts steps failed before any call from these events."""
    ev = _event({"code": "BRT-APE-005", "msg": "document text", "node_id": "n2"})
    assert json.loads(_redact_events([ev])[0]["decision_json"]) == {
        "code": "BRT-APE-005", "node_id": "n2"}
    warn = _event({"code": "BRT-ORC-019", "severity": "warning", "node_id": "n1", "msg": "x"})
    assert json.loads(_redact_events([warn])[0]["decision_json"]) == {
        "code": "BRT-ORC-019", "node_id": "n1", "severity": "warning"}
    for bad in ({"code": "BRT-APE-005", "node_id": "n2 the client said"},
                {"code": "Smith", "node_id": "n2"}):
        assert _redact_events([_event(bad)])[0]["decision_json"] is None
