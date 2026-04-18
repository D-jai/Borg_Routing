"""Slice 6 tests — SSE lifecycle, token streaming.

TC-S6-001 — GET /runs/{id}/stream: 400 for invalid UUID format
TC-S6-002 — GET /runs/{id}/stream: 404 BRT-API-028 for unknown run
TC-S6-003 — GET /runs/{id}/stream: emits run_terminal for completed run
TC-S6-004 — GET /runs/{id}/stream: emits run_terminal with status=failed
TC-S6-005 — GET /runs/{id}/stream: emits envelope_update before terminal
TC-S6-006 — POST /v1/chat/completions stream=true: content-type is text/event-stream
TC-S6-007 — POST /v1/chat/completions stream=true: chunks contain delta content
TC-S6-008 — POST /v1/chat/completions stream=true: final chunk has finish_reason=stop
TC-S6-009 — POST /v1/chat/completions stream=true: last data line is [DONE]
"""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from main import app
from src.db.schema import DB_PATH, create_all
from src.db.state import create_run, get_run, update_run_status, save_envelope
from src.envelopes.schema import WrappedTask


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────

def _sse_lines(text: str) -> list[dict]:
    """Parse all data: lines from an SSE response body."""
    msgs = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if payload == "[DONE]":
            continue
        try:
            msgs.append(json.loads(payload))
        except json.JSONDecodeError:
            pass
    return msgs


async def _stream_text(client: AsyncClient, method: str, url: str, **kwargs) -> str:
    """Collect all bytes from a streaming response."""
    chunks = []
    async with client.stream(method, url, **kwargs) as resp:
        async for chunk in resp.aiter_bytes():
            chunks.append(chunk)
    return b"".join(chunks).decode()


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-001 — invalid UUID format → 400
# ──────────────────────────────────────────────────────────────────────────────

async def test_stream_invalid_uuid():
    """TC-S6-001 — GET /runs/{id}/stream returns 400 for non-UUID id."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/runs/not-a-uuid/stream")
    assert resp.status_code == 400
    assert "BRT-API-028" in resp.text


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-002 — unknown run → 404 BRT-API-028
# ──────────────────────────────────────────────────────────────────────────────

async def test_stream_unknown_run():
    """TC-S6-002 — GET /runs/{id}/stream returns 404 for unknown run_id."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(f"/runs/{uuid.uuid4()}/stream")
    assert resp.status_code == 404
    assert "BRT-API-028" in resp.text


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-003 — completed run → run_terminal event
# ──────────────────────────────────────────────────────────────────────────────

async def test_stream_emits_terminal_for_completed_run():
    """TC-S6-003 — stream closes with run_terminal event when run is completed."""
    run_id = await create_run("stream test completed", db_path=DB_PATH)
    await update_run_status(run_id, "completed", db_path=DB_PATH)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = await _stream_text(client, "GET", f"/runs/{run_id}/stream")

    msgs = _sse_lines(body)
    terminal = next((m for m in msgs if m.get("type") == "run_terminal"), None)
    assert terminal is not None, f"no run_terminal in: {msgs}"
    assert terminal["status"] == "completed"
    assert terminal["run_id"] == str(run_id)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-004 — failed run → run_terminal with status=failed
# ──────────────────────────────────────────────────────────────────────────────

async def test_stream_emits_terminal_for_failed_run():
    """TC-S6-004 — run_terminal event carries status=failed for a failed run."""
    run_id = await create_run("stream test failed", db_path=DB_PATH)
    await update_run_status(run_id, "failed", db_path=DB_PATH)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = await _stream_text(client, "GET", f"/runs/{run_id}/stream")

    msgs = _sse_lines(body)
    terminal = next((m for m in msgs if m.get("type") == "run_terminal"), None)
    assert terminal is not None
    assert terminal["status"] == "failed"


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-005 — run with envelopes → envelope_update events emitted first
# ──────────────────────────────────────────────────────────────────────────────

async def test_stream_emits_envelope_updates_then_terminal():
    """TC-S6-005 — envelope_update events appear before run_terminal."""
    run_id = await create_run("stream test envelopes", db_path=DB_PATH)

    # Seed one envelope
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=run_id,
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=0,
        max_depth=5,
        level_path="0",
        model_name="test-model",
        trigger_reason="test",
        skill_id="general_qa",
        skill_version=1,
        instruction="What is 2+2?",
        output_schema={"answer": "string"},
    )
    await save_envelope(task, db_path=DB_PATH)
    await update_run_status(run_id, "completed", db_path=DB_PATH)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        body = await _stream_text(client, "GET", f"/runs/{run_id}/stream")

    msgs = _sse_lines(body)
    env_updates = [m for m in msgs if m.get("type") == "envelope_update"]
    terminal = next((m for m in msgs if m.get("type") == "run_terminal"), None)

    assert len(env_updates) >= 1, f"expected envelope_update events, got: {msgs}"
    assert terminal is not None
    # envelope_updates come before terminal
    first_terminal_idx = msgs.index(terminal)
    assert all(msgs.index(e) < first_terminal_idx for e in env_updates)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-006 — stream=true returns text/event-stream content type
# ──────────────────────────────────────────────────────────────────────────────

async def test_chat_completions_stream_content_type():
    """TC-S6-006 — stream=true response has text/event-stream content-type."""
    async def mock_pipeline(**kwargs):
        return {"status": "completed", "response": "The answer is 42.", "run_id": str(uuid.uuid4())}

    with patch("src.api.chat_completions.run_pipeline", new=AsyncMock(side_effect=mock_pipeline)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            async with client.stream(
                "POST", "/v1/chat/completions",
                json={"model": "borg_route", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            ) as resp:
                ct = resp.headers.get("content-type", "")
                await resp.aread()

    assert "text/event-stream" in ct


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-007 — SSE chunks contain delta content
# ──────────────────────────────────────────────────────────────────────────────

async def test_chat_completions_stream_delta_content():
    """TC-S6-007 — streaming chunks include delta.content fields with the response text."""
    async def mock_pipeline(**kwargs):
        return {"status": "completed", "response": "Hello world", "run_id": str(uuid.uuid4())}

    with patch("src.api.chat_completions.run_pipeline", new=AsyncMock(side_effect=mock_pipeline)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = await _stream_text(
                client, "POST", "/v1/chat/completions",
                json={"model": "borg_route", "messages": [{"role": "user", "content": "Hi"}], "stream": True},
            )

    msgs = _sse_lines(body)
    delta_texts = [
        m["choices"][0]["delta"].get("content", "")
        for m in msgs
        if m.get("choices") and m["choices"][0].get("delta", {}).get("content")
    ]
    assert len(delta_texts) > 0, "no delta content chunks found"
    full = "".join(delta_texts)
    assert "Hello" in full


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-008 — final chunk has finish_reason=stop
# ──────────────────────────────────────────────────────────────────────────────

async def test_chat_completions_stream_finish_reason_stop():
    """TC-S6-008 — last non-[DONE] chunk carries finish_reason=stop."""
    async def mock_pipeline(**kwargs):
        return {"status": "completed", "response": "Done.", "run_id": str(uuid.uuid4())}

    with patch("src.api.chat_completions.run_pipeline", new=AsyncMock(side_effect=mock_pipeline)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = await _stream_text(
                client, "POST", "/v1/chat/completions",
                json={"model": "borg_route", "messages": [{"role": "user", "content": "Go"}], "stream": True},
            )

    msgs = _sse_lines(body)
    stop_chunks = [
        m for m in msgs
        if m.get("choices") and m["choices"][0].get("finish_reason") == "stop"
    ]
    assert len(stop_chunks) >= 1, f"no finish_reason=stop chunk found in: {msgs}"


# ──────────────────────────────────────────────────────────────────────────────
# TC-S6-009 — last data line is [DONE]
# ──────────────────────────────────────────────────────────────────────────────

async def test_chat_completions_stream_ends_with_done():
    """TC-S6-009 — streaming response ends with data: [DONE]."""
    async def mock_pipeline(**kwargs):
        return {"status": "completed", "response": "Finished.", "run_id": str(uuid.uuid4())}

    with patch("src.api.chat_completions.run_pipeline", new=AsyncMock(side_effect=mock_pipeline)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            body = await _stream_text(
                client, "POST", "/v1/chat/completions",
                json={"model": "borg_route", "messages": [{"role": "user", "content": "End"}], "stream": True},
            )

    data_lines = [l.strip() for l in body.splitlines() if l.strip().startswith("data:")]
    assert data_lines, "no data: lines found"
    assert data_lines[-1] == "data: [DONE]", f"last data line was: {data_lines[-1]}"
