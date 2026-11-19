"""Work order 4.5: replay mode (BORG_DEMO_MODE=replay) plays the committed demo exports.

Done when: replay makes zero outbound HTTP calls (httpx patched to raise), every
state-changing route returns 403, and a replayed run renders the same receipt totals as the
source run. Runs at a high `replay_speed`, so no test waits for the recorded times.
"""
import json
import socket
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from main import app
from src.api import replay

ROOT = Path(__file__).resolve().parents[2]
RUNS = ROOT / "demo" / "runs"
EXPORTS = sorted(RUNS.glob("job*_*.json"))
FAST = 100000.0
MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


def _exports():
    return [json.loads(p.read_text(encoding="utf-8")) for p in EXPORTS]


@pytest.fixture
def outbound(monkeypatch):
    """Every way out raises and is counted: httpx (sync and async transports, the clients'
    send) and new sockets. The pipeline and the model call path raise too."""
    calls = []

    def boom(*a, **k):
        calls.append(a[:1])
        raise AssertionError("an outbound call was made in replay mode")

    async def aboom(*a, **k):
        boom(*a, **k)

    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", boom)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", aboom)
    monkeypatch.setattr(httpx.AsyncClient, "send", aboom)
    monkeypatch.setattr(socket, "create_connection", boom)
    monkeypatch.setattr("src.pipeline.runner.run_pipeline", aboom)
    monkeypatch.setattr("src.api.chat_completions.run_pipeline", aboom)
    monkeypatch.setattr("src.agents.executor.send_outbound", aboom)
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    return calls


@pytest.fixture
def client(monkeypatch, outbound):
    monkeypatch.setenv("BORG_DEMO_MODE", "replay")
    monkeypatch.setenv("BORG_REPLAY_DIR", str(RUNS))
    replay.reset()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c
    replay.reset()


def _play(c, export, speed=FAST):
    """POST the recorded prompt the way the Ask page does; returns (stages, answer, run id)."""
    body = {"model": "borg_route", "stream": True, "replay_speed": speed,
            "messages": [{"role": "user", "content": export["prompt"]}]}
    stages, text, run_id = [], "", None
    with c.stream("POST", "/v1/chat/completions", json=body) as r:
        assert r.status_code == 200
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            chunk = json.loads(payload)
            if chunk.get("event") == "stage":
                stages.append(chunk["stage"])
                continue
            run_id = run_id or chunk["id"][len("chatcmpl-"):]
            text += chunk["choices"][0]["delta"].get("content") or ""
    return stages, text, run_id


def test_the_committed_exports_are_the_ruling_24_runs():
    ids = {e["run_id"][:8] for e in _exports()}
    assert {"d04e0d8a", "e535b654", "e31a5330"} <= ids
    assert not ids & {"230b0186", "19f56430", "ece3d973"}


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_a_replay_plays_stages_then_the_answer_with_no_outbound_call(client, outbound, path):
    export = json.loads(path.read_text(encoding="utf-8"))
    stages, text, run_id = _play(client, export)
    assert stages == [s["stage"] for s in export["timeline"]["stages"]]
    assert text == export["answer"]
    view = client.get(f"/runs/{run_id}").json()
    assert view["run"]["status"] == export["view"]["run"]["status"]
    assert view["run"]["run_id"] == run_id
    assert outbound == []


def test_the_run_stream_plays_envelope_updates_then_the_end(client, outbound):
    export = _exports()[0]
    _, _, run_id = _play(client, export)
    events = []
    with client.stream("GET", f"/runs/{run_id}/stream") as r:
        for line in r.iter_lines():
            if line.startswith("data:"):
                events.append(json.loads(line[5:]))
    kinds = [e["type"] for e in events]
    assert kinds[-1] == "run_terminal" and events[-1]["status"] == export["view"]["run"]["status"]
    assert kinds.count("envelope_update") >= len(export["view"]["envelopes"])
    assert outbound == []


def test_mid_play_the_run_is_running_and_shows_only_what_had_happened(client, monkeypatch):
    export = next(e for e in _exports() if e["run_id"].startswith("d04e0d8a"))
    clock = {"now": 1000.0}
    monkeypatch.setattr(replay, "_now", lambda: clock["now"])
    session = replay.start_session(export["run_id"], speed=1.0)
    clock["now"] += 0.5
    early = client.get(f"/runs/{session}").json()
    assert early["run"]["status"] == "running" and early["digest"] is None
    assert len(early["events"]) < len(export["view"]["events"])
    clock["now"] += export["timeline"]["end_ms"] / 1000 + 1
    done = client.get(f"/runs/{session}").json()
    assert done["run"]["status"] == "completed"
    assert len(done["events"]) == len(export["view"]["events"])
    assert done["digest"] == export["view"]["digest"]


def test_every_state_changing_route_returns_403(client, outbound):
    tried = set()
    for route in app.routes:
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods & MUTATING):
            path = route.path.replace("{", "").replace("}", "")
            r = client.request(method, path, json={})
            assert r.status_code == 403, (method, route.path, r.status_code)
            code = r.json()["detail"]["error"]
            assert code in ("BRT-RPL-001", "BRT-RPL-002"), (method, route.path)
            tried.add((method, route.path))
    for must in (("POST", "/v1/task"), ("POST", "/v1/chat/completions"), ("POST", "/keys"),
                 ("POST", "/config"), ("POST", "/runs/{run_id}/resume"), ("POST", "/runs/{run_id}/rating"),
                 ("POST", "/models/discover"), ("PATCH", "/models/{model_id:path}/checked")):
        assert must in tried, must
    assert outbound == []


def test_a_prompt_that_was_not_recorded_is_refused(client, outbound):
    r = client.post("/v1/chat/completions", json={
        "model": "borg_route", "stream": True,
        "messages": [{"role": "user", "content": "Write me a poem about routers."}]})
    assert r.status_code == 403 and r.json()["detail"]["error"] == "BRT-RPL-002"
    assert outbound == []


def test_routes_that_could_call_out_are_closed_in_replay(client, outbound):
    for path in ("/status/local", "/keys/status", "/config/effective", "/logs/cost-summary"):
        r = client.get(path)
        assert r.status_code == 403 and r.json()["detail"]["error"] == "BRT-RPL-001", path
        assert "content-security-policy" in r.headers        # the CSP still wraps a refusal
    assert client.get("/health").status_code == 200
    assert client.get("/ui/ask.html").status_code == 200
    assert outbound == []


def test_status_lists_the_recorded_prompts_and_the_notice_date(client):
    st = client.get("/replay/status").json()
    assert st["mode"] == "replay"
    assert st["notice"] == "Replay of a recorded run on 18-Nov-2026"
    assert {r["run_id"] for r in st["runs"]} == {e["run_id"] for e in _exports()}
    assert all(r["label"] == "demo run" and r["prompt"] for r in st["runs"])


def test_the_runs_list_and_the_matrix_come_from_the_exports(client):
    runs = client.get("/runs").json()
    assert {r["run_id"] for r in runs["runs"]} == {e["run_id"] for e in _exports()}
    assert runs["counts"]["total"] == len(EXPORTS)
    rows = client.get("/models/matrix").json()
    assert {"model_id": "ollama/llama3.2:3b", "is_local": True} in rows


def test_a_recorded_run_opens_finished_by_its_own_id(client):
    export = _exports()[0]
    view = client.get(f"/runs/{export['run_id']}").json()
    assert view["digest"] == export["view"]["digest"]
    assert client.get("/runs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_an_unknown_mode_value_fails_closed_to_replay(monkeypatch, outbound):
    monkeypatch.setenv("BORG_DEMO_MODE", "replya")
    assert replay.demo_mode() == "replay"
    monkeypatch.setenv("BORG_DEMO_MODE", "off")
    assert replay.demo_mode() is None
    monkeypatch.delenv("BORG_DEMO_MODE")
    assert replay.demo_mode() is None


def test_live_mode_is_untouched(monkeypatch):
    monkeypatch.delenv("BORG_DEMO_MODE", raising=False)
    c = TestClient(app)
    assert c.get("/replay/status").json() == {"mode": "live"}
    r = c.post("/v1/task", json={"instruction": ""})
    assert r.status_code == 400 and r.json()["detail"]["error"] == "BRT-API-020"


# --- the same receipt totals as the source run ------------------------------------------

node = pytest.importorskip("tests.ui.test_app_js")


@pytest.mark.skipif(node.NODE is None, reason="node is not on PATH")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_a_replayed_run_renders_the_same_receipt_totals_as_the_source(client, tmp_path, path):
    export = json.loads(path.read_text(encoding="utf-8"))
    _, _, run_id = _play(client, export)
    replayed = client.get(f"/runs/{run_id}").json()
    script = (
        "const {BorgWork: W} = H.load({extra: ['workspace.js']});"
        f"const a = {json.dumps(export['view'])}; const b = {json.dumps(replayed)};"
        "const pick = (d) => { const r = W.receiptFrom(d); return {r: r, tiles: W.headerTiles(d)}; };"
        "return JSON.stringify([pick(a), pick(b)]).replace(/[\\u007f-\\uffff]/g, c => '');"
    )
    source, played = json.loads(node.run_js(tmp_path, script))
    assert played == source
    assert source["r"]["cost"] == export["view"]["digest"]["cost_dollars"]
