"""Ruling 32c: the BRT-ORC-035 "ran without fields" plan note survives redaction
for a non-admin while the lane is on, in a whitelisted shape; nothing else of
its decision text does."""
import json

from src.api.tasks import _redact_events


def _note(**over):
    d = {"code": "BRT-ORC-035", "action": "ran_without_fields", "node_id": "n1",
         "skill_id": "classify_into_buckets", "fields": ["file", "urgency"],
         "msg": "BRT-ORC-035: 'classify_into_buckets' returns: bucket", "extra": "doc text"}
    d.update(over)
    return {"event_type": "orchestrator_decision", "decision_json": json.dumps(d)}


def test_the_note_keeps_exactly_five_keys_and_drops_the_rest():
    kept = json.loads(_redact_events([_note()])[0]["decision_json"])
    assert kept == {"code": "BRT-ORC-035", "action": "ran_without_fields", "node_id": "n1",
                    "skill_id": "classify_into_buckets", "fields": ["file", "urgency"]}


def test_strings_are_cut_at_40_and_at_most_10_fields_are_kept():
    long = "x" * 100
    kept = json.loads(_redact_events([_note(
        node_id=long, skill_id=long, fields=[long] + [f"f{i}" for i in range(20)])])[0]["decision_json"])
    assert kept["node_id"] == "x" * 40 and kept["skill_id"] == "x" * 40
    assert len(kept["fields"]) == 10 and kept["fields"][0] == "x" * 40


def test_another_decision_with_the_same_code_but_no_action_is_still_dropped():
    ev = {"event_type": "orchestrator_decision",
          "decision_json": json.dumps({"code": "BRT-ORC-035", "msg": "text"})}
    assert _redact_events([ev])[0]["decision_json"] is None


def test_the_planning_stopped_warning_keeps_code_severity_and_action_only():
    """Reading (2) under ruling 35: the receipt line survives redaction; the
    message and the error text do not."""
    d = {"code": "BRT-ORC-002", "severity": "warning", "action": "planning_stopped",
         "msg": "planning stopped early", "err": "BRT-ORC-002: ... doc text"}
    ev = {"event_type": "error", "decision_json": json.dumps(d)}
    kept = json.loads(_redact_events([ev])[0]["decision_json"])
    assert kept == {"code": "BRT-ORC-002", "severity": "warning", "action": "planning_stopped"}


def test_a_planning_stopped_warning_from_a_failed_call_is_kept_the_same_way():
    """The lead's ruling: a re_plan that ends in BRT-ORC-001 stops planning too."""
    d = {"code": "BRT-ORC-001", "severity": "warning", "action": "planning_stopped",
         "err": "BRT-ORC-001: HTTP 500 from orchestrator"}
    kept = json.loads(_redact_events([{"event_type": "error", "decision_json": json.dumps(d)}])[0]["decision_json"])
    assert kept == {"code": "BRT-ORC-001", "severity": "warning", "action": "planning_stopped"}


def test_the_plain_rows_warning_keeps_its_receipt_line_only():
    """Ruling 39c: the BRT-ORC-037 warning of an answer kept with plain rows
    keeps code, severity, action and the fixed receipt line; the step ids and
    message do not leave."""
    d = {"code": "BRT-ORC-037", "severity": "warning", "action": "rows_placed_plain",
         "msg": "text", "steps": ["n1"], "receipt": "anything"}
    kept = json.loads(_redact_events([{"event_type": "error", "decision_json": json.dumps(d)}])[0]["decision_json"])
    assert kept == {"code": "BRT-ORC-037", "severity": "warning", "action": "rows_placed_plain",
                    "receipt": "the write-up asked for columns that do not exist; all columns are shown"}


def test_a_037_warning_without_the_plain_rows_action_is_dropped():
    d = {"code": "BRT-ORC-037", "severity": "warning", "msg": "text"}
    assert _redact_events([{"event_type": "error", "decision_json": json.dumps(d)}])[0]["decision_json"] is None
