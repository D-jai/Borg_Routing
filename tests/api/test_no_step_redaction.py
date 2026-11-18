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
