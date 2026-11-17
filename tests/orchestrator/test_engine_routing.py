"""Per-step routing as the engine applies it: skill steps get a model, the
choice is written to the run trace, and a step no model can serve at its
floor falls back visibly (BRT-ORC-019)."""
import json
import sqlite3
import uuid

import pytest

from src.orchestrator import engine
from src.orchestrator.engine import _build_task_from_node, _load_leaf_floors
from src.pipeline.runner import run_pipeline
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.node_router import DEFAULT_FLOORS
from tests.e2e.stub_model_server import (
    StubModels,
    drain_log_writes,
    stub_model,
    three_tier_matrix,
)

INSTRUCTION = "For each of these two notes, do the listed work.\n1. alpha\n2. beta"


@pytest.fixture
async def db(tmp_path):
    from src.db.schema import create_all
    path = str(tmp_path / "routing.db")
    await create_all(path)
    return path


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-routing")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    for name in ("LEAF_QUALITY_LIGHT", "LEAF_QUALITY_MID", "LEAF_QUALITY_HEAVY"):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _decisions(db_path: str, run_id: str, event_type: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        rows = conn.execute(
            "SELECT envelope_id, model_id, decision_json FROM logs "
            "WHERE run_id=? AND event_type=? AND decision_json IS NOT NULL",
            (run_id, event_type),
        ).fetchall()
    return [(e, m, json.loads(d)) for e, m, d in rows]


def test_a_skill_step_is_given_its_routed_model():
    lib = engine._get_skill_lib()
    skill = next(s for s in lib._skills if "claim" in (s.get("required_args") or {}))
    task = _build_task_from_node(
        node={
            "type": "answerer",
            "id": "n1",
            "skill_id": skill["skill_id"],
            "skill_version": skill.get("version", 1),
            "skill_args": {"claim": "The sky is blue.",
                           "evidence_ref": ["The sky looks blue at noon."]},
        },
        run_id=uuid.uuid4(),
        current_depth=1,
        max_depth=5,
        level_path="1a",
        model_endpoint="https://openrouter.ai/api",
        model_name="openrouter/stub/mid",
        user_instruction="Check this claim.",
    )
    assert task.skill_id == skill["skill_id"]
    assert task.model_name == "openrouter/stub/mid"
    assert task.model_endpoint == "https://openrouter.ai/api"


@pytest.mark.asyncio
async def test_each_steps_routing_decision_is_written_to_the_trace(db, env):
    env.setattr(engine, "_shared_matrix", three_tier_matrix())
    plan = [
        {"type": "answerer", "id": "n1", "instruction": "Extract the names.",
         "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}},
        {"type": "answerer", "id": "n2", "instruction": "Judge the strategy.",
         "task_type": "reasoning", "tier": "heavy", "output_schema": {"result": "string"}},
    ]
    StubModels(plan_nodes=plan).install(env)

    result = await run_pipeline(INSTRUCTION, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    routes = {
        d["node_id"]: (envelope_id, model_id, d)
        for envelope_id, model_id, d in _decisions(db, result["run_id"], "orchestrator_decision")
        if d.get("mode") == "route"
    }
    assert set(routes) == {"n1", "n2"}

    envelope_id, model_id, d = routes["n1"]
    assert model_id == d["chosen"] == "openrouter/stub/cheap"
    assert (d["reason"], d["task_type"], d["tier"], d["floor"]) == (
        "routed", "extraction", "light", 6.0,
    )
    assert [c["model_id"] for c in d["candidates"]][0] == "openrouter/stub/cheap"
    assert routes["n2"][2]["chosen"] == "openrouter/stub/top"

    with sqlite3.connect(db) as conn:
        served_by = dict(
            conn.execute(
                "SELECT envelope_id, model_id FROM envelopes WHERE run_id=?",
                (result["run_id"],),
            ).fetchall()
        )
    assert served_by[envelope_id] == "openrouter/stub/cheap"  # the trace names the envelope


@pytest.mark.asyncio
async def test_a_step_no_model_can_serve_at_its_floor_falls_back_and_says_so(db, env):
    matrix = CapabilityMatrix()
    top = stub_model("top", 9.5, 5e-6, 2.5e-5)
    top.task_scores["coding"] = 8.0          # good, but below the heavy floor of 9.0
    matrix.upsert(top)
    matrix.upsert(stub_model("cheap", 6.5, 1e-7, 2e-7))
    env.setattr(engine, "_shared_matrix", matrix)
    plan = [
        {"type": "answerer", "id": "n1", "instruction": "Design the storage engine.",
         "task_type": "coding", "tier": "heavy", "output_schema": {"result": "string"}},
    ]
    stub = StubModels(plan_nodes=plan).install(env)

    result = await run_pipeline(INSTRUCTION, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    assert stub.step_request("Design the storage engine.").model == "stub/top"
    warnings = [
        d for _e, _m, d in _decisions(db, result["run_id"], "error")
        if d.get("code") == "BRT-ORC-019"
    ]
    assert len(warnings) == 1
    assert warnings[0]["severity"] == "warning"
    assert (warnings[0]["node_id"], warnings[0]["tier"], warnings[0]["floor"]) == ("n1", "heavy", 9.0)
    assert warnings[0]["fallback_model"] == "openrouter/stub/top"


@pytest.mark.asyncio
async def test_a_state_op_step_is_not_routed(db, env):
    env.setattr(engine, "_shared_matrix", three_tier_matrix())
    plan = [{"type": "state_op", "id": "n1", "op": "count", "items": [1, 2, 3]}]
    stub = StubModels(plan_nodes=plan).install(env)

    result = await run_pipeline(INSTRUCTION, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    assert stub.of_kind("answerer") == []
    routes = [
        d for _e, _m, d in _decisions(db, result["run_id"], "orchestrator_decision")
        if d.get("mode") == "route"
    ]
    assert routes == []


@pytest.mark.asyncio
async def test_floors_come_from_config_then_env_then_defaults(db, env):
    from src.db.state import set_config

    assert await _load_leaf_floors(db) == DEFAULT_FLOORS

    env.setenv("LEAF_QUALITY_LIGHT", "5.5")
    env.setenv("LEAF_QUALITY_MID", "not a number")
    floors = await _load_leaf_floors(db)
    assert floors == {"light": 5.5, "mid": DEFAULT_FLOORS["mid"], "heavy": DEFAULT_FLOORS["heavy"]}

    await set_config("leaf_quality_light", 7.0, db_path=db)
    assert (await _load_leaf_floors(db))["light"] == 7.0


def _routes(db_path: str, run_id: str) -> dict[str, tuple]:
    return {
        d["node_id"]: (envelope_id, model_id, d)
        for envelope_id, model_id, d in _decisions(db_path, run_id, "orchestrator_decision")
        if d.get("mode") == "route"
    }


@pytest.mark.asyncio
async def test_a_skill_step_is_routed_by_the_skills_tier_and_specialty(db, env):
    """extract_fields is light/extraction in skills.yaml. The planner gives
    no task_type or tier, so without the skill's hints the step would be
    reasoning/mid (stub/mid); with them it is extraction/light (stub/cheap)."""
    env.setattr(engine, "_shared_matrix", three_tier_matrix())
    plan = [
        {"type": "apply_to_each", "id": "n1", "skill_id": "extract_fields",
         "skill_version": 1,
         "skill_args": {"items_ref": "notes", "fields": [{"name": "name", "type": "string"}],
                        "concurrency_limit": 2}},
    ]
    StubModels(plan_nodes=plan).install(env)

    result = await run_pipeline(INSTRUCTION, db_path=db)
    await drain_log_writes()

    routes = _routes(db, result["run_id"])
    assert set(routes) == {"n1"}
    envelope_id, model_id, d = routes["n1"]
    assert (d["reason"], d["task_type"], d["tier"]) == ("routed", "extraction", "light")
    assert model_id == d["chosen"] == "openrouter/stub/cheap"

    with sqlite3.connect(db) as conn:
        served_by = dict(
            conn.execute(
                "SELECT envelope_id, model_id FROM envelopes WHERE run_id=?",
                (result["run_id"],),
            ).fetchall()
        )
    assert served_by[envelope_id] == "openrouter/stub/cheap"


@pytest.mark.asyncio
async def test_a_failed_skill_lookup_is_logged_not_swallowed(db):
    run_id = uuid.uuid4()
    skill = await engine._skill_for_node(
        {"type": "answerer", "skill_id": "no_such_skill"}, run_id, "n7", db
    )
    await drain_log_writes()

    assert skill is None
    warnings = [d for _e, _m, d in _decisions(db, str(run_id), "error")]
    assert len(warnings) == 1
    w = warnings[0]
    assert (w["code"], w["severity"], w["node_id"], w["skill_id"]) == (
        "BRT-ORC-023", "warning", "n7", "no_such_skill",
    )
    assert "BRT-SKL-003" in w["err"]


@pytest.mark.asyncio
async def test_apply_to_each_items_are_served_by_the_steps_routed_model(db, env):
    """The run default is stub/top (quality bar 9.0); the step is routed to
    stub/cheap, and every item must go to stub/cheap too."""
    env.setattr(engine, "_shared_matrix", three_tier_matrix())
    plan = [
        {"type": "apply_to_each", "id": "n1", "instruction": "Pull the name from the note.",
         "task_type": "extraction", "tier": "light",
         "items": ["note alpha", "note beta", "note gamma"],
         "output_schema": {"result": "string"}},
    ]
    stub = StubModels(plan_nodes=plan).install(env)

    result = await run_pipeline(INSTRUCTION, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    item_calls = [r for r in stub.of_kind("answerer") if "Pull the name" in r.system]
    assert len(item_calls) == 3
    assert {r.model for r in item_calls} == {"stub/cheap"}


@pytest.mark.asyncio
async def test_an_unexpected_routing_error_still_fills_every_decision_field(db, env):
    env.setattr(engine, "_shared_matrix", three_tier_matrix())

    def broken_router(**_kwargs):
        raise RuntimeError("router exploded")

    env.setattr(engine, "select_model_for_node", broken_router)
    plan = [
        {"type": "answerer", "id": "n1", "instruction": "Extract the names.",
         "task_type": "extraction", "tier": "light", "output_schema": {"result": "string"}},
    ]
    stub = StubModels(plan_nodes=plan).install(env)

    result = await run_pipeline(INSTRUCTION, db_path=db)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")

    _envelope_id, model_id, d = _routes(db, result["run_id"])["n1"]
    for key in ("reason", "task_type", "tier", "floor", "chosen", "est_cost", "candidates", "error"):
        assert key in d, f"decision is missing {key}: {d}"
    assert d["reason"] == "fallback_default"
    assert (d["task_type"], d["tier"], d["floor"]) == ("extraction", "light", 6.0)
    assert d["candidates"] == []
    assert "router exploded" in d["error"]
    assert model_id == d["chosen"] == "openrouter/stub/top"
    assert stub.step_request("Extract the names.").model == "stub/top"
