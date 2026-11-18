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
    post = _Recorder([{"usage": {"completion_tokens": 32}}, {"usage": {"completion_tokens": 30}}])
    ticks = iter([100.0, 102.0])
    m = _matrix(LMSTUDIO)
    tps = await throughput.measure_throughput(m, LMSTUDIO.model_id, post=post, clock=lambda: next(ticks))
    assert tps == pytest.approx(15.0)                   # second call: 30 tokens in 2 s
    assert len(post.calls) == 2
    assert post.calls[1][0] == "http://localhost:1234/v1/chat/completions"


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


def test_discover_measures_a_newly_found_local_model(monkeypatch):
    from unittest.mock import patch

    from fastapi.testclient import TestClient

    from main import app, matrix

    seen = _fake_probe(monkeypatch)

    async def fake_discover(mx, openrouter_key=None):
        mx.upsert(_row("ollama/probe-test:1b", "ollama", "http://localhost:11434", checked=False))
        return 1

    with patch("src.api.models.discover_all", new=fake_discover):
        resp = TestClient(app).post("/models/discover")
    assert resp.status_code == 200
    assert matrix.get("ollama/probe-test:1b").throughput_tps == pytest.approx(21.5)
    assert seen == ["http://localhost:11434/api/generate"]


def test_ticking_an_unmeasured_local_model_measures_it(monkeypatch):
    from fastapi.testclient import TestClient

    from main import app, matrix

    seen = _fake_probe(monkeypatch, figure=7.0)
    matrix.upsert(_row("ollama/tick-test:1b", "ollama", "http://localhost:11434", checked=False))
    resp = TestClient(app).patch("/models/ollama/tick-test:1b/checked", json={"checked": True})
    assert resp.status_code == 200
    assert matrix.get("ollama/tick-test:1b").throughput_tps == pytest.approx(7.0)
    assert len(seen) == 1
    # unticking calls nothing
    TestClient(app).patch("/models/ollama/tick-test:1b/checked", json={"checked": False})
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_a_ticked_local_model_still_without_a_figure_is_measured_at_the_start_of_a_run(monkeypatch):
    from src.orchestrator import engine

    seen = _fake_probe(monkeypatch, figure=5.0)
    m = _matrix(OLLAMA, CLOUD)
    await engine._measure_local_speeds(m)
    assert m.get(OLLAMA.model_id).throughput_tps == pytest.approx(5.0)
    assert seen == ["http://localhost:11434/api/generate"]
