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


@pytest.mark.asyncio
async def test_an_alias_does_not_leak_into_the_next_run_in_the_same_task(tmp_path, monkeypatch):
    db_path = str(tmp_path / "leak.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-alias")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    stub = StubModels(plan_nodes=PLAN).install(monkeypatch)

    first = await run_pipeline("One.", db_path=db_path,
                               route_options={"alias": "borg_route:light", "tier": "light"})
    await drain_log_writes()
    assert first["status"] == "completed", first.get("error")
    assert {r.model for r in stub.of_kind("answerer")} == {"stub/cheap"}
    seen = len(stub.of_kind("answerer"))

    second = await run_pipeline("Two.", db_path=db_path)
    await drain_log_writes()
    assert second["status"] == "completed", second.get("error")
    later = {r.model for r in stub.of_kind("answerer")[seen:]}
    assert later == {"stub/top"}, later


@pytest.mark.asyncio
async def test_an_alias_reaches_the_steps_of_a_child_run(tmp_path, monkeypatch):
    import json
    import sqlite3

    db_path = str(tmp_path / "child_alias.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-alias")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    root, sub = "Root job.", "Sub job for the child run."

    def plan_for(req):
        if req.user == root:
            return [{"type": "orchestrate", "id": "n1", "instruction": sub, "output_schema": {}}]
        return [{"type": "answerer", "id": "c1", "instruction": "Child step text.",
                 "task_type": "extraction", "tier": "heavy", "output_schema": {"result": "string"}}]

    stub = StubModels(plan_nodes=[], plan_for=plan_for).install(monkeypatch)
    result = await run_pipeline(root, db_path=db_path,
                                route_options={"alias": "borg_route:light", "tier": "light"})
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    child_step = [r for r in stub.of_kind("answerer") if "Child step text." in r.system]
    assert child_step and {r.model for r in child_step} == {"stub/cheap"}
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT run_id, decision_json FROM logs WHERE event_type='orchestrator_decision' "
            "AND run_id != ?", (result["run_id"],)).fetchall()
    decisions = [json.loads(d) for _r, d in rows if d]
    routed = [d for d in decisions if d.get("alias")]
    assert routed, decisions
    assert all(d["alias"] == "borg_route:light" for d in routed)
    assert any("borg_route:light" in " ".join(d.get("notes", [])) for d in routed)
