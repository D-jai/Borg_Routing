"""Orchestrated answers stream in chunks that keep every character."""
import json

import pytest

from src.api.chat_completions import _stream_response


async def _collect(text: str) -> str:
    out = ""
    async for line in _stream_response(text, "borg_route", "run-1"):
        payload = line.removeprefix("data: ").strip()
        if not payload or payload == "[DONE]":
            continue
        out += json.loads(payload)["choices"][0]["delta"].get("content", "")
    return out


@pytest.mark.asyncio
@pytest.mark.parametrize("text", [
    "## Title\n\n- a\n- b\n\n```python\ndef f():\n    return 1\n```\n",
    "  leading and trailing  \n",
    "one",
    "tabs\tand   runs of   spaces",
])
async def test_stream_preserves_every_character(text):
    assert await _collect(text) == text


@pytest.mark.asyncio
async def test_stream_of_empty_text_is_just_the_end_markers():
    assert await _collect("") == ""


def test_endpoint_stream_returns_the_exact_answer(monkeypatch):
    """Through the HTTP endpoint (pipeline stubbed, database in a temp file)."""
    from fastapi.testclient import TestClient

    from main import app

    answer = "## Result\n\n- one\n- two\n\n```python\nx = 1\n```\n"

    async def fake_pipeline(user_instruction, thread_window=None, **kwargs):
        return {"run_id": "rid-1", "response": answer, "status": "completed",
                "latency_ms": 1, "envelope_count": 0, "error": None}

    monkeypatch.setattr("src.api.chat_completions.run_pipeline", fake_pipeline)

    async def noop_db(*a, **k):
        return None

    monkeypatch.setattr("src.api.chat_completions._ensure_db", noop_db)
    client = TestClient(app)
    resp = client.post("/v1/chat/completions", json={
        "model": "borg_route", "stream": True,
        "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    got = ""
    for line in resp.text.splitlines():
        if line.startswith("data: ") and "[DONE]" not in line:
            got += json.loads(line[6:])["choices"][0]["delta"].get("content", "")
    assert got == answer
