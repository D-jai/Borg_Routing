"""Slice BRT-RT-01 admin wiring tests.

TC-ADM-011 — POST /config time_cost_per_sec=X → next orchestrate_run reads X
              (not the default) when picking a model.
TC-ADM-012 — POST /config orchestrator_quality_requirement=5.0 → routing
              accepts lower-scored rows.
"""
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import DB_PATH, create_all
from src.db.state import create_run, set_config
from src.orchestrator import engine as engine_mod
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def prepped_db(tmp_path):
    # Redirect DB_PATH so admin.py writes config to our tmp DB.
    import src.db.schema as schema
    import src.api.admin as admin_mod

    path = str(tmp_path / "adm_knobs.db")
    await create_all(path)
    original_schema_path = schema.DB_PATH
    original_admin_path = admin_mod.DB_PATH
    schema.DB_PATH = path
    admin_mod.DB_PATH = path
    try:
        yield path
    finally:
        schema.DB_PATH = original_schema_path
        admin_mod.DB_PATH = original_admin_path


def _build_matrix_for_engine(time_sensitive: bool) -> CapabilityMatrix:
    """Two rows with equal quality so the cost tie-break is decisive."""
    cm = CapabilityMatrix()
    cm.upsert(
        ModelInfo(
            model_id="openrouter/fast-ext",
            source="openrouter",
            endpoint="https://openrouter.ai/api",
            context_length=128000,
            pricing=ModelInfo.model_fields["pricing"].default or None,  # placeholder; override below
            throughput_tps=200.0,
            task_scores={"reasoning": 8.5},
            checked=True,
        )
    )
    # Patch pricing after construction to avoid Pydantic default-validation noise.
    from src.registry.models import Pricing
    cm.get("openrouter/fast-ext").pricing = Pricing(
        input_per_token=5e-6, output_per_token=5e-6
    )
    cm.upsert(
        ModelInfo(
            model_id="lmstudio/slow-local",
            source="lmstudio",
            endpoint="http://localhost:1234",
            context_length=32000,
            throughput_tps=20.0,
            task_scores={"reasoning": 8.5},
            checked=True,
        )
    )
    return cm


@pytest.mark.asyncio
async def test_time_cost_post_flows_into_run(prepped_db, client, monkeypatch):
    """TC-ADM-011 — POST /config persists time_cost_per_sec; orchestrate_run reads it."""
    # Operator lowers time-cost to 0 → should prefer LM Studio over OpenRouter.
    r = client.post("/config", json={"key": "time_cost_per_sec", "value": "0"})
    assert r.status_code == 200

    # Also set quality_requirement high enough to require the 8.5 rows.
    client.post(
        "/config",
        json={"key": "orchestrator_quality_requirement", "value": "8.0"},
    )

    matrix = _build_matrix_for_engine(time_sensitive=True)
    monkeypatch.setattr(engine_mod, "_shared_matrix", matrix)
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")

    picked: dict = {}

    async def fake_invoke(**kwargs):
        picked["model_id"] = kwargs["orchestrator_model"].model_id
        # Return a direct_answer so the run short-circuits.
        return {"direct_answer": "ok", "plan": None}

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=fake_invoke):
        run_id = await create_run("hi", db_path=prepped_db)
        await engine_mod.orchestrate_run(run_id, "hi", db_path=prepped_db)

    assert picked["model_id"] == "lmstudio/slow-local"


@pytest.mark.asyncio
async def test_low_quality_requirement_accepts_low_rows(prepped_db, client, monkeypatch):
    """TC-ADM-012 — orchestrator_quality_requirement=5.0 admits a weak row."""
    client.post(
        "/config", json={"key": "orchestrator_quality_requirement", "value": "5.0"}
    )

    matrix = CapabilityMatrix()
    matrix.upsert(
        ModelInfo(
            model_id="lmstudio/weak-local",
            source="lmstudio",
            endpoint="http://localhost:1234",
            context_length=4096,
            throughput_tps=50.0,
            task_scores={"reasoning": 5.2},
            checked=True,
        )
    )
    monkeypatch.setattr(engine_mod, "_shared_matrix", matrix)

    picked: dict = {}

    async def fake_invoke(**kwargs):
        picked["model_id"] = kwargs["orchestrator_model"].model_id
        return {"direct_answer": "ok", "plan": None}

    with patch("src.orchestrator.engine._invoke_orchestrator_model", new=fake_invoke):
        run_id = await create_run("hi", db_path=prepped_db)
        await engine_mod.orchestrate_run(run_id, "hi", db_path=prepped_db)

    assert picked["model_id"] == "lmstudio/weak-local"
