"""Slice 5 tests — observability, NER, rating, digests, skills API.

TC-S5-001 — run_digests table created by create_all
TC-S5-002 — compute_and_save_digest: stores correct summary and returns dict
TC-S5-003 — compute_and_save_digest: upsert — second call updates existing row
TC-S5-004 — set_run_rating: 1 and -1 accepted; stored correctly
TC-S5-005 — set_run_rating: invalid value raises ValueError
TC-S5-006 — set_run_rating: unknown run_id raises BRT-DB-010
TC-S5-007 — run_pipeline: digest saved after successful run
TC-S5-008 — POST /runs/{id}/rating returns 200 with valid rating
TC-S5-009 — POST /runs/{id}/rating returns 400 for invalid rating
TC-S5-010 — NER worker: process_batch extracts entities from log_payloads
TC-S5-011 — NER worker: marks ner_processed=1 after processing
TC-S5-012 — NER worker: nonexistent db is handled gracefully
TC-S5-013 — GET /logs returns list of log events
TC-S5-014 — GET /logs/cost-summary returns by_model aggregates
TC-S5-015 — GET /skills returns skill list with stats
TC-S5-016 — POST /skills/{id}/version creates new version
TC-S5-017 — POST /skills/nonexistent/version returns 404
"""
import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import create_all, drop_all
from src.db.state import (
    compute_and_save_digest,
    create_run,
    set_run_rating,
    update_run_status,
    get_run,
)
from src.logging.writer import write_event
from src.logging.ner_worker import process_batch


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "slice5_test.db")
    await create_all(path)
    return path


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-001 — schema tables created
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_digests_table_exists(db):
    """TC-S5-001 — run_digests table created by create_all."""
    import aiosqlite
    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='run_digests'"
        ) as cur:
            row = await cur.fetchone()
    assert row is not None


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-002 — compute_and_save_digest happy path
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_and_save_digest_stores_correctly(db):
    """TC-S5-002 — digest computed and stored for a run."""
    import aiosqlite
    run_id = await create_run("test task", db_path=db)
    result = await compute_and_save_digest(run_id, db_path=db)

    assert result["run_id"] == str(run_id)
    assert "envelope(s)" in result["summary"]

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT * FROM run_digests WHERE run_id=?", (str(run_id),)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-003 — compute_and_save_digest upserts
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compute_and_save_digest_upserts(db):
    """TC-S5-003 — second call overwrites existing row without error."""
    run_id = await create_run("task", db_path=db)
    await compute_and_save_digest(run_id, db_path=db)
    result2 = await compute_and_save_digest(run_id, db_path=db)
    assert result2["run_id"] == str(run_id)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-004 — set_run_rating accepts 1 and -1
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_run_rating_valid(db):
    """TC-S5-004 — user_rating stored for thumbs up and down."""
    run_id = await create_run("task", db_path=db)
    await set_run_rating(run_id, 1, db_path=db)
    run = await get_run(run_id, db_path=db)
    assert run["user_rating"] == 1

    await set_run_rating(run_id, -1, db_path=db)
    run = await get_run(run_id, db_path=db)
    assert run["user_rating"] == -1


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-005 — set_run_rating rejects invalid values
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_run_rating_invalid_raises(db):
    """TC-S5-005 — ValueError raised for invalid rating value."""
    run_id = await create_run("task", db_path=db)
    with pytest.raises(ValueError, match="must be 1"):
        await set_run_rating(run_id, 0, db_path=db)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-006 — set_run_rating unknown run raises BRT-DB-010
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_set_run_rating_unknown_run(db):
    """TC-S5-006 — BRT-DB-010 raised for unknown run_id."""
    with pytest.raises(ValueError, match="BRT-DB-010"):
        await set_run_rating(uuid.uuid4(), 1, db_path=db)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-007 — run_pipeline saves digest on completion
# ──────────────────────────────────────────────────────────────────────calls
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_pipeline_saves_digest(db):
    """TC-S5-007 — run_pipeline writes a run_digest row after completion."""
    import aiosqlite

    async def mock_orchestrate(*args, **kwargs):
        return "done"

    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate)):
        result = await __import__("src.pipeline.runner", fromlist=["run_pipeline"]).run_pipeline(
            user_instruction="test", db_path=db
        )

    assert result["status"] == "completed"
    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT run_id FROM run_digests WHERE run_id=?", (result["run_id"],)
        ) as cur:
            row = await cur.fetchone()
    assert row is not None


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-008 — POST /runs/{id}/rating happy path
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rate_run_endpoint_valid(db):
    """TC-S5-008 — POST /runs/{id}/rating returns 200 with valid rating."""
    run_id = await create_run("task", db_path=db)

    with patch("src.api.tasks.DB_PATH", db):
        client = TestClient(app, raise_server_exceptions=False)
        resp = client.post(f"/runs/{run_id}/rating", json={"rating": 1})

    assert resp.status_code == 200
    assert resp.json()["rating"] == 1


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-009 — POST /runs/{id}/rating rejects invalid
# ──────────────────────────────────────────────────────────────────────────────

def test_rate_run_endpoint_invalid_rating(client):
    """TC-S5-009 — POST /runs/{id}/rating returns 400 for bad rating value."""
    resp = client.post(f"/runs/{uuid.uuid4()}/rating", json={"rating": 5})
    assert resp.status_code == 400
    assert "BRT-API-026" in resp.text


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-010 — NER worker extracts entities
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ner_worker_extracts_entities(db):
    """TC-S5-010 — process_batch extracts PERSON/ORG entities from payload."""
    import aiosqlite
    run_id = await create_run("task", db_path=db)

    # Write a log event with a payload containing named entities
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            """INSERT INTO logs(run_id, event_type, model_id)
               VALUES (?, 'model_call', 'test-model')""",
            (str(run_id),),
        )
        await conn.commit()
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            log_id = (await cur.fetchone())[0]
        await conn.execute(
            """INSERT INTO log_payloads(log_id, direction, text, ner_processed)
               VALUES (?, 'out', 'Barack Obama visited Google in London.', 0)""",
            (log_id,),
        )
        await conn.commit()

    count = await process_batch(db_path=db)
    assert count >= 1

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT text, label FROM log_entities WHERE run_id=?", (str(run_id),)
        ) as cur:
            entities = await cur.fetchall()
    entity_texts = [e[0] for e in entities]
    assert any("Obama" in t or "Barack" in t for t in entity_texts)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-011 — NER worker marks ner_processed=1
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ner_worker_marks_processed(db):
    """TC-S5-011 — payload ner_processed flag set to 1 after processing."""
    import aiosqlite
    run_id = await create_run("task", db_path=db)

    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "INSERT INTO logs(run_id, event_type) VALUES (?, 'model_call')", (str(run_id),)
        )
        await conn.commit()
        async with conn.execute("SELECT last_insert_rowid()") as cur:
            log_id = (await cur.fetchone())[0]
        await conn.execute(
            "INSERT INTO log_payloads(log_id, direction, text, ner_processed) VALUES (?, 'in', 'Simple text.', 0)",
            (log_id,),
        )
        await conn.commit()

    await process_batch(db_path=db)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT ner_processed FROM log_payloads WHERE log_id=?", (log_id,)
        ) as cur:
            row = await cur.fetchone()
    assert row[0] == 1


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-012 — NER worker returns 0 when no unprocessed payloads
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_ner_worker_empty_batch(db):
    """TC-S5-012 — process_batch returns 0 when nothing to process."""
    count = await process_batch(db_path=db)
    assert count == 0


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-013 — GET /logs returns list
# ──────────────────────────────────────────────────────────────────────────────

def test_get_logs_returns_list(client):
    """TC-S5-013 — GET /logs returns a JSON object with logs list."""
    resp = client.get("/logs")
    assert resp.status_code == 200
    assert "logs" in resp.json()


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-014 — GET /logs/cost-summary returns by_model
# ──────────────────────────────────────────────────────────────────────────────

def test_cost_summary_returns_structure(client):
    """TC-S5-014 — GET /logs/cost-summary returns by_model and totals keys."""
    resp = client.get("/logs/cost-summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "by_model" in data
    assert "totals" in data


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-015 — GET /skills returns list
# ──────────────────────────────────────────────────────────────────────────────

def test_get_skills_returns_list(client):
    """TC-S5-015 — GET /skills returns a skills list."""
    resp = client.get("/skills")
    assert resp.status_code == 200
    data = resp.json()
    assert "skills" in data
    assert isinstance(data["skills"], list)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-016 — POST /skills/{id}/version creates new version
# ──────────────────────────────────────────────────────────────────────────────

def test_create_skill_version(client, tmp_path):
    """TC-S5-016 — POST /skills/{id}/version appends new version to yaml."""
    import yaml
    import src.api.skills as skills_module

    temp_skills = str(tmp_path / "skills.yaml")
    sample = [{"skill_id": "test_skill", "version": 1, "description": "A test skill",
               "envelope_type": "answerer", "recommended_tier": "light",
               "when_to_use": [], "not_when": [], "outbound_template": {}, "required_args": {}}]
    with open(temp_skills, "w") as f:
        yaml.dump(sample, f)

    # Pre-populate the in-memory library with test data and prevent reload from
    # overwriting it — patch load() to be a no-op for this test.
    skills_module._library._skills = sample

    with patch("src.api.skills.SKILLS_PATH", temp_skills), \
         patch.object(skills_module._library, "load"):
        resp = client.post("/skills/test_skill/version", json={
            "description": "Improved version",
            "when_to_use": ["use this"],
            "not_when": [],
            "recommended_tier": "mid",
        })

    assert resp.status_code == 201
    data = resp.json()
    assert data["version"] == 2
    assert data["skill_id"] == "test_skill"

    with open(temp_skills) as f:
        loaded = yaml.safe_load(f)
    versions = [s for s in loaded if s["skill_id"] == "test_skill"]
    assert len(versions) == 2
    assert any(s["version"] == 2 for s in versions)


# ──────────────────────────────────────────────────────────────────────────────
# TC-S5-017 — POST /skills/nonexistent/version returns 404
# ──────────────────────────────────────────────────────────────────────────────

def test_create_skill_version_not_found(client):
    """TC-S5-017 — 404 returned for unknown skill_id."""
    resp = client.post("/skills/does_not_exist_skill_xyz/version", json={
        "description": "nope",
    })
    assert resp.status_code == 404
    assert "BRT-API-051" in resp.text
