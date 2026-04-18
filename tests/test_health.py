"""TC-API-001 — GET /health returns {"status": "ok"} with HTTP 200.

Tests for the S1-00 health endpoint.
"""
import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app)


def test_health_happy_path(client):
    """TC-API-001 — Happy path: /health returns 200 and {"status": "ok"}."""
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_method_not_allowed(client):
    """TC-API-002 — Failure path: POST /health is not allowed."""
    response = client.post("/health")
    assert response.status_code == 405
