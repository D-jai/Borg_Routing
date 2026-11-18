"""Ruling 22: the run digest stores ``direct_floor_dollars``; the old per-run baseline stops."""
import json
import sqlite3

import pytest

from src.db.state import compute_and_save_digest, create_run, get_run_digest
from src.orchestrator import engine
from src.pipeline.runner import run_pipeline
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

TOP_IN, TOP_OUT = 5e-6, 2.5e-5      # stub/top: the highest-reasoning checked model = the baseline
ROOT = "Review the client notes and say which contract is at risk."


@pytest.fixture
async def db(tmp_path, monkeypatch):
    from src.db.schema import create_all

    path = str(tmp_path / "floor.db")
    await create_all(path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-floor")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.delenv("BASELINE_MODEL_ID", raising=False)
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    return path


def _q(db, sql, *args):
    with sqlite3.connect(db) as conn:
        return conn.execute(sql, args).fetchall()


@pytest.mark.asyncio
async def test_the_digest_floor_counts_message_documents_once_and_the_answer(db):
    docs = [{"id": "doc_001", "type": "txt", "size": 9, "parts": 1, "chars": 1000, "name": "a.txt"},
            {"id": "doc_002", "type": "md", "size": 9, "parts": 2, "chars": 600, "name": "b.md"},
            {"type": "pdf", "size": 5, "status": "unsupported, not read", "name": "c.pdf"}]
    run_id = await create_run("x" * 400, db_path=db, documents=docs)
    answer = "y" * 800
    digest = await compute_and_save_digest(run_id, db_path=db, answer_text=answer)
    expected = (400 + 1000 + 600) / 4 * TOP_IN + 800 / 4 * TOP_OUT
    assert digest["direct_floor_dollars"] == pytest.approx(expected, rel=1e-9)
    stored = await get_run_digest(run_id, db_path=db)
    assert stored["direct_floor_dollars"] == pytest.approx(expected, rel=1e-9)
    assert stored["direct_floor_model"] == "openrouter/stub/top"
    assert stored["baseline_dollars"] is None          # the old meaning is no longer written


@pytest.mark.asyncio
async def test_a_digest_saved_without_an_answer_keeps_an_earlier_floor(db):
    run_id = await create_run("x" * 40, db_path=db)
    await compute_and_save_digest(run_id, db_path=db, answer_text="y" * 40)
    first = (await get_run_digest(run_id, db_path=db))["direct_floor_dollars"]
    assert first is not None
    await compute_and_save_digest(run_id, db_path=db)          # a later re-save, no answer in hand
    assert (await get_run_digest(run_id, db_path=db))["direct_floor_dollars"] == first


@pytest.mark.asyncio
async def test_old_digest_rows_are_kept_and_read_as_no_floor(db):
    run_id = await create_run("hello", db_path=db)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO run_digests(run_id, summary, baseline_dollars) VALUES (?, 's', 1.5)", (str(run_id),))
    d = await get_run_digest(run_id, db_path=db)
    assert d["baseline_dollars"] == 1.5 and d["direct_floor_dollars"] is None
    await compute_and_save_digest(run_id, db_path=db, answer_text="ok")
    assert (await get_run_digest(run_id, db_path=db))["baseline_dollars"] == 1.5   # stored data is never erased


@pytest.mark.asyncio
async def test_a_pipeline_run_stores_the_floor_from_the_answer_it_returns(db):
    plan = [{"type": "answerer", "id": "n1", "instruction": "Pick the client.",
             "tier": "light", "output_schema": {"result": "string"}}]
    StubModels(plan_nodes=plan).install(pytest.MonkeyPatch())
    result = await run_pipeline(ROOT, db_path=db)
    await drain_log_writes()
    expected = len(ROOT) / 4 * TOP_IN + len(result["response"]) / 4 * TOP_OUT
    assert result["direct_floor_dollars"] == pytest.approx(expected, rel=1e-9)
    (row,) = _q(db, "SELECT direct_floor_dollars, baseline_dollars FROM run_digests WHERE run_id=?", result["run_id"])
    assert row[0] == pytest.approx(expected, rel=1e-9) and row[1] is None


@pytest.mark.asyncio
async def test_get_run_returns_the_floor_and_the_published_ratio_range(db, monkeypatch):
    from src.api import tasks as tasks_api
    from src.registry.cost import DIRECT_FLOOR_RATIO_RANGE

    run_id = await create_run("x" * 40, db_path=db)
    await compute_and_save_digest(run_id, db_path=db, answer_text="y" * 40)
    monkeypatch.setattr(tasks_api, "DB_PATH", db)
    body = json.loads((await tasks_api.get_run_state(str(run_id))).body)
    d = body["digest"]
    assert d["direct_floor_dollars"] > 0 and d["direct_floor_model"] == "openrouter/stub/top"
    assert d["direct_floor_ratio"] == list(DIRECT_FLOOR_RATIO_RANGE)
    assert "baseline_dollars" in d and d["baseline_dollars"] is None


def test_the_document_manifest_carries_each_documents_chars(tmp_path, monkeypatch):
    from src.lane.documents import load_documents

    (tmp_path / "a.txt").write_text("héllo wörld", encoding="utf-8")
    monkeypatch.setenv("BORG_DOCS_ROOT", str(tmp_path))
    rows = load_documents({"files": ["a.txt"]}).manifest()
    assert rows[0]["chars"] == len("héllo wörld") and rows[0]["size"] > rows[0]["chars"]
