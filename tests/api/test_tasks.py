"""Tests for src/api/tasks.py.

TC-API-030 — POST /v1/task returns 202 with run_id
TC-API-031 — BRT-API-020 returned for empty instruction
TC-API-032 — GET /runs/{id} returns run state after completion
TC-API-033 — GET /runs/{id} returns 404 for unknown run_id
TC-API-034 — GET /runs/{id} returns 400 for invalid UUID format
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import DB_PATH, create_all


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def test_submit_task_returns_202(client):
    """TC-API-030 — POST /v1/task returns 202 with run_id."""
    async def mock_background(*args, **kwargs):
        pass

    with patch("src.api.tasks._run_in_background", new=mock_background):
        resp = client.post("/v1/task", json={"instruction": "Classify these issues."})

    assert resp.status_code == 202
    body = resp.json()
    assert "run_id" in body
    assert body["status"] == "pending"
    # Validate run_id is a valid UUID
    uuid.UUID(body["run_id"])


def test_submit_task_empty_instruction(client):
    """TC-API-031 — BRT-API-020 for empty instruction."""
    resp = client.post("/v1/task", json={"instruction": "  "})
    assert resp.status_code == 400
    assert "BRT-API-020" in resp.text


def test_get_run_state_after_task(client):
    """TC-API-032 — GET /runs/{id} returns run state."""
    async def mock_background(*args, **kwargs):
        pass

    with patch("src.api.tasks._run_in_background", new=mock_background):
        post_resp = client.post("/v1/task", json={"instruction": "Test instruction."})

    run_id = post_resp.json()["run_id"]

    get_resp = client.get(f"/runs/{run_id}")
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert "run" in body
    assert body["run"]["run_id"] == run_id
    assert body["run"]["user_instruction"] == "Test instruction."
    assert "envelopes" in body
    assert "events" in body


def test_get_run_not_found(client):
    """TC-API-033 — GET /runs/{id} returns 404 for unknown run_id."""
    unknown = str(uuid.uuid4())
    resp = client.get(f"/runs/{unknown}")
    assert resp.status_code == 404
    assert "BRT-API-021" in resp.text


def test_get_run_invalid_uuid(client):
    """TC-API-034 — GET /runs/{id} returns 400 for invalid UUID."""
    resp = client.get("/runs/not-a-uuid")
    assert resp.status_code == 400
    assert "BRT-API-021" in resp.text
