"""Planner ruling 28 (a): a local model's speed is measured by one short
generation and stored with ``update_throughput``. HTTP is mocked: no test calls
a real model server (tests/conftest.py also blocks the probe's transport)."""
import logging

import pytest

from src.registry import throughput
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing


def _row(model_id, source, endpoint, pricing=None, tps=None, checked=True):
    return ModelInfo(
        model_id=model_id, source=source, endpoint=endpoint, context_length=8192,
        pricing=pricing, throughput_tps=tps, checked=checked,
    )


OLLAMA = _row("ollama/llama3.2:3b", "ollama", "http://localhost:11434")
LMSTUDIO = _row("lmstudio/qwen-7b", "lmstudio", "http://localhost:1234")
CLOUD = _row("openrouter/x/y", "openrouter", "https://openrouter.ai/api",
             pricing=Pricing(input_per_token=1e-6, output_per_token=2e-6))


def _matrix(*rows):
    m = CapabilityMatrix(path="__test_unused__.yaml")
    for r in rows:
        m.upsert(r.model_copy(deep=True))
    return m


class _Recorder:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def __call__(self, url, body, timeout):
        self.calls.append((url, body))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


@pytest.mark.asyncio
async def test_ollama_speed_is_the_servers_own_generation_rate():
    post = _Recorder([{"eval_count": 32, "eval_duration": 4_000_000_000, "load_duration": 9_000_000_000}])
    m = _matrix(OLLAMA)
    tps = await throughput.measure_throughput(m, OLLAMA.model_id, post=post)
    assert tps == pytest.approx(8.0)                    # 32 tokens in 4 s; the 9 s load is left out
    assert m.get(OLLAMA.model_id).throughput_tps == pytest.approx(8.0)
    url, body = post.calls[0]
    assert url == "http://localhost:11434/api/generate"
    assert body["model"] == "llama3.2:3b" and body["stream"] is False
    assert len(post.calls) == 1


@pytest.mark.asyncio
async def test_another_local_server_is_timed_on_a_second_call_after_a_warm_up():
    # The fake clock moves inside each fake call: the warm-up (it loads the model) takes 9 s,
    # the timed call 2 s. Timing from before the warm-up would give 30 / 11 s, not 15.
    now = [100.0]
    seconds = [9.0, 2.0]
    replies = [{"usage": {"completion_tokens": 32}}, {"usage": {"completion_tokens": 30}}]
    calls = []

    async def post(url, body, timeout):
        calls.append(url)
        now[0] += seconds[len(calls) - 1]
        return replies[len(calls) - 1]

    m = _matrix(LMSTUDIO)
    tps = await throughput.measure_throughput(m, LMSTUDIO.model_id, post=post, clock=lambda: now[0])
    assert tps == pytest.approx(15.0)                   # second call: 30 tokens in 2 s
    assert len(calls) == 2
    assert calls[1] == "http://localhost:1234/v1/chat/completions"


@pytest.mark.asyncio
async def test_a_failed_probe_logs_brt_reg_016_and_leaves_the_figure_unset(caplog):
    post = _Recorder([ConnectionError("refused")])
    m = _matrix(OLLAMA)
    with caplog.at_level(logging.WARNING):
        tps = await throughput.measure_throughput(m, OLLAMA.model_id, post=post)
    assert tps is None
    assert m.get(OLLAMA.model_id).throughput_tps is None
    assert "BRT-REG-016" in caplog.text


@pytest.mark.asyncio
async def test_a_cloud_model_or_a_measured_model_is_never_probed():
    post = _Recorder([])
    m = _matrix(CLOUD, OLLAMA.model_copy(update={"throughput_tps": 12.0}))
    assert await throughput.measure_unmeasured(m, post=post) == {}
    assert post.calls == []


@pytest.mark.asyncio
async def test_measure_unmeasured_probes_ticked_local_rows_only_by_default():
    post = _Recorder([{"eval_count": 10, "eval_duration": 1_000_000_000}])
    unticked = LMSTUDIO.model_copy(update={"checked": False})
    m = _matrix(OLLAMA, unticked)
    assert await throughput.measure_unmeasured(m, post=post) == {OLLAMA.model_id: pytest.approx(10.0)}
    assert m.get(unticked.model_id).throughput_tps is None


@pytest.mark.asyncio
async def test_the_suite_blocks_the_real_transport():
    with pytest.raises(RuntimeError, match="no real model call in tests"):
        await throughput._post_json("http://localhost:11434/api/generate", {}, 1.0)


# --- the app's own paths: Discover, the checkbox, the start of a run ----------

def _fake_probe(monkeypatch, figure=21.5):
    seen = []

    async def fake(url, body, timeout):
        seen.append(url)
        return {"eval_count": int(figure * 2), "eval_duration": 2_000_000_000}

    monkeypatch.setattr(throughput, "_post_json", fake)
    return seen


def _gated_probe(monkeypatch, figure):
    """A slow fake probe: it answers only when the test opens the gate."""
    import asyncio
    import threading

    gate = threading.Event()
    seen = []

    async def fake(url, body, timeout):
        seen.append(url)
        await asyncio.to_thread(gate.wait, 20)
        return {"eval_count": int(figure * 2), "eval_duration": 2_000_000_000}

    monkeypatch.setattr(throughput, "_post_json", fake)
    return gate, seen


def _wait_for(check, seconds=20.0):
    import time as _t

    end = _t.monotonic() + seconds
    while _t.monotonic() < end:
        if check():
            return True
        _t.sleep(0.05)
    return False


def _row_of(client, model_id):
    return next(r for r in client.get("/models").json() if r["model_id"] == model_id)


def test_discover_returns_at_once_and_the_figure_lands_afterwards(monkeypatch):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from main import app, matrix

    gate, seen = _gated_probe(monkeypatch, figure=21.5)

    async def fake_discover(mx, openrouter_key=None):
        mx.upsert(_row("ollama/probe-test:1b", "ollama", "http://localhost:11434", checked=False))
        return 1

    with TestClient(app) as client, patch("src.api.models.discover_all", new=fake_discover):
        resp = client.post("/models/discover")
        assert resp.status_code == 200                       # returned while the probe still waits
        assert matrix.get("ollama/probe-test:1b").throughput_tps is None
        assert _row_of(client, "ollama/probe-test:1b")["speed_status"] == "measuring"
        gate.set()
        assert _wait_for(lambda: matrix.get("ollama/probe-test:1b").throughput_tps is not None)
        assert matrix.get("ollama/probe-test:1b").throughput_tps == pytest.approx(21.5)
        assert _wait_for(lambda: _row_of(client, "ollama/probe-test:1b")["speed_status"] is None)
    assert seen == ["http://localhost:11434/api/generate"]


def test_a_tick_returns_before_a_slow_probe_ends_and_the_figure_lands_afterwards(monkeypatch):
    from fastapi.testclient import TestClient

    from main import app, matrix

    gate, seen = _gated_probe(monkeypatch, figure=7.0)
    with TestClient(app) as client:
        matrix.upsert(_row("ollama/tick-test:1b", "ollama", "http://localhost:11434", checked=False))
        resp = client.patch("/models/ollama/tick-test:1b/checked", json={"checked": True})
        assert resp.status_code == 200 and resp.json()["checked"] is True
        assert matrix.get("ollama/tick-test:1b").throughput_tps is None   # the probe has not ended
        assert _row_of(client, "ollama/tick-test:1b")["speed_status"] == "measuring"
        gate.set()
        assert _wait_for(lambda: matrix.get("ollama/tick-test:1b").throughput_tps is not None)
        assert matrix.get("ollama/tick-test:1b").throughput_tps == pytest.approx(7.0)
        assert len(seen) == 1
        # unticking calls nothing
        client.patch("/models/ollama/tick-test:1b/checked", json={"checked": False})
    assert len(seen) == 1


def test_a_failed_background_probe_shows_brt_reg_016_on_the_models_list():
    from fastapi.testclient import TestClient

    from main import app, matrix

    # tests/conftest.py blocks the real transport, so the probe fails
    with TestClient(app) as client:
        matrix.upsert(_row("ollama/fail-test:1b", "ollama", "http://localhost:11434", checked=False))
        client.patch("/models/ollama/fail-test:1b/checked", json={"checked": True})
        assert _wait_for(lambda: _row_of(client, "ollama/fail-test:1b")["speed_status"] == "BRT-REG-016")
        assert matrix.get("ollama/fail-test:1b").throughput_tps is None


# --- ruling 28 4a: a failed probe is remembered ---------------------------------

@pytest.mark.asyncio
async def test_a_failed_probe_is_not_retried_by_a_run_start_until_a_tick_re_measures_it(monkeypatch):
    from src.orchestrator import engine

    calls = []

    async def failing(url, body, timeout):
        calls.append(url)
        raise ConnectionError("busy")

    monkeypatch.setattr(throughput, "_post_json", failing)
    m = _matrix(OLLAMA)
    await engine._measure_local_speeds(m)            # first run start: probes, fails
    await engine._measure_local_speeds(m)            # second run start: remembered, not probed
    assert len(calls) == 1
    assert throughput.failure_of(OLLAMA.model_id).startswith("ConnectionError")
    # a tick (or Discover) re-measures it, and a success clears the memory
    _fake_probe(monkeypatch, figure=4.0)
    assert await throughput.measure_throughput(m, OLLAMA.model_id) == pytest.approx(4.0)
    assert throughput.failure_of(OLLAMA.model_id) is None


@pytest.mark.asyncio
async def test_a_ticked_local_model_still_without_a_figure_is_measured_at_the_start_of_a_run(monkeypatch):
    from src.orchestrator import engine

    seen = _fake_probe(monkeypatch, figure=5.0)
    m = _matrix(OLLAMA, CLOUD)
    await engine._measure_local_speeds(m)
    assert m.get(OLLAMA.model_id).throughput_tps == pytest.approx(5.0)
    assert seen == ["http://localhost:11434/api/generate"]


# --- ruling 28, round 2: single flight per row, one deadline per model ------------

def test_tick_twice_discover_and_a_run_start_share_one_probe(monkeypatch):
    import uuid
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from main import app, matrix
    from src.orchestrator import engine

    gate, seen = _gated_probe(monkeypatch, figure=6.0)
    events = []

    async def write_event(event_type, **kw):
        events.append((event_type, kw))

    monkeypatch.setattr(engine, "write_event", write_event)

    async def no_new_rows(mx, openrouter_key=None):
        return 0

    mid = "ollama/flight-test:1b"
    with TestClient(app) as client, patch("src.api.models.discover_all", new=no_new_rows):
        matrix.upsert(_row(mid, "ollama", "http://localhost:11434", checked=False))
        assert client.patch(f"/models/{mid}/checked", json={"checked": True}).status_code == 200
        assert client.patch(f"/models/{mid}/checked", json={"checked": True}).status_code == 200
        assert client.post("/models/discover").status_code == 200
        run_start = client.portal.start_task_soon(engine._measure_local_speeds, matrix, uuid.uuid4(), None)
        assert _wait_for(lambda: len(seen) >= 1)
        import time as _t
        _t.sleep(0.3)                                             # give any second probe a chance to start
        assert len(seen) == 1
        assert _row_of(client, mid)["speed_status"] == "measuring"
        assert not run_start.done()                               # the run start waits for the one probe
        gate.set()
        run_start.result(timeout=20)
        assert matrix.get(mid).throughput_tps == pytest.approx(6.0)
        assert _wait_for(lambda: _row_of(client, mid)["speed_status"] is None)
    assert len(seen) == 1
    assert not any((kw.get("decision") or {}).get("code") == "BRT-REG-016" for _t2, kw in events)


@pytest.mark.asyncio
async def test_a_waiter_with_another_matrix_object_stores_the_shared_figure(monkeypatch):
    """Lead's final check of 209f1c9: a run start holding its own matrix waits for a
    tick's probe and keeps the figure, so its next run does not probe again."""
    import asyncio

    app_matrix, run_matrix = _matrix(OLLAMA), _matrix(OLLAMA)
    gate = asyncio.Event()
    calls = []

    async def slow(url, body, timeout):
        calls.append(url)
        await gate.wait()
        return {"eval_count": 32, "eval_duration": 4_000_000_000}

    monkeypatch.setattr(throughput, "_post_json", slow)
    owner = asyncio.ensure_future(throughput.measure_throughput(app_matrix, OLLAMA.model_id))
    await asyncio.sleep(0)
    waiter = asyncio.ensure_future(throughput.measure_throughput(run_matrix, OLLAMA.model_id))
    await asyncio.sleep(0)
    gate.set()
    assert await owner == await waiter == 8.0
    assert len(calls) == 1
    assert run_matrix.get(OLLAMA.model_id).throughput_tps == 8.0
    assert not throughput.needs_measuring(run_matrix.get(OLLAMA.model_id))


@pytest.mark.asyncio
async def test_a_failure_never_overwrites_a_figure_stored_meanwhile(monkeypatch):
    m = _matrix(OLLAMA)

    async def fails_after_a_figure_arrives(url, body, timeout):
        m.update_throughput(OLLAMA.model_id, 9.0)                 # e.g. a hand edit or another source
        raise ConnectionError("busy")

    monkeypatch.setattr(throughput, "_post_json", fails_after_a_figure_arrives)
    assert await throughput.measure_throughput(m, OLLAMA.model_id) == 9.0
    assert m.get(OLLAMA.model_id).throughput_tps == 9.0
    assert throughput.failure_of(OLLAMA.model_id) is None


@pytest.mark.asyncio
async def test_the_whole_probe_of_one_model_has_one_deadline(monkeypatch, caplog):
    import asyncio

    monkeypatch.setattr(throughput, "PROBE_TIMEOUT_S", 0.5)
    calls = []

    async def slow(url, body, timeout):                          # each call takes most of the limit
        calls.append(url)
        await asyncio.sleep(0.35)
        return {"usage": {"completion_tokens": 30}}

    monkeypatch.setattr(throughput, "_post_json", slow)
    m = _matrix(LMSTUDIO)
    with caplog.at_level(logging.WARNING):
        assert await throughput.measure_throughput(m, LMSTUDIO.model_id) is None
    assert len(calls) == 2                                       # the second call was cut off by the deadline
    assert m.get(LMSTUDIO.model_id).throughput_tps is None
    assert "BRT-REG-016" in caplog.text and "TimeoutError" in throughput.failure_of(LMSTUDIO.model_id)
