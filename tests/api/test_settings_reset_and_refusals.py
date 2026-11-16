"""4.3 review: saving an empty setting resets it (env/default applies again),
and POST /config refuses unknown keys, a non-URL planner endpoint and a
rules ledger that is not a list of objects. Temp databases only.
"""
import json

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import create_all


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
async def db(tmp_path):
    import src.api.admin as admin_mod
    import src.db.schema as schema

    path = str(tmp_path / "reset.db")
    await create_all(path)
    saved = (schema.DB_PATH, admin_mod.DB_PATH)
    schema.DB_PATH = admin_mod.DB_PATH = path
    try:
        yield path
    finally:
        schema.DB_PATH, admin_mod.DB_PATH = saved


def shown(client, key):
    return {s["key"]: s for s in client.get("/config/effective").json()["settings"]}[key]


def test_empty_save_deletes_the_key_and_env_applies_again(client, db, monkeypatch):
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.25")
    assert client.post("/config", json={"key": "time_cost_per_sec", "value": "0.5"}).status_code == 200
    assert shown(client, "time_cost_per_sec")["source"] == "db"

    res = client.post("/config", json={"key": "time_cost_per_sec", "value": "  "})
    assert res.status_code == 200
    assert res.json()["status"] == "reset"
    assert "time_cost_per_sec" not in client.get("/config").json()
    now = shown(client, "time_cost_per_sec")
    assert (now["source"], now["value"]) == ("env", 0.25)


def test_empty_save_falls_back_to_the_default_without_env(client, db, monkeypatch):
    monkeypatch.delenv("BORG_LANE_PART_CHARS", raising=False)
    client.post("/config", json={"key": "lane_part_chars", "value": "5555"})
    assert client.post("/config", json={"key": "lane_part_chars", "value": ""}).status_code == 200
    now = shown(client, "lane_part_chars")
    assert (now["source"], now["value"]) == ("default", 8000)


def test_empty_save_of_a_key_that_was_never_saved_is_harmless(client, db):
    res = client.post("/config", json={"key": "baseline_model_id", "value": ""})
    assert res.status_code == 200
    assert shown(client, "baseline_model_id")["source"] == "default"


def test_env_only_setting_still_refuses_an_empty_save(client, db):
    assert client.post("/config", json={"key": "answerer_max_tokens", "value": ""}).status_code == 400


@pytest.mark.parametrize("key", ["max_recursion_depth", "typo_key", "context_window_size"])
def test_unknown_key_is_refused_and_not_saved(key, client, db):
    res = client.post("/config", json={"key": key, "value": "3"})
    assert res.status_code == 400
    assert res.json()["detail"]["error"] == "BRT-ADM-006"
    assert key not in client.get("/config").json()


@pytest.mark.parametrize("bad", ["pinned-host", "ftp://host:1", "http://", "localhost:1234", "http:///x"])
def test_non_url_planner_endpoint_is_refused(bad, client, db):
    res = client.post("/config", json={"key": "orchestrator_endpoint", "value": bad})
    assert res.status_code == 400
    assert client.get("/config").json().get("orchestrator_endpoint") is None


@pytest.mark.parametrize("good", ["http://localhost:1234", "https://planner.example.com/v1"])
def test_url_planner_endpoint_is_saved(good, client, db):
    assert client.post("/config", json={"key": "orchestrator_endpoint", "value": good}).status_code == 200


@pytest.mark.parametrize("bad", ["not json", "{}", "[1, 2]", '["a"]', "\"text\"", "[{\"id\":\"1\"}, 3]"])
def test_rules_ledger_that_is_not_a_list_of_objects_is_refused(bad, client, db):
    res = client.post("/config", json={"key": "admin_rules_ledger", "value": bad})
    assert res.status_code == 400
    assert "admin_rules_ledger" not in client.get("/config").json()


def test_a_ledger_of_objects_and_global_rules_are_saved(client, db):
    ledger = json.dumps([{"id": "ab", "text": "Be brief.", "retired_at": None}])
    assert client.post("/config", json={"key": "admin_rules_ledger", "value": ledger}).status_code == 200
    assert client.post("/config", json={"key": "global_rules", "value": "Be kind."}).status_code == 200
    assert client.get("/config/rules").json()["rules"][0]["id"] == "ab"
