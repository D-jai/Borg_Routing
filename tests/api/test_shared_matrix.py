"""R2: the API and the engine see one capability matrix."""
import subprocess
import sys
from unittest.mock import AsyncMock, patch

import pytest
import yaml
from fastapi.testclient import TestClient

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing


def _row(model_id: str, checked: bool = True) -> ModelInfo:
    return ModelInfo(
        model_id=model_id, source="openrouter", checked=checked, context_length=8000,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
        task_scores={"reasoning": 7.0},
    )


def test_app_and_engine_use_the_same_matrix_object():
    code = (
        "import main\n"
        "from src.orchestrator import engine\n"
        "assert engine._get_matrix() is main.matrix\n"
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


@pytest.fixture
def api(tmp_path, monkeypatch):
    from main import app
    from src.api.models import get_matrix
    from src.orchestrator import engine

    path = tmp_path / "matrix.yaml"
    m = CapabilityMatrix(str(path))
    m.upsert(_row("a/old"))
    m.save()
    monkeypatch.setattr(engine, "_shared_matrix", None)
    app.dependency_overrides[get_matrix] = lambda: m
    yield TestClient(app), m, engine
    app.dependency_overrides.pop(get_matrix, None)


def test_discover_is_seen_by_the_engine_without_a_restart(api):
    client, m, engine = api
    engine._get_matrix()  # engine has already loaded its own copy

    async def fake_discover(matrix, openrouter_key=None):
        matrix.upsert(_row("a/new"))
        return 1

    with patch("src.api.models.discover_all", new=fake_discover):
        assert client.post("/models/discover").status_code == 200
    assert engine._get_matrix().get("a/new") is not None


def test_get_model_info_is_seen_by_the_engine(api):
    client, m, engine = api
    engine._get_matrix()

    async def fake_research(matrix, ids, **kw):
        row = matrix.get("a/old")
        matrix.upsert(row.model_copy(update={"task_scores": {"reasoning": 9.5}}))
        return {"a/old": {}}

    with patch("src.api.models.research_and_apply", new=fake_research):
        assert client.post("/models/research", json={"model_ids": ["a/old"]}).status_code == 200
    assert engine._get_matrix().get("a/old").task_scores["reasoning"] == 9.5


def test_checkbox_change_is_seen_by_the_engine(api):
    client, m, engine = api
    engine._get_matrix()
    r = client.patch("/models/a/old/checked", json={"checked": False})
    assert r.status_code == 200
    assert engine._get_matrix().get("a/old").checked is False
