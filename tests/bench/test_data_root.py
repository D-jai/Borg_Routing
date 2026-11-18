"""Planner ruling 33a: --data-root runs the bench on data regenerated outside bench/data (round-1 seed),
and every reader (docs root, answer keys, canaries, sizing) follows it. bench/data is never touched."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench import make_data, run_bench

ROUND1 = 20261117


@pytest.fixture(scope="module")
def r1(tmp_path_factory):
    root = tmp_path_factory.mktemp("round1_data")
    make_data.write_all(root, ROUND1)
    return root


@pytest.fixture
def clean_env(tmp_path):
    import os
    saved = dict(os.environ)
    os.environ.pop("BASELINE_MODEL_ID", None)
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    yield empty
    os.environ.clear()
    os.environ.update(saved)


def jobs():
    return run_bench.load_jobs(run_bench.BENCH_DIR / "jobs")


def test_default_data_root_is_unchanged():
    cfg = run_bench.Config()
    assert cfg.data_root == run_bench.BENCH_DIR / "data"
    assert cfg.data_root == make_data.DATA_DIR


def test_graders_score_against_the_given_roots_answer_keys(r1):
    job = jobs()["A"]
    key1 = json.loads((r1 / "answers" / "job_a_answers.json").read_text(encoding="utf-8"))
    stub = json.dumps([{"file": n, **t} for n, t in key1["answers"].items()])
    q = job["quality"]
    on_r1 = run_bench.score_answer(job, stub, run_bench.Config(data_root_override=r1))
    assert on_r1["accuracy"] == 1.0
    on_default = run_bench.score_answer(job, stub, run_bench.Config())
    assert on_default["accuracy"] < 1.0, q


def test_every_reader_follows_the_root(r1):
    cfg = run_bench.Config(data_root_override=r1)
    assert cfg.data_root == r1
    js = jobs()
    d_key = run_bench.load_answers(js["D"], cfg)
    r1_key = json.loads((r1 / "answers" / "job_d_answers.json").read_text(encoding="utf-8"))
    assert d_key["canaries"] == r1_key["canaries"]
    assert run_bench.load_answers(js["C"], cfg) == json.loads(
        (r1 / "answers" / "job_c_answers.json").read_text(encoding="utf-8"))
    (r1 / "job_c" / "zz_marker.txt").write_text("marker", encoding="utf-8")
    try:
        names = [n for n, _ in run_bench.data_units(js["C"], cfg)]
        assert "zz_marker.txt" in names
        assert "zz_marker.txt" not in [n for n, _ in run_bench.data_units(js["C"], run_bench.Config())]
    finally:
        (r1 / "job_c" / "zz_marker.txt").unlink()


def test_result_files_record_the_data_root(r1, tmp_path):
    cfg = run_bench.Config(data_root_override=r1, results_root=tmp_path, date="2026_01_01")
    spec = run_bench.RunSpec("A", "direct", 1)
    p = run_bench.write_result(cfg, spec, {"answer": "x"})
    assert json.loads(p.read_text(encoding="utf-8"))["data_root"] == str(r1)
    cfg2 = run_bench.Config(results_root=tmp_path, date="2026_01_02")
    p2 = run_bench.write_result(cfg2, spec, {"answer": "x"})
    assert json.loads(p2.read_text(encoding="utf-8"))["data_root"] == str(make_data.DATA_DIR)


def test_dry_run_sizes_from_the_given_root_and_leaves_bench_data_alone(r1, clean_env, capsys):
    assert make_data.differs(make_data.DATA_DIR, make_data.SEED) == []
    rc = run_bench.main(["--dry-run", "--env-file", str(clean_env), "--date", "2026_01_01",
                         "--results-root", str(clean_env.parent / "res"), "--data-root", str(r1)])
    out = capsys.readouterr().out
    assert rc == 0
    assert f"data root" in out and str(r1) in out
    assert make_data.differs(make_data.DATA_DIR, make_data.SEED) == []
    assert make_data.differs(r1, ROUND1) == []


def test_data_root_flag_is_validated(clean_env, tmp_path, capsys):
    rc = run_bench.main(["--dry-run", "--env-file", str(clean_env), "--data-root", str(tmp_path / "nope")])
    assert rc == 2
    assert "data root" in capsys.readouterr().out
