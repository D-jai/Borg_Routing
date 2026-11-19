"""Work order 4.3 — every editable setting takes effect on the next run, no restart.

For each setting the Admin Settings page can edit: read what the engine sees,
POST /config with a new value, read again in the same process. A stub run
(real orchestrate_run, fake planner call) records what the engine reads from
the config table; the others are read through the engine's own reader. Time cost, quality bar,
Rule Zero and the pinned planner are asserted on what they drive: the arguments
of the planner selection, the mode of the first planner call, and the host and
model the planner request reached on the stand-in model server.
Temp databases only, never data/borg_route.db.
"""
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import create_all
from src.db.state import create_run
from src.lane.settings import load_lane_settings
from src.orchestrator import engine as engine_mod
from src.pipeline.runner import resolve_max_run_seconds
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.cost import resolve_baseline_with_note
from src.registry.models import ModelInfo
from tests.e2e.stub_model_server import StubModels, drain_log_writes


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def db(tmp_path):
    import src.api.admin as admin_mod
    import src.db.schema as schema

    path = str(tmp_path / "take_effect.db")
    await create_all(path)
    saved = (schema.DB_PATH, admin_mod.DB_PATH)
    schema.DB_PATH = admin_mod.DB_PATH = path
    try:
        yield path
    finally:
        schema.DB_PATH, admin_mod.DB_PATH = saved


@pytest.fixture
def matrix(monkeypatch):
    cm = CapabilityMatrix()
    for model_id, score in (("lmstudio/planner", 9.5), ("openrouter/other", 7.0)):
        cm.upsert(ModelInfo(
            model_id=model_id,
            source="lmstudio" if model_id.startswith("lmstudio") else "openrouter",
            endpoint="http://localhost:1234",
            context_length=32000,
            throughput_tps=50.0,
            task_scores={"reasoning": score},
            checked=True,
        ))
    monkeypatch.setattr(engine_mod, "_shared_matrix", cm)
    monkeypatch.setenv("OPENROUTER_API_KEY", "dummy")
    return cm


async def stub_run(db_path, monkeypatch) -> dict:
    """Run the real orchestrate_run with a fake planner; return the config reads it made."""
    seen: dict = {}
    real_get = engine_mod.get_config

    async def recording_get(key, db_path=db_path):
        value = await real_get(key, db_path=db_path)
        seen[key] = value
        return value

    async def fake_invoke(**kwargs):
        return {"direct_answer": "ok", "plan": None}

    monkeypatch.setattr(engine_mod, "get_config", recording_get)
    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)
    run_id = await create_run("hi", db_path=db_path)
    await engine_mod.orchestrate_run(run_id, "hi", db_path=db_path)
    # The run's log writes are fire-and-forget tasks on this loop. The caller
    # next makes a blocking TestClient call, which freezes this loop: a write
    # caught between its INSERT and its commit would hold the database lock
    # for good and the config write would fail "database is locked".
    await drain_log_writes()
    return seen


async def run_with_select_spy(db_path, monkeypatch) -> dict:
    """Run the engine with a fake planner call; return the arguments the
    planner selection (``select_model``) was given. What the setting drives,
    not what ``get_config`` returned."""
    calls: list[dict] = []
    real_select = engine_mod.select_model

    def spy(*args, **kwargs):
        calls.append(kwargs)
        return real_select(*args, **kwargs)

    monkeypatch.setattr(engine_mod, "select_model", spy)
    await stub_run(db_path, monkeypatch)
    assert calls, "the planner was never selected"
    return calls[0]


def select_arg(name):
    """The ``name`` argument the planner selection got (time cost, quality bar)."""
    async def read(db_path, matrix, monkeypatch):
        return (await run_with_select_spy(db_path, monkeypatch))[name]
    return read


async def read_rule_zero_taken(db_path, matrix, monkeypatch):
    """True when the run answered through the Rule Zero shortcut: its first
    planner call is a compose, never a plan."""
    modes: list[str] = []

    async def fake_invoke(**kwargs):
        modes.append(kwargs["mode"])
        return {"response": "ok", "direct_answer": "ok", "plan": None}

    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)
    run_id = await create_run("hi", db_path=db_path)
    await engine_mod.orchestrate_run(run_id, "hi", db_path=db_path)
    await drain_log_writes()  # see stub_run
    assert modes
    return modes[0] == "compose"


async def planner_request(db_path, monkeypatch):
    """The first planner call, as the stand-in model server received it."""
    stub = StubModels(plan_nodes=[]).install(monkeypatch)
    run_id = await create_run("hi", db_path=db_path)
    await engine_mod.orchestrate_run(run_id, "hi", db_path=db_path)
    await drain_log_writes()
    calls = [r for r in stub.requests if r.kind in ("plan", "compose")]
    assert calls, "no planner call reached the model server"
    return calls[0]


async def read_planner_host(db_path, matrix, monkeypatch):
    """host:port the planner call went to."""
    return urlparse((await planner_request(db_path, monkeypatch)).url).netloc


async def read_planner_model(db_path, matrix, monkeypatch):
    """The model name the planner call asked for."""
    return (await planner_request(db_path, monkeypatch)).model


def floor_read(tier):
    async def read(db_path, matrix, monkeypatch):
        return (await engine_mod._load_leaf_floors(db_path))[tier]
    return read


def lane_read(attr):
    async def read(db_path, matrix, monkeypatch):
        # With lane_mode off no other lane setting is read (Phase 1 exactly),
        # so the other lane keys are read with the lane on.
        if attr != "mode":
            monkeypatch.setenv("BORG_LANE_MODE", "raw_local")
        return getattr(await load_lane_settings(db_path), attr)
    return read


async def read_max_run_seconds(db_path, matrix, monkeypatch):
    return await resolve_max_run_seconds(db_path=db_path)


async def read_baseline(db_path, matrix, monkeypatch):
    baseline, _note = await resolve_baseline_with_note(matrix, db_path)
    return baseline.model_id


# key -> (value to POST, what the engine should then read, reader)
CASES = {
    "max_run_seconds": ("42", 42, read_max_run_seconds),
    # these five are read through what they drive in the engine, not through get_config
    "time_cost_per_sec": ("0.5", 0.5, select_arg("time_cost_per_sec")),
    "orchestrator_quality_requirement": ("7.0", 7.0, select_arg("quality_requirement")),
    "rule_zero_shortcut": ("false", False, read_rule_zero_taken),
    "orchestrator_endpoint": ("http://pinned.test:9", "pinned.test:9", read_planner_host),
    "orchestrator_model_name": ("pinned-model", "pinned-model", read_planner_model),
    "leaf_quality_light": ("5.25", 5.25, floor_read("light")),
    "leaf_quality_mid": ("6.5", 6.5, floor_read("mid")),
    "leaf_quality_heavy": ("8.25", 8.25, floor_read("heavy")),
    "baseline_model_id": ("openrouter/other", "openrouter/other", read_baseline),
    "lane_mode": ("raw_local", "raw_local", lane_read("mode")),
    "lane_max_output_chars": ("777", 777, lane_read("max_output_chars")),
    "lane_max_verbatim_chars": ("66", 66, lane_read("max_verbatim_chars")),
    "lane_max_ngram_share": ("0.25", 0.25, lane_read("max_ngram_share")),
    "lane_manifest": ("ids", "ids", lane_read("manifest")),
    "lane_local_allowlist": ("ollama:11434", ("ollama:11434",), lane_read("local_allowlist")),
    "lane_part_chars": ("5555", 5555, lane_read("part_chars")),
}


def editable_keys(client):
    body = client.get("/config/effective").json()
    return {s["key"] for s in body["settings"] if s["editable"]}


def test_every_editable_setting_has_a_case(client, db):
    """A new editable setting cannot be added without a take-effect test."""
    assert editable_keys(client) == set(CASES)


def test_settings_the_engine_ignores_are_not_listed(client, db):
    keys = {s["key"] for s in client.get("/config/effective").json()["settings"]}
    assert not keys & {"max_recursion_depth", "default_strategy", "context_window_size"}


@pytest.mark.asyncio
@pytest.mark.parametrize("key", sorted(CASES))
async def test_setting_takes_effect_on_the_next_run(key, client, db, matrix, monkeypatch):
    value, expected, read = CASES[key]
    if key == "orchestrator_model_name":
        # the name is only read once a pinned endpoint is set
        assert client.post("/config", json={"key": "orchestrator_endpoint", "value": "http://pinned.test:9"}).status_code == 200

    before = await read(db, matrix, monkeypatch)
    assert before != expected  # a real change, not a value that was already there

    res = client.post("/config", json={"key": key, "value": value})
    assert res.status_code == 200, res.text

    # same process, same app, no restart
    assert await read(db, matrix, monkeypatch) == expected
    shown = {s["key"]: s for s in client.get("/config/effective").json()["settings"]}[key]
    assert shown["source"] == "db"


@pytest.mark.parametrize("key,bad", [
    ("time_cost_per_sec", "abc"),
    ("max_run_seconds", "1.5"),
    ("max_run_seconds", "0"),
    ("orchestrator_quality_requirement", "11"),
    ("leaf_quality_mid", "abc"),
    ("rule_zero_shortcut", "maybe"),
    ("lane_mode", "everything"),
    ("lane_max_ngram_share", "2"),
])
def test_a_bad_value_is_refused_and_not_saved(key, bad, client, db):
    res = client.post("/config", json={"key": key, "value": bad})
    assert res.status_code == 400
    assert client.get("/config").json().get(key) is None


@pytest.mark.parametrize("key", ["orchestrator_max_tokens", "answerer_max_tokens"])
def test_env_only_settings_are_not_editable(key, client, db):
    """The engine reads these from .env only; a saved value would be ignored, so it is refused."""
    res = client.post("/config", json={"key": key, "value": "9000"})
    assert res.status_code == 400
    shown = {s["key"]: s for s in client.get("/config/effective").json()["settings"]}[key]
    assert shown["editable"] is False
