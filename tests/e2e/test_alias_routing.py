"""R5 end to end: the alias changes which model serves the steps."""
import pytest

from src.db.schema import create_all
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

PLAN = [
    {"type": "answerer", "id": "n1", "instruction": "Step one text.",
     "task_type": "extraction", "tier": "heavy", "output_schema": {"result": "string"}},
    {"type": "answerer", "id": "n2", "instruction": "Step two text.",
     "task_type": "reasoning", "tier": "heavy", "output_schema": {"result": "string"}},
]


async def _run(tmp_path, monkeypatch, route_options):
    db_path = str(tmp_path / "alias.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-alias")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    stub = StubModels(plan_nodes=PLAN).install(monkeypatch)
    result = await run_pipeline("Do two things.", db_path=db_path, route_options=route_options)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    return stub


@pytest.mark.asyncio
async def test_light_alias_sends_planner_heavy_steps_to_the_cheap_model(tmp_path, monkeypatch):
    stub = await _run(tmp_path, monkeypatch, {"alias": "borg_route:light", "tier": "light"})
    assert {r.model for r in stub.of_kind("answerer")} == {"stub/cheap"}


@pytest.mark.asyncio
async def test_without_an_alias_the_planners_tier_stands(tmp_path, monkeypatch):
    stub = await _run(tmp_path, monkeypatch, None)
    assert {r.model for r in stub.of_kind("answerer")} == {"stub/top"}
