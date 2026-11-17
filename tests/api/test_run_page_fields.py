"""Work order 4.2: GET /runs/{id} carries every field the Run page (and the Ask receipt) reads.

A real run goes through the pipeline on the stand-in models (tests/e2e/stub_model_server.py)
with a temp database, then is served by the app itself. The lists below are what
ui/run.html, ui/ask.html and ui/assets/workspace.js read; if one of them is renamed or
dropped on the server, this test fails before the page silently shows "-".
"""
import json
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

from src.api import tasks as tasks_api
from tests.e2e.test_cost_recording import _run

# ---- the fields the pages read -------------------------------------------------------
RUN_FIELDS = ["run_id", "status", "created_at", "completed_at", "user_instruction",
              "failure_reason", "parent_run_id", "documents"]
ENVELOPE_FIELDS = ["envelope_id", "run_id", "type", "trigger_reason", "status", "model_id",
                   "outbound_payload", "inbound_payload", "tokens_input", "tokens_output",
                   "dollars", "latency_ms", "skill_id", "routing"]
OUTBOUND_PAYLOAD_FIELDS = ["instruction", "data_scope", "dependency_input"]
INBOUND_PAYLOAD_FIELDS = ["status", "failure_reason", "output"]
ROUTING_FIELDS = ["node_id", "chosen", "reason", "task_type", "tier", "floor", "candidates", "notes"]
CANDIDATE_FIELDS = ["model_id", "score", "est_dollars"]
EVENT_FIELDS = ["event_type", "envelope_id", "model_id", "tokens_input", "tokens_output",
                "dollars", "latency_ms", "created_at", "decision_json"]
PLAN_DECISION_FIELDS = ["mode", "parsed"]          # parsed.plan.nodes[].id / depends_on
ROUTE_DECISION_FIELDS = ["mode", "node_id", "chosen", "reason"]
DIGEST_FIELDS = ["cost_dollars"]
TOP_LEVEL = ["run", "envelopes", "child_envelopes", "events", "digest", "outbound"]
CHILD_ENVELOPE_FIELDS = ["run_id", "model_id", "type", "status"]
OUTBOUND_FIELDS = ["envelope_id", "purpose", "destination", "url", "status", "http_status", "body"]
RUNS_LIST_FIELDS = ["run_id", "created_at", "user_instruction", "status", "user_rating", "kind"]
RUNS_COUNT_FIELDS = ["total", "completed", "failed", "thumbs_up", "thumbs_down"]

PLAN = [
    {"type": "answerer", "id": "n1", "instruction": "Extract product and sentiment.",
     "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}},
    {"type": "answerer", "id": "n2", "instruction": "Write a tally function.",
     "task_type": "coding", "tier": "mid", "output_schema": {"result": "string"}},
    {"type": "answerer", "id": "n3", "instruction": "Recommend which product to stock.",
     "task_type": "reasoning", "tier": "heavy", "depends_on": ["n1", "n2"],
     "output_schema": {"result": "string"}},
]


def _client(monkeypatch, db):
    from main import app

    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    return TestClient(app, raise_server_exceptions=False, client=("127.0.0.1", 50000))


def _has(obj, fields, where):
    missing = [f for f in fields if f not in obj]
    assert not missing, f"{where} lacks {missing}"


@pytest.mark.asyncio
async def test_run_detail_carries_every_field_the_run_page_reads(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch, plan=PLAN)
    body = _client(monkeypatch, db).get(f"/runs/{result['run_id']}").json()

    _has(body, TOP_LEVEL, "response")
    _has(body["run"], RUN_FIELDS, "run")
    assert body["run"]["status"] == "completed" and body["run"]["completed_at"]

    assert len(body["envelopes"]) == 3
    for env in body["envelopes"]:
        _has(env, ENVELOPE_FIELDS, "envelope")
        _has(json.loads(env["outbound_payload"]), OUTBOUND_PAYLOAD_FIELDS, "outbound_payload")
        _has(json.loads(env["inbound_payload"]), INBOUND_PAYLOAD_FIELDS, "inbound_payload")
        _has(env["routing"], ROUTING_FIELDS, "routing")
        assert env["routing"]["candidates"]
        for c in env["routing"]["candidates"]:
            _has(c, CANDIDATE_FIELDS, "candidate")
        assert env["dollars"] > 0 and env["latency_ms"] is not None
    assert sorted(e["routing"]["node_id"] for e in body["envelopes"]) == ["n1", "n2", "n3"]

    for ev in body["events"]:
        _has(ev, EVENT_FIELDS, "event")
    decisions = [json.loads(e["decision_json"]) for e in body["events"]
                 if e["event_type"] == "orchestrator_decision" and e["decision_json"]]
    plans = [d for d in decisions if d.get("mode") == "plan"]
    assert plans, "no plan event: the diagram has no depends_on to draw"
    _has(plans[0], PLAN_DECISION_FIELDS, "plan decision")
    nodes = {n["id"]: n for n in plans[0]["parsed"]["plan"]["nodes"]}
    assert nodes["n3"]["depends_on"] == ["n1", "n2"]
    routes = [d for d in decisions if d.get("mode") == "route"]
    assert len(routes) == 3
    for d in routes:
        _has(d, ROUTE_DECISION_FIELDS, "route decision")
    assert any(d.get("mode") == "compose" for d in decisions)

    _has(body["digest"], DIGEST_FIELDS, "digest")
    assert body["digest"]["cost_dollars"] > 0
    assert isinstance(body["child_envelopes"], list) and isinstance(body["outbound"], list)


@pytest.mark.asyncio
async def test_a_failed_run_carries_its_failure_reason(tmp_path, monkeypatch):
    from src.db.schema import create_all
    from src.db.state import create_run, update_run_status

    db = str(tmp_path / "failed.db")
    await create_all(db)
    run_id = await create_run("do a thing", db_path=db)
    await update_run_status(run_id, "failed", failure_reason="BRT-PIP-008: run exceeded wall-clock budget (5s).", db_path=db)
    body = _client(monkeypatch, db).get(f"/runs/{run_id}").json()
    assert body["run"]["status"] == "failed"
    assert body["run"]["failure_reason"].startswith("BRT-PIP-008")


@pytest.mark.asyncio
async def test_what_left_rows_carry_their_fields_and_bodies_only_for_the_admin(tmp_path, monkeypatch):
    _stub, result, db = await _run(tmp_path, monkeypatch, plan=PLAN)
    run_id = result["run_id"]
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO outbound_requests(request_id, run_id, envelope_id, attempt, purpose, destination, url,"
            " left_machine, body, status, http_status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), run_id, None, 1, "orchestrator:plan", "openrouter/stub/top",
             "https://openrouter.ai/api/v1/chat/completions", 1, '{"messages": []}', "sent", 200),
        )
        conn.commit()
    monkeypatch.delenv("BORG_ADMIN_TOKEN", raising=False)
    admin = _client(monkeypatch, db).get(f"/runs/{run_id}").json()
    assert len(admin["outbound"]) == 1
    _has(admin["outbound"][0], OUTBOUND_FIELDS, "outbound row")
    assert admin["outbound"][0]["body"] == '{"messages": []}'

    monkeypatch.setenv("BORG_ADMIN_TOKEN", "a-token-for-this-test")
    other = _client(monkeypatch, db).get(f"/runs/{run_id}").json()
    _has(other["outbound"][0], OUTBOUND_FIELDS, "outbound row")
    assert other["outbound"][0]["body"] is None


@pytest.mark.asyncio
async def test_runs_list_and_page_inputs_carry_their_fields(tmp_path, monkeypatch):
    from src.db.state import create_run

    _stub, result, db = await _run(tmp_path, monkeypatch, plan=PLAN)
    bypass = await create_run("direct question", db_path=db, kind="bypass")
    client = _client(monkeypatch, db)
    listing = client.get("/runs?limit=10").json()
    ids = [r["run_id"] for r in listing["runs"]]
    assert result["run_id"] in ids
    assert str(bypass) not in ids, "the Runs page leaves direct-to-model runs out by default"
    for r in listing["runs"]:
        _has(r, RUNS_LIST_FIELDS, "runs row")
    _has(listing["counts"], RUNS_COUNT_FIELDS, "counts")
    with_bypass = client.get("/runs?limit=10&include_bypass=true").json()
    assert str(bypass) in [r["run_id"] for r in with_bypass["runs"]]
