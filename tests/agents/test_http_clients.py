"""Work order 1.12: model and tool sends reuse long-lived HTTP clients.

One ``httpx.AsyncClient`` per (trust_env, timeout) kind, created lazily per
event loop, kept on the loop object and closed when the loop ends. Local
calls keep ``trust_env=False`` (lane rule); cloud calls keep today's client.
"""
from __future__ import annotations

import asyncio
import gc
import ssl
import weakref

import httpx
import pytest

from src.lane import outbound
from src.lane.context import lane_scope, prepare_lane

CLOUD = "https://openrouter.ai/api/v1/chat/completions"
LOCAL = "http://127.0.0.1:1234/v1/chat/completions"
SENDS = 10


@pytest.fixture
def fake_post(monkeypatch):
    """Every POST answers 200 without touching the network."""
    async def post(client, url, json=None, headers=None, **kwargs):  # noqa: A002
        return httpx.Response(200, json={"ok": True}, request=httpx.Request("POST", url))

    monkeypatch.setattr(httpx.AsyncClient, "post", post)


@pytest.fixture
def built(monkeypatch) -> list[dict]:
    """The keyword arguments of every AsyncClient constructed."""
    seen: list[dict] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        seen.append(kwargs)
        real_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
    return seen


@pytest.fixture
async def lane_db(tmp_path, monkeypatch):
    from src.db.schema import create_all

    db = str(tmp_path / "clients.db")
    await create_all(db)
    for name in ("BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
    return db


async def test_lane_off_sends_build_one_client(fake_post, built):
    for _ in range(SENDS):
        resp = await outbound.send_outbound(CLOUD, {"x": 1}, timeout=30)
        assert resp.status_code == 200
    assert len(built) == 1
    assert "trust_env" not in built[0]   # today's cloud client: proxy variables honoured


async def test_lane_on_sends_build_at_most_one_client_per_kind(fake_post, built, lane_db):
    ctx = await prepare_lane(None, lane_db)
    with lane_scope(ctx):
        for _ in range(SENDS):
            await outbound.send_outbound(LOCAL, {"x": 1}, timeout=30)
            await outbound.send_outbound(CLOUD, {"x": 1}, timeout=30)
    assert len(built) == 2, built
    local = [kw for kw in built if kw.get("trust_env") is False]
    assert len(local) == 1                       # local calls ignore proxy variables
    # Every send was still recorded, each with the right left_machine.
    import sqlite3

    with sqlite3.connect(lane_db) as conn:
        rows = conn.execute("SELECT left_machine FROM outbound_requests").fetchall()
    assert sorted(r[0] for r in rows) == [0] * SENDS + [1] * SENDS


async def test_a_different_timeout_is_a_different_kind(fake_post, built):
    for timeout in (30, 30, 120, 120):
        await outbound.send_outbound(CLOUD, {}, timeout=timeout)
    assert sorted(kw["timeout"] for kw in built) == [30, 120]


async def test_no_certificate_load_per_send(fake_post, monkeypatch, lane_db):
    """The stand-in for the 1.5 s block: loading the CA bundle happens once
    per client kind, not once per send."""
    loads = {"n": 0}
    real = ssl.SSLContext.load_verify_locations

    def counting(self, *args, **kwargs):
        loads["n"] += 1
        return real(self, *args, **kwargs)

    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", counting)
    ctx = await prepare_lane(None, lane_db)
    with lane_scope(ctx):
        for _ in range(SENDS):
            await outbound.send_outbound(LOCAL, {}, timeout=30)
            await outbound.send_outbound(CLOUD, {}, timeout=30)
    assert 1 <= loads["n"] <= 2, loads["n"]


async def test_the_streamed_relay_client_is_shared_and_ignores_proxies(built):
    first = await outbound.client_for(True, 120)
    again = await outbound.client_for(True, 120)
    cloud = await outbound.client_for(False, 120)
    assert first is again and first is not cloud
    assert [kw.get("trust_env") for kw in built] == [False, None]


def test_clients_are_closed_when_their_loop_ends_and_free_the_loop():
    loops: list = []

    async def use() -> list:
        loops.append(weakref.ref(asyncio.get_running_loop()))
        return [await outbound.shared_client(False, 30), await outbound.shared_client(True, 30)]

    first = asyncio.run(use())
    second = asyncio.run(use())
    assert all(c.is_closed for c in first + second)
    assert not set(map(id, first)) & set(map(id, second))   # a new loop, new clients
    gc.collect()
    assert [r for r in loops if r() is not None] == []


async def test_a_closed_client_is_replaced():
    client = await outbound.shared_client(True, 30)
    await client.aclose()
    assert (await outbound.shared_client(True, 30)) is not client
