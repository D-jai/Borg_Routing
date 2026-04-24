"""
TC-ADM-020 — GET /config/effective returns each known setting with a source
             tag (db / env / default), reflecting what the engine actually uses.
TC-PIP-008 — Pipeline wall-clock budget (BORG_MAX_RUN_SECONDS): a long run
             gets cancelled at the cap, marked `failed`, and emits BRT-PIP-008.
"""
import asyncio
import os
import uuid
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import create_all
from src.db.state import set_config, get_run


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def prepped_db(tmp_path):
    import src.db.schema as schema
    import src.api.admin as admin_mod

    path = str(tmp_path / "eff_cfg.db")
    await create_all(path)
    original_schema = schema.DB_PATH
    original_admin = admin_mod.DB_PATH
    schema.DB_PATH = path
    admin_mod.DB_PATH = path
    try:
        yield path
    finally:
        schema.DB_PATH = original_schema
        admin_mod.DB_PATH = original_admin


@pytest.mark.asyncio
async def test_effective_config_default_source(client, prepped_db, monkeypatch):
    """TC-ADM-020a — with no DB entry and no env var, source is 'default'."""
    # Clear any env vars that might otherwise dominate.
    for var in ("BORG_MAX_RUN_SECONDS", "TIME_COST_PER_SEC",
                "ORCHESTRATOR_QUALITY_REQUIREMENT"):
        monkeypatch.delenv(var, raising=False)

    r = client.get("/config/effective")
    assert r.status_code == 200
    body = r.json()
    by_key = {s["key"]: s for s in body["settings"]}

    assert by_key["max_run_seconds"]["value"] == 300
    assert by_key["max_run_seconds"]["source"] == "default"
    assert by_key["max_run_seconds"]["env_var"] == "BORG_MAX_RUN_SECONDS"

    assert by_key["time_cost_per_sec"]["value"] == 0.0001
    assert by_key["time_cost_per_sec"]["source"] == "default"


@pytest.mark.asyncio
async def test_effective_config_env_source(client, prepped_db, monkeypatch):
    """TC-ADM-020b — when env var is set and DB is empty, source is 'env'."""
    monkeypatch.setenv("BORG_MAX_RUN_SECONDS", "120")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.005")

    r = client.get("/config/effective")
    body = r.json()
    by_key = {s["key"]: s for s in body["settings"]}

    assert by_key["max_run_seconds"]["value"] == 120
    assert by_key["max_run_seconds"]["source"] == "env"

    assert by_key["time_cost_per_sec"]["value"] == 0.005
    assert by_key["time_cost_per_sec"]["source"] == "env"


@pytest.mark.asyncio
async def test_effective_config_db_source(client, prepped_db, monkeypatch):
    """TC-ADM-020c — DB entry wins over env var; source is 'db'."""
    monkeypatch.setenv("BORG_MAX_RUN_SECONDS", "120")
    await set_config("max_run_seconds", "45", db_path=prepped_db)

    r = client.get("/config/effective")
    body = r.json()
    by_key = {s["key"]: s for s in body["settings"]}

    assert by_key["max_run_seconds"]["value"] == 45
    assert by_key["max_run_seconds"]["source"] == "db"


@pytest.mark.asyncio
async def test_pipeline_wall_clock_cap_fires(prepped_db, monkeypatch):
    """TC-PIP-008 — orchestrate_run exceeding cap returns failed status +
    persists BRT-PIP-008 in failure_reason."""
    from src.pipeline.runner import run_pipeline

    # 1-second cap, then a 5-second orchestrator → timeout must fire.
    monkeypatch.setenv("BORG_MAX_RUN_SECONDS", "1")

    async def _slow(*args, **kwargs):
        await asyncio.sleep(5)
        return "should never arrive"

    with patch("src.pipeline.runner.orchestrate_run", new=_slow):
        result = await run_pipeline(
            user_instruction="long-running test",
            db_path=prepped_db,
        )

    assert result["status"] == "failed"
    assert "BRT-PIP-008" in (result.get("error") or "")

    row = await get_run(uuid.UUID(result["run_id"]), db_path=prepped_db)
    assert row["status"] == "failed"
    assert row.get("failure_reason") and "BRT-PIP-008" in row["failure_reason"]


@pytest.mark.asyncio
async def test_pipeline_wall_clock_cap_does_not_fire_on_fast_run(prepped_db, monkeypatch):
    """TC-PIP-008 — fast runs complete normally under the cap."""
    from src.pipeline.runner import run_pipeline

    monkeypatch.setenv("BORG_MAX_RUN_SECONDS", "30")

    async def _fast(*args, **kwargs):
        return "ok"

    with patch("src.pipeline.runner.orchestrate_run", new=_fast):
        result = await run_pipeline(
            user_instruction="fast test",
            db_path=prepped_db,
        )

    assert result["status"] == "completed"
    assert result["response"] == "ok"
