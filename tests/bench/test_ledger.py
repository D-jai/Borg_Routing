"""The spend ledger is append-only, written at run start, recovered from the run's database after a crash,
and never records 0 for a run that spent; each BORG run's database is kept under results/<date>/dbs/."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from bench import budget, run_bench
from bench.run_bench import Config, RunSpec
from tests.e2e.stub_model_server import three_tier_matrix

REPO = Path(__file__).resolve().parents[2]

JOB = {"job": "T", "data_folder": "job_t", "answers_file": "answers/t.json", "instruction": "Label.",
       "arms": {"direct": {}, "borg_cloud": {"lane_mode": "by_reference"}}, "repeats": 1, "est_output_tokens": 200,
       "quality": {"kind": "classification", "key_field": "file", "fields": ["risk"]}}


@pytest.fixture
def cfg(tmp_path):
    root = tmp_path / "bench"
    (root / "data" / "job_t").mkdir(parents=True)
    (root / "data" / "answers").mkdir(parents=True)
    (root / "data" / "job_t" / "a.txt").write_text("Client A.", encoding="utf-8")
    (root / "data" / "answers" / "t.json").write_text(json.dumps({"answers": {"a.txt": {"risk": "high"}}}),
                                                      encoding="utf-8")
    return Config(bench_dir=root, results_root=root / "results", date="2026_01_01", use_docker=False)


def fake_logged_db(path: Path, *dollars: float) -> None:
    """A run database with logged model calls (the columns the recovery reads)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS logs (event_type TEXT, dollars REAL)")
        conn.executemany("INSERT INTO logs VALUES ('model_call', ?)", [(d,) for d in dollars])


def entries(cfg):
    return json.loads((cfg.results_root / "spend.json").read_text(encoding="utf-8"))["entries"]


def test_a_rerun_adds_an_entry_and_never_replaces(tmp_path):
    led = budget.Ledger(tmp_path / "spend.json")
    led.record("T_direct_1", 1.25)
    led.record("T_direct_1", 1.5)
    assert led.spent() == pytest.approx(2.75)
    data = json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))
    assert [e["dollars"] for e in data["entries"]] == [1.25, 1.5]


def test_old_ledger_format_is_kept(tmp_path):
    (tmp_path / "spend.json").write_text(json.dumps(
        {"prior_dollars": 2.0, "runs": [{"key": "A_direct_1", "kind": "run", "dollars": 0.5}]}), encoding="utf-8")
    led = budget.Ledger(tmp_path / "spend.json")
    assert led.spent() == pytest.approx(2.5)
    led.record("A_direct_2", 0.25)
    assert led.spent() == pytest.approx(2.75)


def test_start_then_finish(tmp_path):
    led = budget.Ledger(tmp_path / "spend.json")
    eid = led.start("C_borg_cloud_1", db_path="x.db", date="2026_01_01")
    e = json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))["entries"][0]
    assert e["status"] == "started" and e["db_path"] == "x.db" and e["date"] == "2026_01_01"
    led.finish(eid, 0.75)
    e = json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))["entries"][0]
    assert e["status"] == "ended" and e["dollars"] == 0.75 and e["ended_at"]


def test_an_unfinished_entry_is_recovered_from_its_database_and_calls_log(tmp_path):
    db = tmp_path / "dbs" / "T_borg_cloud_1.db"
    fake_logged_db(db, 0.5, 0.25)
    calls = tmp_path / "dbs" / "T_direct_1.calls.jsonl"
    calls.write_text(json.dumps({"dollars": 0.125}) + "\n" + json.dumps({"dollars": 0.125}) + "\n", encoding="utf-8")
    led = budget.Ledger(tmp_path / "spend.json")
    led.start("T_borg_cloud_1", db_path=str(db))
    led.start("T_direct_1", calls_log=str(calls))
    led.start("T_borg_cloud_2", db_path=str(tmp_path / "dbs" / "never_created.db"))
    assert led.spent() == 0.0
    rec = led.recover()
    assert [r["key"] for r in rec] == ["T_borg_cloud_1", "T_direct_1", "T_borg_cloud_2"]
    assert led.spent() == pytest.approx(1.0)
    assert {e["status"] for e in json.loads((tmp_path / "spend.json").read_text(encoding="utf-8"))["entries"]} == {"recovered"}
    assert led.recover() == []  # once only


async def test_the_entry_is_written_at_run_start_with_the_db_path(cfg, monkeypatch):
    seen = {}

    async def fake_borg(job, spec, cfg_, matrix, db_path, baseline=None):
        seen["entries"] = json.loads((cfg_.results_root / "spend.json").read_text(encoding="utf-8"))["entries"]
        seen["db_path"] = str(db_path)
        fake_logged_db(Path(db_path), 0.4)
        return {"status": "completed", "cost_dollars": 0.4, "answer": "[]", "models": [], "models_per_step": []}

    monkeypatch.setattr(run_bench, "run_borg", fake_borg)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m))
    e = seen["entries"][0]
    assert e["status"] == "started" and e["key"] == "T_borg_cloud_1" and e["db_path"] == seen["db_path"]
    assert Path(seen["db_path"]) == cfg.day_dir / "dbs" / "T_borg_cloud_1.db"
    assert rec["db_path"] == seen["db_path"]
    assert json.loads(Path(rec["path"]).read_text(encoding="utf-8"))["db_path"] == seen["db_path"]
    assert entries(cfg)[0]["status"] == "ended" and entries(cfg)[0]["dollars"] == pytest.approx(0.4)


async def test_a_crashed_borg_run_records_its_logged_dollars_never_zero(cfg, monkeypatch):
    async def crashing_borg(job, spec, cfg_, matrix, db_path, baseline=None):
        fake_logged_db(Path(db_path), 0.3, 0.2)
        raise RuntimeError("engine fell over")

    monkeypatch.setattr(run_bench, "run_borg", crashing_borg)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m))
    assert rec["status"] == "failed" and rec["cost_dollars"] == pytest.approx(0.5)
    assert cfg.ledger.spent() == pytest.approx(0.5)


async def test_a_crashed_direct_run_records_the_calls_it_paid_for(cfg):
    n = {"i": 0}

    async def chat(model, system, user, max_tokens, timeout=0):
        n["i"] += 1
        if n["i"] == 2:
            raise RuntimeError("socket gone")
        return {"content": "partial", "tokens_in": 10, "tokens_out": 10, "dollars": 0.07, "finish_reason": "stop",
                "model": model.model_id}

    m = three_tier_matrix()
    base = run_bench.baseline_model(m)
    base.context_length = 1500  # forces two calls (one per note) so the crash comes after a paid call
    (cfg.data_root / "job_t" / "b.txt").write_text("Client B.", encoding="utf-8")
    rec = await run_bench.execute_run(RunSpec("T", "direct", 1), {"T": JOB}, cfg, m, base, chat=chat)
    assert rec["status"] == "failed" and rec["cost_dollars"] == pytest.approx(0.07)
    assert cfg.ledger.spent() == pytest.approx(0.07)
    assert rec["calls_log"] and Path(rec["calls_log"]).exists()


async def test_run_matrix_recovers_an_unfinished_entry_before_it_starts(cfg):
    db = cfg.day_dir / "dbs" / "T_borg_cloud_9.db"
    fake_logged_db(db, 1.5)
    cfg.ledger.start("T_borg_cloud_9", db_path=str(db))
    await run_bench.run_matrix([], {"T": JOB}, cfg, three_tier_matrix(), None, log=lambda *_: None)
    assert cfg.ledger.spent() == pytest.approx(1.5)


def test_a_second_run_with_the_same_key_gets_a_new_database(cfg):
    spec = RunSpec("T", "borg_cloud", 1)
    first = run_bench.new_db_path(cfg, spec)
    assert first == cfg.day_dir / "dbs" / "T_borg_cloud_1.db"
    first.parent.mkdir(parents=True)
    first.write_bytes(b"")
    assert run_bench.new_db_path(cfg, spec) == cfg.day_dir / "dbs" / "T_borg_cloud_1__2.db"


def test_run_databases_are_git_ignored():
    lines = (REPO / ".gitignore").read_text(encoding="utf-8").splitlines()
    assert "bench/results/*/dbs/" in lines


async def test_paths_written_into_results_and_the_ledger_are_relative_to_the_repo_root(tmp_path, monkeypatch):
    """Ruling 5.3 review: a result file and a ledger entry hold repo-relative paths; crash recovery resolves them."""
    monkeypatch.setattr(budget, "REPO_ROOT", tmp_path)
    root = tmp_path / "bench"
    (root / "data" / "job_t").mkdir(parents=True)
    (root / "data" / "answers").mkdir(parents=True)
    (root / "data" / "job_t" / "a.txt").write_text("Client A.", encoding="utf-8")
    (root / "data" / "answers" / "t.json").write_text(json.dumps({"answers": {"a.txt": {"risk": "high"}}}),
                                                      encoding="utf-8")
    cfg = Config(bench_dir=root, results_root=root / "results", date="2026_01_01", use_docker=False)

    async def fake_borg(job, spec, cfg_, matrix, db_path, baseline=None):
        fake_logged_db(Path(db_path), 0.4)
        return {"status": "completed", "cost_dollars": 0.4, "answer": "[]", "models": [], "models_per_step": []}

    monkeypatch.setattr(run_bench, "run_borg", fake_borg)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m))
    expected = "bench/results/2026_01_01/dbs/T_borg_cloud_1.db"
    assert json.loads(Path(rec["path"]).read_text(encoding="utf-8"))["db_path"] == expected
    assert entries(cfg)[0]["db_path"] == expected
    # the relative path resolves against the repo root, so recovery and stage 2 still find the database
    assert budget.logged_dollars(expected) == pytest.approx(0.4)
    # an absolute path (the committed round-1 files) is still read as it is
    assert budget.logged_dollars(str(tmp_path / expected)) == pytest.approx(0.4)
