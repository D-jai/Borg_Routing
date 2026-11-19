"""Review (4) of work order 1.12 (17-Nov-2026): the shared HTTP clients.

A tiny local HTTP server stands in for a model server. Covers: the streamed
bypass relay releases its connection before the non-stream fallback (no pool
deadlock), pool exhaustion has its own error code (BRT-ANS-011) in the
answerer and orchestrator paths, shared clients never keep cookies, and the
relay itself goes through the shared client.
"""
from __future__ import annotations

import asyncio
import json
import ssl
import uuid

import httpx
import pytest

from src.lane import outbound

PROXY_VARS = ("HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy",
              "HTTPS_PROXY", "https_proxy")


class FakeUpstream:
    """Minimal HTTP/1.1 server on 127.0.0.1. ``stream_status`` answers the
    ``"stream": true`` requests; the others get a chat completion after
    ``delay`` seconds. ``cookie`` adds a Set-Cookie header. Every request's
    Cookie header is kept in ``cookies_seen``."""

    def __init__(self, stream_status: int = 500, delay: float = 0.02, cookie: str | None = None):
        self.stream_status = stream_status
        self.delay = delay
        self.cookie = cookie
        self.cookies_seen: list[str | None] = []
        self.requests = 0
        self.server: asyncio.base_events.Server | None = None
        self.port = 0

    async def start(self) -> "FakeUpstream":
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def stop(self) -> None:
        self.server.close()
        await self.server.wait_closed()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            headers = {}
            for line in head.decode("latin-1").split("\r\n")[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()
            body = json.loads(await reader.readexactly(int(headers.get("content-length", 0))) or b"{}")
            self.requests += 1
            self.cookies_seen.append(headers.get("cookie"))
            if body.get("stream"):
                status, payload = self.stream_status, b"no streaming"
            else:
                await asyncio.sleep(self.delay)
                status = 200
                payload = json.dumps({
                    "choices": [{"message": {"content": "answer"}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
                }).encode()
            extra = f"Set-Cookie: {self.cookie}\r\n" if self.cookie else ""
            writer.write(
                f"HTTP/1.1 {status} X\r\nContent-Length: {len(payload)}\r\n"
                f"Content-Type: application/json\r\n{extra}Connection: close\r\n\r\n".encode()
                + payload
            )
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()


@pytest.fixture(autouse=True)
def no_proxy_env(monkeypatch):
    for name in PROXY_VARS:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
async def relay_env(tmp_path, monkeypatch):
    from src.api import chat_completions as cc
    from src.db.schema import create_all

    db = str(tmp_path / "pool.db")
    await create_all(db)
    monkeypatch.setattr(cc, "DB_PATH", db)
    return cc


def _matrix(tmp_path, port: int):
    from src.registry.capability_matrix import CapabilityMatrix
    from src.registry.models import ModelInfo

    matrix = CapabilityMatrix(path=str(tmp_path / "m.yaml"))
    matrix.upsert(ModelInfo(model_id="local-llm", source="lmstudio",
                            endpoint=f"http://127.0.0.1:{port}", context_length=4096))
    return matrix


async def _relay_once(cc, matrix) -> str:
    run_id = str(uuid.uuid4())
    out = [c async for c in cc._stream_bypass_relay(
        matrix, "local-llm", "local-llm",
        [cc.ChatMessage(role="user", content="hi")], run_id)]
    return "".join(out)


async def test_many_concurrent_relays_fall_back_without_pool_timeout(
    relay_env, tmp_path, monkeypatch
):
    monkeypatch.setattr(outbound, "POOL_MAX_CONNECTIONS", 4)
    upstream = await FakeUpstream(stream_status=500).start()
    try:
        matrix = _matrix(tmp_path, upstream.port)
        outs = await asyncio.wait_for(
            asyncio.gather(*[_relay_once(relay_env, matrix) for _ in range(30)]), 20)
    finally:
        await upstream.stop()
    assert len(outs) == 30
    for text in outs:
        assert "BRT-API-013" not in text and "PoolTimeout" not in text, text[:200]
        assert "answer" in text and text.rstrip().endswith("data: [DONE]")


async def test_pool_exhaustion_is_its_own_error_in_the_answerer_path(monkeypatch):
    from src.agents import executor

    monkeypatch.setattr(outbound, "POOL_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(executor, "_MODEL_CALL_TIMEOUT_SEC", 0.3)
    upstream = await FakeUpstream(delay=1.0).start()
    url = f"http://127.0.0.1:{upstream.port}/v1/chat/completions"
    try:
        results = await asyncio.gather(
            *[executor._post_chat_completion(url, {}, {"model": "m"}) for _ in range(2)],
            return_exceptions=True,
        )
    finally:
        await upstream.stop()
    messages = [str(r) for r in results if isinstance(r, ValueError)]
    assert any(m.startswith("BRT-ANS-011") for m in messages), results
    assert not any(m.startswith("BRT-ANS-006") and "pool" in m for m in messages)
    assert "pool exhausted" in " ".join(messages)


async def test_pool_exhaustion_is_its_own_error_in_the_orchestrator_path(monkeypatch):
    from src.orchestrator import engine
    from src.registry.models import ModelInfo

    async def exhausted(*args, **kwargs):
        raise httpx.PoolTimeout("pool full")

    monkeypatch.setattr(engine, "send_outbound", exhausted)
    model = ModelInfo(model_id="local-llm", source="lmstudio",
                      endpoint="http://127.0.0.1:1", context_length=4096)
    with pytest.raises(ValueError) as info:
        await engine._invoke_orchestrator_model(
            "plan", uuid.uuid4(), "do it", 0, 3, [], "", {}, {}, "",
            {"global_rules": "", "admin_rules_ledger": ""}, "", model, ":memory:")
    assert str(info.value).startswith("BRT-ANS-011")


async def test_a_set_cookie_from_one_provider_is_never_sent_to_another():
    first = await FakeUpstream(cookie="session=secret-A; Path=/").start()
    second = await FakeUpstream().start()
    try:
        for _ in range(2):
            await outbound.send_outbound(
                f"http://127.0.0.1:{first.port}/v1/chat/completions", {"m": 1}, timeout=10)
            await outbound.send_outbound(
                f"http://127.0.0.1:{second.port}/v1/chat/completions", {"m": 1}, timeout=10)
    finally:
        await first.stop()
        await second.stop()
    assert first.cookies_seen == [None, None]
    assert second.cookies_seen == [None, None]


async def test_the_relay_uses_the_shared_client_and_a_local_target_ignores_proxies(
    relay_env, tmp_path, monkeypatch
):
    loads = {"n": 0}
    real = ssl.SSLContext.load_verify_locations

    def counting(self, *a, **kw):
        loads["n"] += 1
        return real(self, *a, **kw)

    built: list[dict] = []
    real_init = httpx.AsyncClient.__init__

    def spy(self, *args, **kwargs):
        built.append(kwargs)
        real_init(self, *args, **kwargs)

    # A proxy variable that would swallow the request if it were honoured.
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)
    upstream = await FakeUpstream(stream_status=500).start()
    try:
        matrix = _matrix(tmp_path, upstream.port)
        from src.lane.context import lane_scope, prepare_lane
        from src.db.schema import create_all

        monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
        ldb = str(tmp_path / "lane.db")
        await create_all(ldb)
        ctx = await prepare_lane(None, ldb)
        with lane_scope(ctx):
            await _relay_once(relay_env, matrix)        # warm up: builds the clients
        monkeypatch.setattr(httpx.AsyncClient, "__init__", spy)
        monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", counting)
        with lane_scope(ctx):
            texts = [await _relay_once(relay_env, matrix) for _ in range(5)]
        shared = await outbound.client_for(True, 120)
    finally:
        await upstream.stop()
    assert all("answer" in t for t in texts), texts[0][:200]   # reached the server, not the proxy
    assert built == [], built                    # no AsyncClient built per call
    assert loads["n"] == 0                       # no certificate load per call
    assert shared.is_closed is False


async def test_open_relay_streams_do_not_starve_an_answerer_call(
    relay_env, tmp_path, monkeypatch
):
    """Round-2 review (3): the streamed relay has its own pool kind, so open
    streams cannot hold the connections an answerer call needs."""
    from src.agents import executor

    monkeypatch.setattr(outbound, "POOL_MAX_CONNECTIONS", 2)
    assert executor._MODEL_CALL_TIMEOUT_SEC == 120   # same timeout as the relay: only the kind separates them
    held: list[asyncio.StreamWriter] = []

    async def hang(reader, writer):
        held.append(writer)
        try:
            await reader.read(-1)       # never answers
        except Exception:
            pass

    slow = await asyncio.start_server(hang, "127.0.0.1", 0)
    slow_port = slow.sockets[0].getsockname()[1]
    fast = await FakeUpstream(delay=0.01).start()
    matrix = _matrix(tmp_path, slow_port)
    tasks = [asyncio.create_task(_relay_once(relay_env, matrix)) for _ in range(2)]
    try:
        await asyncio.sleep(0.5)        # both relays now hold their connections
        text = await asyncio.wait_for(executor._post_chat_completion(
            f"http://127.0.0.1:{fast.port}/v1/chat/completions", {}, {"model": "m"}), 5)
    finally:
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        for w in held:
            w.close()
        slow.close()
        await fast.stop()
    assert text[0] == "answer"


async def test_relay_client_is_a_separate_kind_with_the_same_policy():
    relay = await outbound.relay_client_for(True, 120)
    other = await outbound.client_for(True, 120)
    assert relay is not other
    assert relay is await outbound.relay_client_for(True, 120)
    assert relay._trust_env is False
    assert relay.timeout.read == 120
    assert list(relay.cookies.jar) == []
