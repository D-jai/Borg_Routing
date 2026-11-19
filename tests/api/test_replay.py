"""Work order 4.5: replay mode (BORG_DEMO_MODE=replay) plays the committed demo exports.

Done when: replay makes zero outbound HTTP calls (httpx patched to raise), every
state-changing route returns 403, and a replayed run renders the same receipt totals as the
source run. Runs at a high `replay_speed`, so no test waits for the recorded times.
"""
import asyncio
import asyncio.proactor_events
import asyncio.selector_events
import json
import os
import shutil
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
    # The event loop's own ways out (lead ruling, review item 5), on every loop class that
    # defines them (Windows' proactor loop and the selector loop).
    for cls in (asyncio.BaseEventLoop, asyncio.proactor_events.BaseProactorEventLoop,
                asyncio.selector_events.BaseSelectorEventLoop):
        for name in ("create_connection", "sock_connect"):
            if name in vars(cls):
                monkeypatch.setattr(cls, name, aboom)
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
    assert {"c882e3c9", "6c83c9c7", "e31a5330"} <= ids
    assert not ids & {"230b0186", "19f56430", "ece3d973"}


@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_a_replay_plays_stages_then_the_answer_with_no_outbound_call(client, outbound, path):
    export = json.loads(path.read_text(encoding="utf-8"))
    stages, text, run_id = _play(client, export)
    recorded = [s["stage"] for s in export["timeline"]["stages"]]
    lead = ["starting"] if (export["timeline"]["stages"][0].get("t_ms") or 0) >= 1 else []
    assert stages == lead + recorded       # the labelled wait before the first stage, then as recorded
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
    export = next(e for e in _exports() if e["run_id"].startswith("c882e3c9"))
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
    assert done["digest"] == dict(export["view"]["digest"], direct_floor_ratio=None)


def _all_routes(routes, prefix=""):
    """Every route the app serves, on any FastAPI version. Since FastAPI 0.16x,
    include_router keeps a wrapper (_IncludedRouter, holding original_router and an
    include_context with the prefix) in app.routes instead of copying the routes in."""
    for route in routes:
        inner = getattr(route, "original_router", None)
        if inner is not None:
            ctx = getattr(route, "include_context", None)
            yield from _all_routes(inner.routes, prefix + (getattr(ctx, "prefix", "") or ""))
        else:
            yield prefix, route


def test_the_route_walk_finds_the_included_routes():
    """Guard for the walk itself: it must reach the routers main.py includes."""
    found = {(p + r.path) for p, r in _all_routes(app.routes) if getattr(r, "methods", None)}
    assert {"/v1/task", "/keys", "/models/discover"} <= found


def test_every_state_changing_route_returns_403(client, outbound):
    tried = set()
    for prefix, route in _all_routes(app.routes):
        methods = getattr(route, "methods", None) or set()
        for method in sorted(methods & MUTATING):
            full = prefix + route.path
            path = full.replace("{", "").replace("}", "")
            r = client.request(method, path, json={})
            assert r.status_code == 403, (method, full, r.status_code)
            code = r.json()["detail"]["error"]
            assert code in ("BRT-RPL-001", "BRT-RPL-002"), (method, full)
            tried.add((method, full))
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
    assert view["digest"] == dict(export["view"]["digest"], direct_floor_ratio=None)
    assert client.get("/runs/00000000-0000-0000-0000-000000000000").status_code == 404


def test_with_no_speed_set_a_run_plays_in_at_most_60_seconds(client, monkeypatch):
    """Lead ruling (item 7): speed = max(1, recorded duration / 60) when nothing is asked."""
    monkeypatch.delenv("BORG_REPLAY_SPEED", raising=False)
    for export in _exports():
        end_s = export["timeline"]["end_ms"] / 1000
        sid = replay.start_session(export["run_id"])
        speed = replay._sessions[sid]["speed"]
        assert speed == pytest.approx(max(1.0, end_s / 60))
        assert end_s / speed <= 60.0 + 1e-9
    assert any(e["timeline"]["end_ms"] > 60000 for e in _exports())   # the rule is exercised
    assert replay.default_speed({"timeline": {"end_ms": 30000}}) == 1.0  # a short run: recorded speed
    runs = client.get("/replay/status").json()["runs"]
    assert all(r["play_seconds"] <= 60.0 + 1e-9 for r in runs)


def test_an_explicit_speed_wins_over_the_60_second_rule(client, monkeypatch):
    export = max(_exports(), key=lambda e: e["timeline"]["end_ms"])
    monkeypatch.setenv("BORG_REPLAY_SPEED", "1")
    sid = replay.start_session(export["run_id"])
    assert replay._sessions[sid]["speed"] == 1.0                     # recorded speed, as asked
    sid = replay.start_session(export["run_id"], speed=3.0)          # the request's replay_speed
    assert replay._sessions[sid]["speed"] == 3.0


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


def _receipts(tmp_path, source_view, replayed_view):
    script = (
        "const {BorgWork: W} = H.load({extra: ['workspace.js']});"
        f"const a = {json.dumps(source_view)}; const b = {json.dumps(replayed_view)};"
        "const pick = (d) => { const r = W.receiptFrom(d);"
        " return {r: r, tiles: W.headerTiles(d), note: W.floorNote(r.estimate)}; };"
        "return JSON.stringify([pick(a), pick(b)]).replace(/[\\u007f-\\uffff]/g, c => '');"
    )
    return json.loads(node.run_js(tmp_path, script))


def _same_totals(source, played):
    # Lead ruling (item 4): the benchmark ratio sentence shows live, never in replay.
    assert "round-1 benchmark" in source["note"]
    assert played["note"] and "round-1 benchmark" not in played["note"]
    assert "between" not in played["note"] and played["r"]["estimate"]["ratio"] is None
    # Every total is as recorded: the receipt and the header tiles, the ratio aside.
    source["r"]["estimate"]["ratio"] = None
    assert played["r"] == source["r"] and played["tiles"] == source["tiles"]


@pytest.mark.skipif(node.NODE is None, reason="node is not on PATH")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_a_replayed_run_renders_the_same_receipt_totals_as_the_export(client, tmp_path, path):
    from src.registry.cost import DIRECT_FLOOR_RATIO_RANGE

    export = json.loads(path.read_text(encoding="utf-8"))
    _, _, run_id = _play(client, export)
    replayed = client.get(f"/runs/{run_id}").json()
    # The export carries no ratio; the live run view adds it (get_run_state), so add it back.
    live = json.loads(json.dumps(export["view"]))
    live["digest"]["direct_floor_ratio"] = list(DIRECT_FLOOR_RATIO_RANGE)
    source, played = _receipts(tmp_path, live, replayed)
    _same_totals(source, played)
    assert source["r"]["cost"] == export["view"]["digest"]["cost_dollars"]


def _demo_db():
    for base in (os.environ.get("BORG_DATA_ROOT"), str(ROOT)):
        if base and (Path(base) / "demo" / "runs" / "db" / "demo.db").exists():
            return Path(base) / "demo" / "runs" / "db" / "demo.db"
    return None


@pytest.mark.skipif(node.NODE is None, reason="node is not on PATH")
@pytest.mark.skipif(_demo_db() is None, reason="the git-ignored demo database is not here "
                                                "(set BORG_DATA_ROOT to the main checkout)")
@pytest.mark.parametrize("path", EXPORTS, ids=lambda p: p.name)
def test_the_replay_receipt_equals_the_source_runs_own_run_view(client, tmp_path, monkeypatch, path):
    """Review item 6: GET /runs/{id} of the SOURCE run, from a temp copy of demo.db opened
    read-only, against the replayed export."""
    import sqlite3

    from src.api import tasks

    export = json.loads(path.read_text(encoding="utf-8"))
    copy = tmp_path / "demo_copy.db"
    src = sqlite3.connect(f"file:{_demo_db().as_posix()}?mode=ro", uri=True)
    dst = sqlite3.connect(str(copy))
    src.backup(dst)
    dst.close()
    src.close()
    lane_on = export["lane_mode"] != "off"

    async def lane_is_on():
        return lane_on

    import importlib.util
    spec = importlib.util.spec_from_file_location("export_run", ROOT / "scripts" / "export_run.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(tasks, "DB_PATH", str(copy))
    monkeypatch.setattr(tasks, "lane_admin_allowed", lambda request=None: False)
    monkeypatch.setattr(tasks, "_lane_is_on", lane_is_on)
    resp = asyncio.run(tasks.get_run_state(export["run_id"], request=mod._Request(mod.load_matrix())))
    source_view = json.loads(resp.body)
    assert source_view["digest"]["direct_floor_ratio"]          # the live view states the ratio
    _, _, run_id = _play(client, export)
    replayed = client.get(f"/runs/{run_id}").json()
    source, played = _receipts(tmp_path, source_view, replayed)
    _same_totals(source, played)


# --- the fixture and the startup guard ------------------------------------------------------

def test_the_outbound_fixture_catches_a_real_attempt(outbound):
    """Check the checker: each way out trips the fixture and is counted."""
    async def attempts():
        async with httpx.AsyncClient() as c:
            with pytest.raises(AssertionError):
                await c.get("http://127.0.0.1:9/")
        with pytest.raises(AssertionError):
            await asyncio.open_connection("127.0.0.1", 9)
        loop = asyncio.get_running_loop()
        s = socket.socket()
        s.setblocking(False)
        try:
            with pytest.raises(AssertionError):
                await loop.sock_connect(s, ("127.0.0.1", 9))
        finally:
            s.close()

    asyncio.run(attempts())
    with pytest.raises(AssertionError):
        httpx.Client().get("http://127.0.0.1:9/")
    with pytest.raises(AssertionError):
        socket.create_connection(("127.0.0.1", 9))
    assert len(outbound) >= 5


def test_replay_startup_writes_no_database(monkeypatch, tmp_path, outbound):
    """main.py's lifespan skips create_all in replay: no call, no file. Fails if the guard
    is removed (the spy would be called and would create the file)."""
    import src.db.schema as schema

    real = schema.create_all
    calls = []

    async def spy(db_path=None):
        calls.append(db_path)
        await real(str(tmp_path / "borg_route.db"))

    monkeypatch.setattr(schema, "create_all", spy)
    monkeypatch.setenv("BORG_DEMO_MODE", "replay")
    monkeypatch.setenv("BORG_REPLAY_DIR", str(RUNS))
    replay.reset()
    with TestClient(app) as c:
        assert c.get("/replay/status").json()["mode"] == "replay"
    assert calls == []
    assert list(tmp_path.glob("*.db")) == []
    # And the spy does work when the guard lets it through (live mode).
    monkeypatch.delenv("BORG_DEMO_MODE")
    with TestClient(app):
        pass
    assert len(calls) == 1 and (tmp_path / "borg_route.db").exists()


@pytest.mark.parametrize("utc,day", [
    ("2026-11-18 03:30:00", "2026-11-17"),     # EST, UTC-5: still the evening before
    ("2026-11-18 05:30:00", "2026-11-18"),
    ("2026-07-01 03:59:00", "2026-06-30"),     # EDT, UTC-4
    ("2026-07-01 04:00:00", "2026-07-01"),
])
def test_the_notice_date_is_the_toronto_date(utc, day):
    from datetime import datetime

    assert replay.toronto_date(datetime.fromisoformat(utc)).isoformat() == day
    doc = {"view": {"run": {"created_at": utc}}, "recorded_on": "1999-01-01"}
    assert replay.notice({"r": doc}) == "Replay of a recorded run on " + replay._day(day)


# ---------- planner, under ruling 38 (c): no wait plays longer than 4 s; recorded times shown ----------

GAP_DOC = {"timeline": {"end_ms": 602000, "stages": [
    {"stage": "plan", "t_ms": 0}, {"stage": "route", "t_ms": 1000}, {"stage": "compose", "t_ms": 601000}]}}


def test_a_ten_minute_gap_plays_in_at_most_4_seconds():
    speed = replay.default_speed(GAP_DOC)                       # 602 s recorded: about 10x
    knots = replay.play_map(GAP_DOC, speed)
    gap = replay.play_seconds_at(knots, 601000) - replay.play_seconds_at(knots, 1000)
    assert gap == pytest.approx(replay.GAP_CAP_S)
    assert replay.play_seconds_at(knots, 1000) == pytest.approx(1.0 / speed)   # short waits keep their pace
    assert knots[-1][1] <= 60.0
    assert replay.play_map(GAP_DOC, 1.0)[-1][1] == pytest.approx(1 + 4 + 1)    # an explicit speed is capped too


def test_the_play_clock_maps_back_to_the_recorded_moment():
    knots = replay.play_map(GAP_DOC, 1.0)
    for rec in (0, 500, 1000, 300000, 601000, 601500, 602000):
        assert replay.recorded_ms_at(knots, replay.play_seconds_at(knots, rec)) == pytest.approx(rec)
    assert replay.recorded_ms_at(knots, knots[-1][1] + 1) > 602000             # past the end: finished


def test_failing_case_a_run_with_no_stages_or_length_still_plays(client):
    knots = replay.play_map({"timeline": {}}, 1.0)
    assert knots == [(0.0, 0.0)] and replay.play_seconds_at(knots, 0) == 0.0
    assert replay.recorded_ms_at(knots, 0.5) > 0


def test_each_stage_carries_its_recorded_duration(client, tmp_path, monkeypatch):
    export = _exports()[0]
    doc = dict(export, timeline=GAP_DOC["timeline"])
    (tmp_path / "gap.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("BORG_REPLAY_DIR", str(tmp_path))
    replay.reset()
    body = {"model": "borg_route", "stream": True, "replay_speed": FAST,
            "messages": [{"role": "user", "content": doc["prompt"]}]}
    got = []
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and '"event": "stage"' in line:
                got.append(json.loads(line[5:]))
    assert [(g["stage"], g["recorded_ms"]) for g in got] == [("plan", 1000), ("route", 600000), ("compose", 1000)]
    st = client.get("/replay/status").json()["runs"][0]
    assert st["seconds"] == 602.0 and st["play_seconds"] <= 4 + 2 * 60 / 602 * 1.01


def test_same_moment_stages_and_the_last_stage_show_no_zero():
    stages = [{"stage": "plan", "t_ms": 71000}, {"stage": "route", "t_ms": 71000},
              {"stage": "re_plan", "t_ms": 80000}, {"stage": "compose", "t_ms": 91000}]
    assert replay.stage_durations(stages, 91000) == [None, 9000, 11000, None]


def test_the_wait_before_the_first_stage_is_labelled(client, tmp_path, monkeypatch):
    export = _exports()[0]
    doc = dict(export, timeline={"end_ms": 90000, "stages": [
        {"stage": "plan", "t_ms": 71000}, {"stage": "route", "t_ms": 71000}, {"stage": "compose", "t_ms": 90000}]})
    (tmp_path / "lead.json").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("BORG_REPLAY_DIR", str(tmp_path))
    replay.reset()
    body = {"model": "borg_route", "stream": True, "replay_speed": FAST,
            "messages": [{"role": "user", "content": doc["prompt"]}]}
    got = []
    with client.stream("POST", "/v1/chat/completions", json=body) as r:
        for line in r.iter_lines():
            if line.startswith("data:") and '"event": "stage"' in line:
                got.append(json.loads(line[5:]))
    assert [(g["stage"], g.get("recorded_ms")) for g in got] == [
        ("starting", 71000), ("plan", None), ("route", 19000), ("compose", None)]
