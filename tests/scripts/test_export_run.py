"""Work order 4.5: scripts/export_run.py writes one replay file per finished run.

Built on small synthetic databases (tmp_path), so no real run database is needed. The
committed demo exports are checked in test_demo_exports.py.
"""
import asyncio
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUN = "11111111-2222-3333-4444-555555555555"
LANE_RUN = "66666666-7777-8888-9999-000000000000"
NOTE = "Call with the harbour office went fine. They hold the current rates until June."
CANARY = "CANARY-1a2b3c-TEST-LANTERN"


def _load():
    spec = importlib.util.spec_from_file_location("export_run", ROOT / "scripts" / "export_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _Pricing:
    input_per_token = 4e-06
    output_per_token = 2e-05


class _Row:
    def __init__(self, model_id, local):
        self.model_id = model_id
        self.pricing = _Pricing()
        self.local = local


class FakeMatrix:
    """Only what the export reads: pricing for the floor check, locality for the rows."""

    def __init__(self):
        self.rows = {"cloud/planner": _Row("cloud/planner", False),
                     "cloud/baseline": _Row("cloud/baseline", False),
                     "ollama/small": _Row("ollama/small", True)}

    def get(self, model_id):
        return self.rows.get(model_id)

    def all(self):
        return list(self.rows.values())


def _floor(instruction, doc_chars, answer):
    return ((len(instruction) + doc_chars) / 4) * 4e-06 + (len(answer) / 4) * 2e-05


def _make_db(path, *, run_id=RUN, lane_mode="off", answer="The answer is 42.", compose_text=None,
             error_msg="step failed", documents=None, outbound_body=None, status="completed",
             floor_answer=None, no_floor=False):
    from src.db.schema import create_all

    asyncio.run(create_all(str(path)))
    con = sqlite3.connect(str(path))
    instruction = "Add the numbers in the list and say the total."
    doc_chars = sum(d["chars"] for d in documents or [])
    con.execute(
        "INSERT INTO runs(run_id, status, user_instruction, created_at, completed_at, kind, "
        "documents, answer_kind, lane_mode, instance_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (run_id, status, instruction, "2026-11-18 10:00:00",
         "2026-11-18 10:00:30" if status == "completed" else None, "orchestrated",
         json.dumps(documents) if documents else None, "composed", lane_mode,
         "borg-route:abcdef0123456789"))
    env_id = "e" + run_id[1:]
    con.execute(
        "INSERT INTO envelopes(envelope_id, run_id, subtask_id, type, level_path, model_id, "
        "trigger_reason, status, outbound_payload, inbound_payload, tokens_input, tokens_output, "
        "dollars, latency_ms, created_at, completed_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (env_id, run_id, "s1", "answerer", "0", "ollama/small", "initial_plan", "resolved",
         json.dumps({"node_id": "n1", "instruction": "add them"}),
         json.dumps({"status": "resolved", "output": {"value": "42"}}),
         100, 20, 0.0, 4000, "2026-11-18 10:00:05", "2026-11-18 10:00:09"))
    events = [
        ("2026-11-18 10:00:02", "orchestrator_decision", None, "cloud/planner", 0.001,
         {"mode": "plan", "parsed": {"plan": {"nodes": [{"id": "n1", "type": "answerer",
                                                          "instruction": "add them"}]}},
          "msg": "read C:\\Users\\deej\\secret\\notes.txt with sk-or-v1-0123456789abcdef"}),
        ("2026-11-18 10:00:05", "orchestrator_decision", env_id, "ollama/small", None,
         {"mode": "route", "node_id": "n1", "chosen": "ollama/small", "reason": "routed",
          "candidates": [{"model_id": "ollama/small", "score": 7, "est_cost": 0.0}]}),
        ("2026-11-18 10:00:09", "model_call", env_id, "ollama/small", 0.0, None),
        ("2026-11-18 10:00:12", "error", None, None, None,
         {"code": "BRT-ORC-019", "msg": error_msg, "path": "/home/deej/borg/data/x.db"}),
        ("2026-11-18 10:00:28", "orchestrator_decision", None, "cloud/planner", 0.002,
         {"mode": "compose", "parsed": {"response": compose_text if compose_text is not None else answer}}),
    ]
    for at, etype, env, model, dollars, decision in events:
        con.execute(
            "INSERT INTO logs(run_id, envelope_id, event_type, model_id, tokens_input, tokens_output, "
            "dollars, latency_ms, decision_json, created_at, instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, env, etype, model, 10 if dollars is not None else None,
             5 if dollars is not None else None, dollars, 50, json.dumps(decision) if decision else None,
             at, "borg-route:abcdef0123456789"))
    if outbound_body is not None:
        con.execute(
            "INSERT INTO outbound_requests(request_id, run_id, envelope_id, attempt, purpose, destination, "
            "url, left_machine, body, status, started_at, finished_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("r1", run_id, None, 1, "orchestrator:plan", "cloud/planner",
             "https://openrouter.ai/api/v1/chat/completions", 1, outbound_body, "sent", "2026-11-18 10:00:05", "2026-11-18 10:00:09"))
    floor = None if no_floor else _floor(instruction, doc_chars,
                                         floor_answer if floor_answer is not None else answer)
    con.execute(
        "INSERT INTO run_digests(run_id, summary, cost_dollars, token_count, tokens_input, tokens_output, "
        "direct_floor_dollars, direct_floor_model, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (run_id, "1 envelope(s)", 0.003, 45, 30, 15, floor, "cloud/baseline", "2026-11-18 10:00:30"))
    con.commit()
    con.close()
    return instruction


def _docs(tmp_path, body=NOTE):
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    text = "Topic: Rates\nOwner: Pat Lee\n" + body + "\n"
    (root / "notes" / "note_01.txt").write_text(text, encoding="utf-8")
    return root, [{"id": "doc_001", "type": "txt", "size": len(text), "parts": 1, "chars": len(text),
                   "name": "notes/note_01.txt"}]


def _export(mod, db, run_id=RUN, **kw):
    return asyncio.run(mod.build_export(run_id, db_path=str(db), matrix=FakeMatrix(), **kw))


# --- the working case -------------------------------------------------------------------

def test_export_holds_run_envelopes_relative_events_routing_costs_and_answer(tmp_path):
    mod = _load()
    db = tmp_path / "run.db"
    instruction = _make_db(db)
    doc = _export(mod, db)
    assert doc["format"] == mod.FORMAT and doc["label"] == "demo run"
    assert doc["prompt"] == instruction and doc["recorded_on"] == "2026-11-18"
    view = doc["view"]
    assert view["run"]["status"] == "completed"
    env = view["envelopes"][0]
    assert env["routing"]["chosen"] == "ollama/small"           # the routing decision
    assert env["t_ms"] == 5000 and env["t_done_ms"] == 9000
    assert [ev["t_ms"] for ev in view["events"]] == [2000, 5000, 9000, 12000, 28000]
    assert view["digest"]["cost_dollars"] == pytest.approx(0.003)
    assert doc["timeline"]["end_ms"] == 30000
    assert [s["stage"] for s in doc["timeline"]["stages"]] == ["plan", "route", "compose"]
    assert doc["answer"] == "The answer is 42." and doc["answer_source"] == "rebuilt"
    assert doc["answer_check"] == "matches the stored floor"
    assert {"model_id": "ollama/small", "is_local": True} in doc["models"]


def test_export_strips_keys_absolute_paths_and_the_machine_id(tmp_path):
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db)
    text = json.dumps(_export(mod, db))
    assert "sk-or-v1" not in text and "deej" not in text and "/home/" not in text
    assert "C:\\\\Users" not in text and "C:\\Users" not in text
    assert "[key]" in text and "[local path]" in text
    assert "abcdef0123456789" not in text                       # instance_id is dropped


def test_a_lane_run_gets_the_non_admin_lane_redaction(tmp_path):
    """Lane on: events as `_redact_events` leaves them, request bodies never exported."""
    mod = _load()
    root, docs = _docs(tmp_path)
    db = tmp_path / "lane.db"
    _make_db(db, run_id=LANE_RUN, lane_mode="raw_local", documents=docs,
             error_msg="the note says " + NOTE, outbound_body=json.dumps({"messages": [NOTE]}))
    doc = _export(mod, db, run_id=LANE_RUN, docs_root=str(root))
    assert doc["redaction"] == "lane_viewer"
    view = doc["view"]
    from src.api.tasks import _redact_events

    stripped = [{k: v for k, v in ev.items() if k != "t_ms"} for ev in view["events"]]
    assert _redact_events(stripped) == stripped                  # nothing left to redact
    plan = [ev for ev in view["events"] if ev["event_type"] == "orchestrator_decision"][0]
    assert plan["decision_json"] is None                          # a plan's text is dropped
    assert all(o["body"] is None for o in view["outbound"]) and view["outbound"]
    assert NOTE not in json.dumps(doc)


def test_a_stored_answer_file_is_used_before_a_rebuild(tmp_path):
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db, answer="Total: 42.", compose_text="@@ROWS:n9@@")
    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / f"{RUN}.md").write_text("Total: 42.", encoding="utf-8")
    doc = _export(mod, db)
    assert doc["answer"] == "Total: 42." and doc["answer_source"] == "stored"


def test_main_writes_one_file_and_says_what_was_stripped(tmp_path, capsys, monkeypatch):
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db)
    monkeypatch.setattr(mod, "load_matrix", lambda: FakeMatrix())
    out = tmp_path / "out"
    assert mod.main([RUN, "--db", str(db), "--out", str(out)]) == 0
    files = list(out.glob("*.json"))
    assert len(files) == 1 and files[0].name == f"run_{RUN[:8]}.json"
    printed = capsys.readouterr().out
    assert "stripped" in printed and "demo run" in printed


# --- the failing cases --------------------------------------------------------------------

def _refused(mod, tmp_path, capsys, monkeypatch, db, run_id, code, extra=()):
    monkeypatch.setattr(mod, "load_matrix", lambda: FakeMatrix())
    out = tmp_path / "out"
    assert mod.main([run_id, "--db", str(db), "--out", str(out), *extra]) == 2
    assert code in capsys.readouterr().out
    assert not out.exists() or not list(out.glob("*.json"))


def test_export_refuses_a_canary(tmp_path, capsys, monkeypatch):
    mod = _load()
    root, docs = _docs(tmp_path, body=NOTE + " Ref: " + CANARY)
    db = tmp_path / "lane.db"
    answer = "Notes read. Reference " + CANARY
    _make_db(db, run_id=LANE_RUN, lane_mode="raw_local", documents=docs, answer=answer)
    _refused(mod, tmp_path, capsys, monkeypatch, db, LANE_RUN, "BRT-RPL-010",
             ["--docs-root", str(root)])


def test_export_refuses_raw_document_text(tmp_path, capsys, monkeypatch):
    mod = _load()
    root, docs = _docs(tmp_path)
    db = tmp_path / "lane.db"
    _make_db(db, run_id=LANE_RUN, lane_mode="raw_local", documents=docs, answer="Summary: " + NOTE)
    _refused(mod, tmp_path, capsys, monkeypatch, db, LANE_RUN, "BRT-RPL-011",
             ["--docs-root", str(root)])


def test_export_refuses_when_the_documents_cannot_be_read(tmp_path, capsys, monkeypatch):
    mod = _load()
    _, docs = _docs(tmp_path)
    db = tmp_path / "lane.db"
    _make_db(db, run_id=LANE_RUN, lane_mode="raw_local", documents=docs)
    _refused(mod, tmp_path, capsys, monkeypatch, db, LANE_RUN, "BRT-RPL-011",
             ["--docs-root", str(tmp_path / "missing")])


def test_export_refuses_a_run_that_is_not_finished_or_not_there(tmp_path, capsys, monkeypatch):
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db, status="running")
    _refused(mod, tmp_path, capsys, monkeypatch, db, RUN, "BRT-RPL-014")
    _refused(mod, tmp_path, capsys, monkeypatch, db, "99999999-2222-3333-4444-555555555555", "BRT-RPL-014")


def test_export_refuses_an_answer_that_does_not_match_the_stored_floor(tmp_path, capsys, monkeypatch):
    """The answer is not stored in the run database; a rebuilt one must have the length the
    run's floor was priced on, or nothing is written."""
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db, answer="The answer is 42.", floor_answer="A longer answer than the one rebuilt here.")
    _refused(mod, tmp_path, capsys, monkeypatch, db, RUN, "BRT-RPL-013")


def test_export_refuses_a_stored_answer_of_the_wrong_length(tmp_path, capsys, monkeypatch):
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db, answer="The answer is 42.")
    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / f"{RUN}.md").write_text("The answer is 42, said at length.", encoding="utf-8")
    _refused(mod, tmp_path, capsys, monkeypatch, db, RUN, "BRT-RPL-013")


def test_export_refuses_a_run_with_no_priced_floor(tmp_path, capsys, monkeypatch):
    """The length check always applies: no floor, no export (stored answer or not)."""
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db, no_floor=True)
    (tmp_path / "answers").mkdir()
    (tmp_path / "answers" / f"{RUN}.md").write_text("The answer is 42.", encoding="utf-8")
    _refused(mod, tmp_path, capsys, monkeypatch, db, RUN, "BRT-RPL-013")


def test_the_export_carries_no_benchmark_ratio(tmp_path):
    mod = _load()
    db = tmp_path / "run.db"
    _make_db(db)
    doc = _export(mod, db)
    assert doc["view"]["digest"]["direct_floor_ratio"] is None


# --- the strong check (lead ruling: every 24-character window; canary bodies) -------------

NOTE_01 = (ROOT / "demo" / "data" / "job3_notes" / "note_01.txt").read_text(encoding="utf-8")
QUOTE = NOTE_01.splitlines()[3][:66]          # 66 characters of note_01's body line
BODY = "7f3a91-ZEBRA-LANTERN"
REAL_CANARY = "CANARY-" + BODY


def _real_docs():
    folder = ROOT / "demo" / "data" / "job3_notes"
    return {f"job3_notes/{p.name}": p.read_text(encoding="utf-8") for p in sorted(folder.glob("*.txt"))}


@pytest.mark.parametrize("answer,code", [
    (f"Summary: the note says {QUOTE} and more.", "BRT-RPL-011"),       # a quote inside an answer
    (f"SUMMARY: {QUOTE.upper()}", "BRT-RPL-011"),                       # the same, upper case
    ("Summary: " + QUOTE.replace(" ", "\n  "), "BRT-RPL-011"),          # whitespace changed
    (f"ref {BODY}", "BRT-RPL-010"),                                      # the bare canary body
    (f"ref {BODY.lower()}", "BRT-RPL-010"),
    (f"ref {REAL_CANARY}", "BRT-RPL-010"),
])
def test_the_strong_check_catches_quotes_case_and_bare_canaries(answer, code):
    mod = _load()
    assert len(QUOTE) == 66
    codes = {c for c, _ in mod.problems({"answer": answer}, _real_docs(), [])}
    assert code in codes


def test_a_short_derived_field_passes_the_strong_check():
    mod = _load()
    assert mod.problems({"answer": "Dock repairs: Mara Quill, yes (decision by Friday)"},
                        _real_docs(), []) == []


@pytest.mark.parametrize("answer,code", [
    ("Summary: " + QUOTE.upper(), "BRT-RPL-011"),
    ("Reference " + BODY, "BRT-RPL-010"),
])
def test_the_export_step_refuses_to_write_a_file_that_fails_the_strong_check(
        tmp_path, capsys, monkeypatch, answer, code):
    """The check runs inside the export step: a failing export is never written."""
    mod = _load()
    root = tmp_path / "docs"
    (root / "notes").mkdir(parents=True)
    text = NOTE_01 + "Internal reference: " + REAL_CANARY + "\n"
    (root / "notes" / "note_01.txt").write_text(text, encoding="utf-8")
    docs = [{"id": "doc_001", "type": "txt", "size": len(text), "parts": 1, "chars": len(text),
             "name": "notes/note_01.txt"}]
    db = tmp_path / "lane.db"
    _make_db(db, run_id=LANE_RUN, lane_mode="raw_local", documents=docs, answer=answer)
    _refused(mod, tmp_path, capsys, monkeypatch, db, LANE_RUN, code, ["--docs-root", str(root)])
