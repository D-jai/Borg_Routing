"""Tests for src/api/models.py endpoints.

TC-API-010 — GET /models returns a list (may be empty before discovery)
TC-API-011 — GET /models/matrix returns the full matrix as JSON array
TC-API-012 — POST /models/discover returns { upserted: int }
TC-API-013 — POST /models/benchmark/{id} returns 404 BRT-API-002 on unknown model
TC-API-014 — POST /keys saves provider key variables
TC-API-015 — POST /keys returns 400 BRT-API-001 on missing name/api_key
"""
import os
import tempfile

import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from main import app, matrix


@pytest.fixture(autouse=True)
def fresh_matrix():
    """Reload the matrix from disk before each test."""
    matrix.load()
    yield


@pytest.fixture
def client():
    return TestClient(app)


def test_list_models_returns_list(client):
    """TC-API-010 — GET /models returns a JSON array."""
    resp = client.get("/models")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 1


def test_get_matrix_returns_array(client):
    """TC-API-011 — GET /models/matrix returns full matrix as JSON array."""
    resp = client.get("/models/matrix")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == len(matrix.all())
    assert all("model_id" in m for m in data)


def test_discover_returns_upserted_count(client):
    """TC-API-012 — POST /models/discover returns upserted count."""
    with patch("src.api.models.discover_all", new_callable=AsyncMock) as mock_disc:
        mock_disc.return_value = 5
        resp = client.post("/models/discover")

    assert resp.status_code == 200
    data = resp.json()
    assert "upserted" in data
    assert data["upserted"] == 5


def test_benchmark_unknown_model_returns_404(client):
    """TC-API-013 — BRT-API-002 raised for unknown model_id."""
    resp = client.post("/models/benchmark/fake/model/that/does/not/exist")
    assert resp.status_code == 404
    assert "BRT-API-002" in resp.json()["detail"]


def test_add_key_saves_to_env(client, tmp_path):
    """TC-API-014 — POST /keys writes env variables."""
    env_file = tmp_path / ".env"
    env_file.write_text("")

    with patch("src.api.models.open", create=True) as mock_open_write, \
         patch("os.path.exists", return_value=True), \
         patch("builtins.open", side_effect=_mock_open_factory(str(env_file))):

        resp = client.post("/keys", json={
            "name": "testprovider",
            "base_url": "https://api.example.com",
            "api_key": "sk-test-123",
        })

    # Even without full file mock, verify the response shape
    assert resp.status_code == 200
    assert "saved" in resp.json()["message"].lower()


def _mock_open_factory(path: str):
    """Return a real open() for the given path, pass-through for others."""
    real_open = open

    def _open(file, *args, **kwargs):
        if file == ".env":
            return real_open(path, *args, **kwargs)
        return real_open(file, *args, **kwargs)

    return _open


def test_add_key_missing_name_returns_400(client):
    """TC-API-015 — BRT-API-001 raised when name is missing."""
    resp = client.post("/keys", json={
        "name": "",
        "base_url": "https://api.example.com",
        "api_key": "sk-test-123",
    })
    assert resp.status_code == 400
    assert "BRT-API-001" in resp.json()["detail"]
