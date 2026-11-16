"""Admin lock tests (work order 4.4).

TC-API-060 — a missing or wrong token gets 401 (BRT-API-045) on every guarded route
TC-API-061 — the right token passes every guarded route
TC-API-062 — token unset: loopback passes, any other address gets 403 (BRT-API-046)
TC-API-063 — the token is compared with hmac.compare_digest, on bytes
TC-API-064 — GET /runs/{id} shows outbound bodies only to an admin
TC-API-065 — open routes still work from a non-loopback client with no token
TC-API-066 — /keys/status reports whether the token is set, never the token

Guarded routes are sent an invalid body wherever one is needed. A request that
passes the lock then stops at 422 (or a handler error) without writing a file,
so no test here changes .env, skills.yaml or the matrix.
"""
import hmac
import uuid
from unittest.mock import AsyncMock, patch

import aiosqlite
import pytest
from fastapi import Request
from fastapi.testclient import TestClient

from main import app
from src.api import auth, tasks as tasks_api
from src.db.schema import create_all

TOKEN = "s3cret-admin-token"

# (method, path, json body or None): every route the lock guards.
GUARDED = [
    ("POST", "/keys", {}),
    ("POST", "/config", {}),
    ("POST", "/config/rules", {}),
    ("POST", "/config/rules/not-a-rule/retire", None),
    ("POST", "/models/discover", None),
    ("POST", "/models/research", {}),
    ("PATCH", "/models/some-model/checked", {}),
    ("POST", "/skills/some-skill/version", {}),
    ("GET", "/logs/entities", None),
    ("GET", "/logs/export", None),
]


@pytest.fixture(autouse=True)
def _no_discovery(monkeypatch):
    """Discover and research would call the network (and spend) if they passed the lock: stub them."""
    monkeypatch.setattr("src.api.models.research_and_apply", AsyncMock(side_effect=RuntimeError("stub")))
    monkeypatch.setattr("src.api.models.discover_all", AsyncMock(side_effect=RuntimeError("stub")))


def _client(host="testclient"):
    return TestClient(app, raise_server_exceptions=False, client=(host, 50000))


def _call(c, method, path, body, headers=None):
    kw = {"headers": headers or {}}
    if body is not None:
        kw["json"] = body
    return c.request(method, path, **kw)


def _bearer(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_missing_token_is_401(monkeypatch, method, path, body):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    r = _call(_client(), method, path, body)
    assert r.status_code == 401
    assert r.json()["detail"]["error"] == "BRT-API-045"


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_wrong_token_is_401(monkeypatch, method, path, body):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    for bad in (_bearer("nope"), {"Authorization": TOKEN}, {"Authorization": "Basic " + TOKEN}):
        r = _call(_client(), method, path, body, bad)
        assert r.status_code == 401, (path, bad)


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_right_token_passes_even_from_a_remote_address(monkeypatch, method, path, body):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    r = _call(_client("10.0.0.5"), method, path, body, _bearer(TOKEN))
    assert r.status_code not in (401, 403), (path, r.status_code, r.text[:200])


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_unset_token_allows_loopback(method, path, body):
    for host in ("127.0.0.1", "127.8.9.1", "::1", "testclient"):
        r = _call(_client(host), method, path, body)
        assert r.status_code not in (401, 403), (path, host, r.status_code)


@pytest.mark.parametrize("method,path,body", GUARDED)
def test_unset_token_refuses_other_addresses(method, path, body):
    for host in ("10.0.0.5", "192.168.1.20", "8.8.8.8", "2001:db8::1", "example.com"):
        r = _call(_client(host), method, path, body)
        assert r.status_code == 403, (path, host)
        assert r.json()["detail"]["error"] == "BRT-API-046"


@pytest.mark.parametrize("empty", ["", "   "])
def test_empty_token_counts_as_unset(monkeypatch, empty):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", empty)
    assert _call(_client("10.0.0.5"), "POST", "/config", {}).status_code == 403
    assert _call(_client("127.0.0.1"), "POST", "/config", {}).status_code != 403


def test_token_is_read_at_request_time(monkeypatch):
    c = _client("10.0.0.5")
    assert _call(c, "POST", "/config", {}).status_code == 403
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    assert _call(c, "POST", "/config", {}).status_code == 401
    assert _call(c, "POST", "/config", {}, _bearer(TOKEN)).status_code == 422


def test_absent_client_is_not_loopback():
    class Req:
        client = None
        headers = {}

    assert auth.is_admin(Req()) is False
    assert auth.is_admin(None) is False


def test_ipv4_mapped_loopback_is_loopback():
    assert auth._is_loopback("::ffff:127.0.0.1") is True
    assert auth._is_loopback("::ffff:10.0.0.5") is False
    assert auth._is_loopback("") is False
    assert auth._is_loopback(None) is False


def test_compare_digest_is_used_on_bytes(monkeypatch):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    calls = []
    real = hmac.compare_digest

    def spy(a, b):
        calls.append((a, b))
        return real(a, b)

    monkeypatch.setattr(auth.hmac, "compare_digest", spy)
    _call(_client(), "POST", "/config", {}, _bearer(TOKEN))
    _call(_client(), "POST", "/config", {}, _bearer("wrong"))
    assert len(calls) == 2
    assert all(isinstance(a, bytes) and isinstance(b, bytes) for a, b in calls)


def test_token_never_appears_in_a_refusal(monkeypatch):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    r = _call(_client(), "POST", "/config", {}, _bearer("wrong"))
    assert TOKEN not in r.text


# ---------- GET /runs/{id}: outbound bodies are admin-only ----------

async def _seed_run_with_outbound(tmp_path, monkeypatch):
    db = str(tmp_path / "lock.db")
    await create_all(db)
    run_id = str(uuid.uuid4())
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO runs (run_id, user_instruction, status) VALUES (?, ?, ?)",
            (run_id, "lock test", "completed"),
        )
        await conn.commit()
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    monkeypatch.setattr(
        tasks_api, "get_outbound_requests",
        AsyncMock(return_value=[{"run_id": run_id, "url": "https://x", "body": "SECRET BODY"}]),
    )
    return run_id


@pytest.mark.asyncio
async def test_outbound_bodies_hidden_without_admin(tmp_path, monkeypatch):
    run_id = await _seed_run_with_outbound(tmp_path, monkeypatch)
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    c = _client("10.0.0.5")
    hidden = c.get(f"/runs/{run_id}")
    assert hidden.status_code == 200
    assert hidden.json()["outbound"][0]["body"] is None
    assert "SECRET BODY" not in hidden.text
    wrong = c.get(f"/runs/{run_id}", headers=_bearer("nope"))
    assert wrong.json()["outbound"][0]["body"] is None
    shown = c.get(f"/runs/{run_id}", headers=_bearer(TOKEN))
    assert shown.json()["outbound"][0]["body"] == "SECRET BODY"


@pytest.mark.asyncio
async def test_outbound_bodies_follow_the_loopback_rule_when_unset(tmp_path, monkeypatch):
    run_id = await _seed_run_with_outbound(tmp_path, monkeypatch)
    assert _client("127.0.0.1").get(f"/runs/{run_id}").json()["outbound"][0]["body"] == "SECRET BODY"
    remote = _client("10.0.0.5").get(f"/runs/{run_id}")
    assert remote.status_code == 200
    assert remote.json()["outbound"][0]["body"] is None


def test_lane_admin_allowed_is_the_one_hook(monkeypatch):
    class Req:
        def __init__(self, host, auth_header=None):
            self.client = type("C", (), {"host": host})()
            self.headers = {"authorization": auth_header} if auth_header else {}

    assert tasks_api.lane_admin_allowed() is False  # no request: fail closed
    assert tasks_api.lane_admin_allowed(Req("127.0.0.1")) is True
    assert tasks_api.lane_admin_allowed(Req("10.0.0.5")) is False
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    assert tasks_api.lane_admin_allowed(Req("10.0.0.5", "Bearer " + TOKEN)) is True
    assert tasks_api.lane_admin_allowed(Req("127.0.0.1")) is False


# ---------- open routes stay open ----------

OPEN = [
    ("GET", "/keys/status"),
    ("GET", "/models"),
    ("GET", "/models/matrix"),
    ("GET", "/config"),
    ("GET", "/config/effective"),
    ("GET", "/config/rules"),
    ("GET", "/skills"),
    ("GET", "/logs/cost-summary"),
    ("GET", "/runs"),
]


@pytest.mark.parametrize("method,path", OPEN)
@pytest.mark.parametrize("token", [None, TOKEN])
def test_read_routes_stay_open_from_a_remote_address(monkeypatch, method, path, token):
    if token:
        monkeypatch.setenv("BORG_ADMIN_TOKEN", token)
    r = _client("10.0.0.5").request(method, path)
    assert r.status_code not in (401, 403), (path, r.status_code)


def test_asking_questions_stays_open_from_a_remote_address(monkeypatch):
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    c = _client("10.0.0.5")

    async def mock_background(*a, **k):
        pass

    with patch("src.api.tasks._run_in_background", new=mock_background):
        r = c.post("/v1/task", json={"instruction": "Classify these issues."})
    assert r.status_code == 202
    run_id = r.json()["run_id"]
    assert c.get(f"/runs/{run_id}").status_code == 200
    chat = c.post("/v1/chat/completions", json={"model": "x", "messages": []})
    assert chat.status_code not in (401, 403)
    mcp = c.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert mcp.status_code == 200


def test_keys_status_reports_token_set_never_the_token(monkeypatch):
    c = _client("10.0.0.5")
    assert c.get("/keys/status").json()["admin_token_set"] is False
    monkeypatch.setenv("BORG_ADMIN_TOKEN", TOKEN)
    r = c.get("/keys/status")
    assert r.json()["admin_token_set"] is True
    assert TOKEN not in r.text
    monkeypatch.setenv("BORG_ADMIN_TOKEN", "  ")
    assert c.get("/keys/status").json()["admin_token_set"] is False
