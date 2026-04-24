"""
TC-COLD-010 — Capability matrix seed has Sonnet 4.6 + Kimi K2 Thinking
              pre-checked (cold-start orchestrator defaults). Both meet
              the 9.0 reasoning bar.
TC-COLD-011 — _select_orchestrator_model picks one of the cold-start
              models on a fresh matrix at quality_requirement=9.0.
TC-COLD-012 — When no checked model meets the bar, _select_orchestrator_model
              degrades to 5.1 AND emits a BRT-ORC-NO-FRONTIER trace event.
"""
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo


# ---------- TC-COLD-010 — seed file has the right pre-checks ----------

def test_capability_matrix_seed_has_two_cold_start_defaults():
    """Sonnet 4.6 and Kimi K2 Thinking ship with checked=True; everyone else
    ships with checked=False. Reasoning >= 9.0 for both."""
    seed_path = Path("data/capability_matrix.yaml")
    raw = yaml.safe_load(seed_path.read_text(encoding="utf-8"))
    by_id = {row["model_id"]: row for row in raw}

    sonnet = by_id["openrouter/anthropic/claude-sonnet-4.6"]
    kimi = by_id["openrouter/moonshotai/kimi-k2-thinking"]

    assert sonnet["checked"] is True, "Sonnet 4.6 must ship pre-checked"
    assert kimi["checked"] is True, "Kimi K2 Thinking must ship pre-checked"
    assert sonnet["task_scores"]["reasoning"] >= 9.0
    assert kimi["task_scores"]["reasoning"] >= 9.0

    # Tag both rows so callers can find them programmatically.
    assert "cold_start_default" in sonnet["specialty_tags"]
    assert "cold_start_default" in kimi["specialty_tags"]

    # And every other OpenRouter row is NOT pre-checked. Otherwise we'd be
    # quietly handing the router 6+ frontier models and a fresh install
    # could spend heavily before the operator notices.
    for mid, row in by_id.items():
        if mid in {"openrouter/anthropic/claude-sonnet-4.6",
                   "openrouter/moonshotai/kimi-k2-thinking"}:
            continue
        if row.get("source") == "openrouter":
            assert row["checked"] is False, (
                f"{mid} must ship un-checked; only the two cold-start "
                f"defaults may be pre-checked."
            )


# ---------- TC-COLD-011 — selection at 9.0 picks a cold-start model ----------

@pytest.mark.asyncio
async def test_select_orchestrator_picks_cold_start_default_at_9(monkeypatch, tmp_path):
    """With only the two cold-start rows checked, _select_orchestrator_model
    at quality_requirement=9.0 picks one of them (router's choice, but
    must be either Sonnet 4.6 or Kimi K2 Thinking)."""
    from src.orchestrator import engine as engine_mod

    matrix = CapabilityMatrix()
    matrix.upsert(ModelInfo(
        model_id="openrouter/anthropic/claude-sonnet-4.6",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=1_000_000,
        task_scores={"reasoning": 9.0},
        specialty_tags=["mid_flagship", "cold_start_default"],
        checked=True,
    ))
    matrix.upsert(ModelInfo(
        model_id="openrouter/moonshotai/kimi-k2-thinking",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=262144,
        task_scores={"reasoning": 9.2},
        specialty_tags=["reasoning_trace", "cold_start_default"],
        checked=True,
    ))

    monkeypatch.setattr(engine_mod, "_get_matrix", lambda: matrix)

    # Use a tmp DB so config reads return None.
    from src.db.schema import create_all
    db = str(tmp_path / "cold.db")
    await create_all(db)

    picked = await engine_mod._select_orchestrator_model(
        "reasoning", db_path=db, quality_requirement=9.0
    )
    assert picked.model_id in {
        "openrouter/anthropic/claude-sonnet-4.6",
        "openrouter/moonshotai/kimi-k2-thinking",
    }


# ---------- TC-COLD-012 — degrade emits BRT-ORC-NO-FRONTIER ----------

@pytest.mark.asyncio
async def test_select_orchestrator_emits_no_frontier_event_on_degrade(monkeypatch, tmp_path):
    """When no checked model meets the 9.0 bar, the function falls back to
    5.1 AND writes a BRT-ORC-NO-FRONTIER trace event to the run timeline."""
    from src.orchestrator import engine as engine_mod
    import uuid as _uuid
    import aiosqlite

    matrix = CapabilityMatrix()
    # One mediocre row (reasoning=6.5) — meets 5.1 but not 9.0.
    matrix.upsert(ModelInfo(
        model_id="openrouter/some/mediocre-model",
        source="openrouter",
        endpoint="https://openrouter.ai/api",
        context_length=128000,
        task_scores={"reasoning": 6.5},
        specialty_tags=["mid"],
        checked=True,
    ))
    monkeypatch.setattr(engine_mod, "_get_matrix", lambda: matrix)

    from src.db.schema import create_all
    db = str(tmp_path / "degrade.db")
    await create_all(db)

    run_id = _uuid.uuid4()
    # The run row needs to exist for write_event to FK-link successfully.
    from src.db.state import create_run
    persisted_run_id = await create_run("test instruction", db_path=db)

    picked = await engine_mod._select_orchestrator_model(
        "reasoning", db_path=db, quality_requirement=9.0, run_id=persisted_run_id
    )
    assert picked.model_id == "openrouter/some/mediocre-model"

    # Verify an 'error'-type event with code BRT-ORC-NO-FRONTIER was written.
    # write_event is fire-and-forget via asyncio.create_task; give it a
    # tick to settle before reading the DB.
    import asyncio as _asyncio
    await _asyncio.sleep(0.05)

    async with aiosqlite.connect(db) as conn:
        conn.row_factory = aiosqlite.Row
        async with conn.execute(
            "SELECT decision_json FROM logs WHERE run_id = ? AND event_type = 'error'",
            (str(persisted_run_id),),
        ) as cur:
            rows = await cur.fetchall()

    assert rows, "expected an 'error' event to be written on degrade"
    import json as _json
    decisions = [_json.loads(r["decision_json"]) for r in rows]
    codes = [d.get("code") for d in decisions]
    assert "BRT-ORC-NO-FRONTIER" in codes
    # Severity tag distinguishes this from hard-fail errors.
    matching = [d for d in decisions if d.get("code") == "BRT-ORC-NO-FRONTIER"]
    assert matching[0].get("severity") == "warning"


# ---------- TC-COLD-013 — admin defaults reflect the new floor ----------

def test_admin_orchestrator_quality_default_is_nine():
    """The Admin UI's read-only display of orchestrator_quality_requirement
    defaults to 9.0 when neither DB nor env sets it."""
    from src.api.admin import _EFFECTIVE_SETTINGS

    by_key = {row["key"]: row for row in _EFFECTIVE_SETTINGS}
    assert by_key["orchestrator_quality_requirement"]["default"] == 9.0
