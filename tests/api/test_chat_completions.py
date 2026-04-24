"""Tests for src/api/chat_completions.py.

TC-API-020 — POST /v1/chat/completions returns OpenAI-shaped response
TC-API-021 — BRT-API-010 returned for malformed request body
TC-API-022 — BRT-API-010 returned when no user message present
TC-API-023 — stream=true returns SSE stream with data: lines
TC-API-024 — pipeline failure returns 200 with error text in content
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _chat_body(content="Hello", model="borg_route", stream=False):
    return {
        "model": model,
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
    }


def _mock_pipeline_success(response_text="This is the answer."):
    async def mock_pipeline(user_instruction, thread_window=None, **kwargs):
        return {
            "run_id": "test-run-id",
            "response": response_text,
            "status": "completed",
            "latency_ms": 100,
            "envelope_count": 1,
            "error": None,
        }
    return mock_pipeline


def test_chat_completions_returns_openai_shape(client):
    """TC-API-020 — POST /v1/chat/completions returns OpenAI-shaped response."""
    with patch("src.api.chat_completions.run_pipeline", new=_mock_pipeline_success()):
        resp = client.post("/v1/chat/completions", json=_chat_body("Tell me something"))

    assert resp.status_code == 200
    body = resp.json()
    assert "choices" in body
    assert body["choices"][0]["message"]["content"] == "This is the answer."
    assert body["choices"][0]["message"]["role"] == "assistant"
    assert "usage" in body
    assert body["object"] == "chat.completion"


def test_malformed_request_returns_400(client):
    """TC-API-021 — BRT-API-010 on malformed body."""
    resp = client.post(
        "/v1/chat/completions",
        content="not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    assert "BRT-API-010" in resp.text


def test_no_user_message_returns_400(client):
    """TC-API-022 — BRT-API-010 when no user message in messages."""
    body = {
        "model": "borg_route",
        "messages": [{"role": "system", "content": "You are helpful."}],
    }
    resp = client.post("/v1/chat/completions", json=body)
    assert resp.status_code == 400


def test_stream_returns_sse(client):
    """TC-API-023 — stream=true returns SSE data: lines."""
    async def mock_pipeline(user_instruction, thread_window=None, **kwargs):
        return {
            "run_id": "stream-run",
            "response": "Streamed answer here.",
            "status": "completed",
            "latency_ms": 50,
            "envelope_count": 0,
            "error": None,
        }

    with patch("src.api.chat_completions.run_pipeline", new=mock_pipeline):
        resp = client.post("/v1/chat/completions", json=_chat_body(stream=True))

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")
    assert "data:" in resp.text
    assert "[DONE]" in resp.text


def test_pipeline_failure_returns_200_with_error_text(client):
    """TC-API-024 — pipeline failure returns 200 with error text in choices."""
    async def mock_pipeline(user_instruction, thread_window=None, **kwargs):
        return {
            "run_id": "failed-run",
            "response": "I was unable to complete this request.",
            "status": "failed",
            "latency_ms": 10,
            "envelope_count": 0,
            "error": "BRT-PIP-002: orchestration failed",
        }

    with patch("src.api.chat_completions.run_pipeline", new=mock_pipeline):
        resp = client.post("/v1/chat/completions", json=_chat_body("Do impossible thing"))

    assert resp.status_code == 200
    body = resp.json()
    # BUG-002 fix: response now carries the real failure reason + run_id so
    # users can find the trace, instead of a generic "unable to complete".
    content = body["choices"][0]["message"]["content"].lower()
    assert "brt-pip-002" in content
    assert "failed-run" in content
