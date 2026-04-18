"""Tests for src/pipeline/runner.py.

TC-PIP-001 — run_pipeline returns completed dict with run_id and response
TC-PIP-002 — BRT-PIP-004 returned when user_instruction is empty
TC-PIP-003 — run record status=completed after successful run
TC-PIP-004 — BRT-PIP-002 captured in error field when orchestration fails
TC-PIP-005 — run status=failed in DB when orchestration raises
"""
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from src.db.schema import create_all
from src.db.state import get_run
from src.pipeline.runner import run_pipeline


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "runner_test.db")
    await create_all(path)
    return path


@pytest.mark.asyncio
async def test_successful_run(db):
    """TC-PIP-001 — run_pipeline returns completed dict."""
    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(return_value="Final answer.")):
        result = await run_pipeline("Classify these issues.", db_path=db)

    assert result["status"] == "completed"
    assert result["response"] == "Final answer."
    assert result["run_id"] is not None
    assert result["error"] is None
    assert isinstance(result["latency_ms"], int)


@pytest.mark.asyncio
async def test_empty_instruction_returns_error(db):
    """TC-PIP-002 — BRT-PIP-004 for empty instruction."""
    result = await run_pipeline("  ", db_path=db)
    assert result["status"] == "failed"
    assert "BRT-PIP-004" in result["error"]
    assert result["run_id"] is None


@pytest.mark.asyncio
async def test_run_status_completed_in_db(db):
    """TC-PIP-003 — run record status=completed after successful run."""
    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(return_value="Done.")):
        result = await run_pipeline("Test instruction", db_path=db)

    run_id = uuid.UUID(result["run_id"])
    run = await get_run(run_id, db_path=db)
    assert run["status"] == "completed"


@pytest.mark.asyncio
async def test_orchestration_failure_captured(db):
    """TC-PIP-004 — BRT-PIP-002 in error field when orchestration raises."""
    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=ValueError("BRT-ORC-007: no model"))):
        result = await run_pipeline("Test", db_path=db)

    assert result["status"] == "failed"
    assert "BRT-PIP-002" in result["error"]
    assert "no model" in result["error"]


@pytest.mark.asyncio
async def test_run_status_failed_in_db_when_orchestration_raises(db):
    """TC-PIP-005 — run record status=failed after orchestration error."""
    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=RuntimeError("crash"))):
        result = await run_pipeline("Test", db_path=db)

    run_id = uuid.UUID(result["run_id"])
    run = await get_run(run_id, db_path=db)
    assert run["status"] == "failed"
