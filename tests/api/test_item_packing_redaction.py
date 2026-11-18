"""Planner ruling 23: run detail for a non-admin while the lane is on keeps the
pack counts (ids and numbers only), so "N items in M calls" and the call count
stay right; every other decision text is still dropped."""
import json

from src.api.tasks import _redact_events


def test_pack_data_survives_redaction_and_nothing_else_does():
    pack = {"call": "c1", "step": "s1", "size": 2, "position": 0, "cut": False, "accepted": True,
            "first": True, "share_picodollars": 5, "share_baseline_picodollars": None,
            "call_tokens_in": 10, "call_tokens_out": 4, "call_picodollars": 9,
            "call_baseline_picodollars": None}
    item = {"event_type": "model_call", "decision_json": json.dumps({"pack": pack, "note": "secret text"})}
    outcome = {"event_type": "orchestrator_decision",
               "decision_json": json.dumps({"mode": "pack_outcome", "step": "s1", "call": "c1",
                                            "items": 2, "accepted": 2, "alone": 0})}
    error = {"event_type": "error", "decision_json": json.dumps({"code": "BRT-X", "msg": "doc text"})}
    plain = {"event_type": "model_call", "decision_json": json.dumps({"code": "BRT-ANS-009"})}

    kept = _redact_events([item, outcome, error, plain])
    assert json.loads(kept[0]["decision_json"]) == {"pack": pack}
    assert json.loads(kept[1]["decision_json"])["mode"] == "pack_outcome"
    assert kept[2]["decision_json"] is None
    assert kept[3]["decision_json"] is None
