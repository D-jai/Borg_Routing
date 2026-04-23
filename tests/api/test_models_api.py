"""Tests for src/api/models.py endpoints.

TC-API-010 — GET /models returns a list
TC-API-011 — GET /models/matrix returns the full matrix as JSON array
TC-API-012 — POST /models/discover returns { upserted: int }
TC-API-014 — POST /keys saves provider key variables
TC-API-015 — POST /keys returns 400 BRT-API-001 on missing name/api_key
TC-API-020 — PATCH /models/{id}/checked flips the flag
TC-API-021 — PATCH /models/{id}/checked returns 404 BRT-API-002 on unknown id
TC-API-022 — POST /models/research applies scores to the matrix
TC-API-023 — POST /models/research returns 502 when scorer LLM fails
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
    assert all("checked" in m for m in data)


def test_discover_returns_upserted_count(client):
    """TC-API-012 — POST /models/discover returns upserted count."""
    with patch("src.api.models.discover_all", new_callable=AsyncMock) as mock_disc:
        mock_disc.return_value = 5
        resp = client.post("/models/discover")

    assert resp.status_code == 200
    data = resp.json()
    assert "upserted" in data
    assert data["upserted"] == 5


def test_patch_checked_flips_flag(client):
    """TC-API-020 — PATCH /models/{id}/checked updates the row."""
    target_id = matrix.all()[0].model_id
    assert matrix.get(target_id).checked is False

    resp = client.patch(
        f"/models/{target_id}/checked",
        json={"checked": True},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["model_id"] == target_id
    assert body["checked"] is True
    assert matrix.get(target_id).checked is True

    # Flip back
    resp2 = client.patch(f"/models/{target_id}/checked", json={"checked": False})
    assert resp2.status_code == 200
    assert resp2.json()["checked"] is False


def test_patch_checked_unknown_returns_404(client):
    """TC-API-021 — unknown model_id returns 404 BRT-API-002."""
    resp = client.patch(
        "/models/does-not/exist/checked",
        json={"checked": True},
    )
    assert resp.status_code == 404
    assert "BRT-API-002" in resp.json()["detail"]


def test_research_applies_scores(client):
    """TC-API-022 — POST /models/research calls the scorer and persists scores."""
    target_id = matrix.all()[0].model_id
    fake_scores = {
        target_id: {
            "reasoning": 8.5,
            "coding": 7.0,
            "summarize": 8.0,
            "writing": 7.5,
            "roleplay": 6.5,
            "agentic": 8.0,
            "extraction": 8.5,
        }
    }

    async def fake_research_and_apply(matrix_arg, model_ids, **kwargs):
        for mid, scores in fake_scores.items():
            for task_type, score in scores.items():
                matrix_arg.update_score(mid, task_type, score)
        return fake_scores

    with patch(
        "src.api.models.research_and_apply",
        new=fake_research_and_apply,
    ):
        resp = client.post(
            "/models/research",
            json={"model_ids": [target_id]},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert target_id in body["scored"]
    assert matrix.get(target_id).task_scores["coding"] == pytest.approx(7.0)


def test_research_scorer_failure_returns_502(client):
    """TC-API-023 — scorer HTTP failure bubbles up as 502."""
    target_id = matrix.all()[0].model_id

    async def raise_runtime(*a, **kw):
        raise RuntimeError("BRT-REG-030: scoring LLM call failed (test)")

    with patch("src.api.models.research_and_apply", new=raise_runtime):
        resp = client.post(
            "/models/research",
            json={"model_ids": [target_id]},
        )

    assert resp.status_code == 502
    assert "BRT-REG-030" in resp.json()["detail"]


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
