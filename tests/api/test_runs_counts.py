"""4.3 review: the counts on Overview and Logs Stats are server totals, not the
length of a 200-row page. GET /runs carries ``counts`` over every run.
"""
import pytest
from fastapi.testclient import TestClient

from main import app
from src.api import tasks as tasks_api
from src.db.schema import create_all
from src.db.state import create_run, set_run_rating, update_run_status


@pytest.fixture
async def db(tmp_path, monkeypatch):
    path = str(tmp_path / "counts.db")
    await create_all(path)
    monkeypatch.setattr(tasks_api, "DB_PATH", path)
    return path


@pytest.mark.asyncio
async def test_runs_counts_are_totals_beyond_the_page(db):
    ids = [await create_run(f"q{i}", db_path=db) for i in range(7)]
    for rid in ids[:3]:
        await update_run_status(rid, "failed", failure_reason="x", db_path=db)
    for rid in ids[3:5]:
        await update_run_status(rid, "completed", db_path=db)
    await set_run_rating(ids[3], 1, db_path=db)
    await set_run_rating(ids[4], -1, db_path=db)
    await create_run("direct", db_path=db, kind="bypass")

    body = TestClient(app).get("/runs?limit=2").json()

    assert len(body["runs"]) == 2
    assert body["counts"] == {
        "total": 7, "completed": 2, "failed": 3, "running": 0, "pending": 2,
        "thumbs_up": 1, "thumbs_down": 1,
    }
