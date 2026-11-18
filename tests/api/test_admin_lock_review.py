"""4.4 review fixes: /logs is admin-only, run detail hides error text, ASCII-only token.

TC-API-067 - GET /logs needs the admin lock
TC-API-068 - run detail events carry no error text or payload for a non-admin while the lane is on
TC-API-069 - a non-ASCII BORG_ADMIN_TOKEN is reported as misconfigured, never a silent mismatch
"""
import json
import uuid

import aiosqlite
import pytest
from fastapi.testclient import TestClient

from main import app
from src.api import logs as logs_api, tasks as tasks_api
from src.db.schema import create_all

TOKEN = "s3cret-admin-token"
CANARY = "CONFIDENTIAL-MERGER-TERMS Acme pays 41M"


def _client(host="10.0.0.5"):
    return TestClient(app, raise_server_exceptions=False, client=(host, 50000))


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


async def _seed(tmp_path, monkeypatch, lane_mode):
    db = str(tmp_path / "rev.db")
    await create_all(db)
    run_id = str(uuid.uuid4())
    route = {"mode": "route", "served_by": "model", "model_id": "m1", "why": "cheapest fit"}
    err = {"code": "BRT-ANS-003", "error": "Raw: " + json.dumps({"quote": CANARY})}
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO runs (run_id, user_instruction, status) VALUES (?, ?, ?)",
            (run_id, "summarise", "completed"),
        )
        await conn.execute(
            "INSERT INTO logs (run_id, event_type, model_id, tokens_input, tokens_output, dollars, latency_ms, decision_json)"
            " VALUES (?, 'error', 'm1', 10, 5, 0.01, 120, ?)",
            (run_id, json.dumps(err)),
        )
        await conn.execute(
            "INSERT INTO logs (run_id, event_type, model_id, decision_json) VALUES (?, 'orchestrator_decision', 'm1', ?)",
            (run_id, json.dumps(route)),
        )
        if lane_mode:
            await conn.execute("INSERT INTO config (key, value) VALUES ('lane_mode', ?)", (json.dumps(lane_mode),))
        await conn.commit()
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    monkeypatch.setattr(logs_api, "DB_PATH", db)
    return run_id


# ---------- GET /logs ----------

@pytest.mark.asyncio
async def test_logs_list_needs_the_admin_lock(tmp_path, monkeypatch):
    await _seed(tmp_path, monkeypatch, None)
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    c = _client()
    assert c.get("/logs").status_code == 401
    assert c.get("/logs", headers=_bearer("nope")).status_code == 401
    assert c.get("/logs", headers=_bearer(TOKEN)).status_code == 200


def test_logs_list_refused_remote_without_a_token(monkeypatch):
    assert _client("10.0.0.5").get("/logs").status_code == 403
    assert _client("127.0.0.1").get("/logs").status_code != 403


# ---------- run detail events ----------

@pytest.mark.asyncio
async def test_run_detail_hides_error_text_from_a_non_admin_when_lane_on(tmp_path, monkeypatch):
    run_id = await _seed(tmp_path, monkeypatch, "raw_local")
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    c = _client()
    r = c.get(f"/runs/{run_id}")
    assert r.status_code == 200
    assert CANARY not in r.text
    events = {e["event_type"]: e for e in r.json()["events"]}
    err = events["error"]
    assert err["decision_json"] is None
    assert err["model_id"] == "m1" and err["tokens_input"] == 10 and err["dollars"] == 0.01
    assert err["latency_ms"] == 120 and err["created_at"]
    assert json.loads(events["orchestrator_decision"]["decision_json"])["mode"] == "route"
    admin = c.get(f"/runs/{run_id}", headers=_bearer(TOKEN))
    assert CANARY in admin.text


@pytest.mark.asyncio
async def test_run_detail_unchanged_when_lane_off(tmp_path, monkeypatch):
    run_id = await _seed(tmp_path, monkeypatch, None)
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    assert CANARY in _client().get(f"/runs/{run_id}").text
    await _set_mode(tmp_path, "off")
    assert CANARY in _client().get(f"/runs/{run_id}").text


async def _set_mode(tmp_path, mode):
    async with aiosqlite.connect(str(tmp_path / "rev.db")) as conn:
        await conn.execute("INSERT OR REPLACE INTO config (key, value) VALUES ('lane_mode', ?)", (json.dumps(mode),))
        await conn.commit()


@pytest.mark.asyncio
async def test_run_detail_loopback_without_token_sees_everything(tmp_path, monkeypatch):
    run_id = await _seed(tmp_path, monkeypatch, "all_local")
    assert CANARY in _client("127.0.0.1").get(f"/runs/{run_id}").text


# ---------- non-ASCII token ----------

@pytest.mark.parametrize("bad", ["été-token", "tok en", "tok x"])
def test_non_ascii_token_is_reported_misconfigured(monkeypatch, bad):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", bad)
    c = _client()
    status = c.get("/keys/status").json()
    assert status["admin_token_set"] is True and status["admin_token_valid"] is False
    r = c.get("/logs/entities", headers=_bearer("anything"))
    assert r.status_code == 500
    assert r.json()["detail"]["error"] == "BRT-API-047"
    assert bad not in r.text
    # even the right-looking header cannot pass: it is never a silent mismatch
    assert c.post("/config", json={}, headers={"Authorization": "Bearer x"}).json()["detail"]["error"] == "BRT-API-047"


def test_ascii_token_is_valid(monkeypatch):
    c = _client()
    assert c.get("/keys/status").json()["admin_token_valid"] is True  # unset counts as valid
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    assert c.get("/keys/status").json()["admin_token_valid"] is True


# ---------- ruling 28: a run-start speed-probe warning in a lane run ----------

async def _seed_probe_warning(tmp_path, monkeypatch, model_id):
    db = str(tmp_path / "probe.db")
    await create_all(db)
    run_id = str(uuid.uuid4())
    warn = {"code": "BRT-REG-016", "severity": "warning", "model_id": model_id,
            "msg": "Could not measure the speed of x (" + CANARY + ")", "extra": CANARY}
    async with aiosqlite.connect(db) as conn:
        await conn.execute("INSERT INTO runs (run_id, user_instruction, status) VALUES (?, ?, ?)",
                           (run_id, "summarise", "completed"))
        await conn.execute("INSERT INTO logs (run_id, event_type, model_id, decision_json) VALUES (?, 'error', ?, ?)",
                           (run_id, model_id, json.dumps(warn)))
        await conn.execute("INSERT INTO config (key, value) VALUES ('lane_mode', ?)", (json.dumps("raw_local"),))
        await conn.commit()
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    return run_id


@pytest.mark.asyncio
async def test_a_speed_probe_warning_reaches_a_non_admin_as_code_model_and_severity_only(tmp_path, monkeypatch):
    run_id = await _seed_probe_warning(tmp_path, monkeypatch, "ollama/llama3.2:3b")
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    r = _client().get(f"/runs/{run_id}")
    assert r.status_code == 200 and CANARY not in r.text
    err = next(e for e in r.json()["events"] if e["event_type"] == "error")
    assert json.loads(err["decision_json"]) == {
        "code": "BRT-REG-016", "severity": "warning", "model_id": "ollama/llama3.2:3b"}


@pytest.mark.asyncio
async def test_a_speed_probe_warning_drops_a_model_id_of_the_wrong_shape(tmp_path, monkeypatch):
    run_id = await _seed_probe_warning(tmp_path, monkeypatch, "ollama/x " + CANARY)
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    r = _client().get(f"/runs/{run_id}")
    err = next(e for e in r.json()["events"] if e["event_type"] == "error")
    assert json.loads(err["decision_json"]) == {"code": "BRT-REG-016", "severity": "warning"}
    assert "CONFIDENTIAL" not in err["decision_json"]
