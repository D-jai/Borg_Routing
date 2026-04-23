"""Slice 1 end-to-end test — S1-10.

Asserts the complete Slice 1 contract:
- App starts and responds to /health
- POST /models/discover succeeds (external sources mocked, local skipped if not running)
- Matrix contains at least one openrouter entry after (re-)loading the seed file
- router.select_model("coding") returns a sensible model
- No regressions in unit tests (run via pytest from CI; this file does E2E only)
"""
import pytest
from fastapi.testclient import TestClient
from unittest.mock import AsyncMock, patch

from main import app, matrix
from src.registry.router import select_model


@pytest.fixture(autouse=True)
def reload_matrix():
    """Ensure matrix is loaded from seed file before each test.

    Every row is flipped to checked=True so the router has a pool to pick
    from — the on-disk seed defaults to False (the operator curates via UI).
    """
    matrix.load()
    for m in matrix.all():
        matrix.set_checked(m.model_id, True)
    yield


@pytest.fixture
def client():
    return TestClient(app)


# ---------------------------------------------------------------------------
# App starts
# ---------------------------------------------------------------------------

def test_app_health(client):
    """S1-10-01 — App starts and /health returns 200."""
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# POST /models/discover
# ---------------------------------------------------------------------------

def test_discover_endpoint_succeeds(client):
    """S1-10-02 — POST /models/discover returns 200 with upserted count."""
    with patch("src.api.models.discover_all", new_callable=AsyncMock) as mock_disc:
        mock_disc.return_value = 3
        resp = client.post("/models/discover")
    assert resp.status_code == 200
    data = resp.json()
    assert "upserted" in data
    assert isinstance(data["upserted"], int)


# ---------------------------------------------------------------------------
# Matrix contains expected entries
# ---------------------------------------------------------------------------

def test_matrix_contains_local_and_openrouter():
    """S1-10-03 — Seed matrix has ≥1 local model and ≥1 OpenRouter model."""
    all_models = matrix.all()
    locals_ = [m for m in all_models if m.source in ("lmstudio", "ollama")]
    externals = [m for m in all_models if m.source == "openrouter"]
    assert len(locals_) >= 1, "Expected at least one local model in seed matrix"
    assert len(externals) >= 1, "Expected at least one OpenRouter model in seed matrix"


# ---------------------------------------------------------------------------
# Router returns a sensible model
# ---------------------------------------------------------------------------

def test_router_coding_returns_sensible_model():
    """S1-10-04 — router.select_model('coding') returns a valid ModelInfo."""
    model = select_model(matrix, task_type="coding", quality_requirement=6.0)
    assert model.model_id, "Expected a non-empty model_id"
    assert model.task_scores.get("coding", 0) >= 6.0
    assert model.source in ("lmstudio", "ollama", "openrouter", "custom_key")


def test_router_local_first_strategy():
    """S1-10-05 — LOCAL_FIRST strategy returns a local model for a task any local can handle."""
    model = select_model(
        matrix,
        task_type="summarize",
        quality_requirement=5.0,
        strategy="LOCAL_FIRST",
    )
    assert model.source in ("lmstudio", "ollama"), (
        f"Expected local source but got '{model.source}'"
    )


# ---------------------------------------------------------------------------
# GET /models endpoint
# ---------------------------------------------------------------------------

def test_get_models_endpoint(client):
    """S1-10-06 — GET /models returns all matrix models as JSON."""
    resp = client.get("/models")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) == len(matrix.all())
    assert all("model_id" in m for m in data)


# ---------------------------------------------------------------------------
# GET /models/matrix endpoint
# ---------------------------------------------------------------------------

def test_get_matrix_endpoint(client):
    """S1-10-07 — GET /models/matrix returns full matrix JSON."""
    resp = client.get("/models/matrix")
    assert resp.status_code == 200
    data = resp.json()
    assert isinstance(data, list)
    assert len(data) >= 19  # seed has 7 local + 12 openrouter = 19
