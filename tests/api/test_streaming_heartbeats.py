"""Tests for BRT-STR-01 — streaming heartbeats during orchestration.

TC-STR-001 — a long-running pipeline emits periodic ': heartbeat ...'
             SSE comments before the final content, so browsers don't
             time out their fetch.
TC-STR-002 — a fast-completing pipeline may emit zero heartbeats; the
             real word-stream + [DONE] sentinel still arrive.
"""
import asyncio

import pytest

from src.api.chat_completions import _stream_orchestration_with_heartbeats


def _slow_pipeline_factory(sleep_sec: float, response_text: str = "fin."):
    """Build a mock run_pipeline coroutine that sleeps then resolves."""
    async def _mock(user_instruction, thread_window=None, **kwargs):
        await asyncio.sleep(sleep_sec)
        return {
            "run_id": "rid-slow",
            "response": response_text,
            "status": "completed",
            "latency_ms": int(sleep_sec * 1000),
            "envelope_count": 0,
            "error": None,
        }
    return _mock


async def _collect(agen):
    out = []
    async for chunk in agen:
        out.append(chunk)
    return out


@pytest.mark.asyncio
async def test_long_pipeline_emits_heartbeats(monkeypatch):
    """TC-STR-001 — a 3s pipeline with heartbeat_every=0.5 emits at least
    two ': heartbeat' comment lines before the word-stream + [DONE]."""
    monkeypatch.setenv("BORG_STREAM_HEARTBEAT_SEC", "0.5")
    monkeypatch.setattr(
        "src.api.chat_completions.run_pipeline",
        _slow_pipeline_factory(sleep_sec=3.0, response_text="hello world"),
    )

    chunks = await _collect(
        _stream_orchestration_with_heartbeats(
            user_instruction="hi",
            thread_window=[],
            model_alias="borg_route",
        )
    )

    heartbeats = [c for c in chunks if c.startswith(": heartbeat")]
    assert len(heartbeats) >= 2, f"expected >= 2 heartbeats, got {heartbeats}"

    data_chunks = [c for c in chunks if c.startswith("data:")]
    assert any("[DONE]" in c for c in data_chunks), \
        "missing final [DONE] sentinel"
    # The response text should appear in at least one chunk's delta.content.
    joined = "".join(data_chunks)
    assert "hello" in joined and "world" in joined


@pytest.mark.asyncio
async def test_fast_pipeline_needs_no_heartbeats(monkeypatch):
    """TC-STR-002 — a fast pipeline may emit zero heartbeats; the real
    word-stream and [DONE] sentinel still arrive correctly."""
    monkeypatch.setenv("BORG_STREAM_HEARTBEAT_SEC", "5")
    monkeypatch.setattr(
        "src.api.chat_completions.run_pipeline",
        _slow_pipeline_factory(sleep_sec=0.0, response_text="ok"),
    )

    chunks = await _collect(
        _stream_orchestration_with_heartbeats(
            user_instruction="hi",
            thread_window=[],
            model_alias="borg_route",
        )
    )

    heartbeats = [c for c in chunks if c.startswith(": heartbeat")]
    assert len(heartbeats) <= 1, f"unexpected heartbeats on fast run: {heartbeats}"

    data_chunks = [c for c in chunks if c.startswith("data:")]
    assert any("[DONE]" in c for c in data_chunks)
    joined = "".join(data_chunks)
    assert "ok" in joined
