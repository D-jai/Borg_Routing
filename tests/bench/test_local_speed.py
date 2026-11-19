"""Planner ruling 28 (f): the bench measures its checked local model with the product's own function,
and the figure goes into the stage's result files and the aggregate's verdicts, not only stdout."""
import json

import pytest

from bench import aggregate, run_bench
from src.registry import throughput

LOCAL = "ollama/llama3.2:3b"


def _fake(monkeypatch, fail=False):
    calls = []

    async def fake(url, body, timeout):
        calls.append(url)
        if fail:
            raise ConnectionError("busy")
        return {"eval_count": 24, "eval_duration": 2_000_000_000}

    monkeypatch.setattr(throughput, "_post_json", fake)
    return calls


def test_the_bench_measures_its_local_model_at_start_and_prints_the_figure(monkeypatch):
    calls = _fake(monkeypatch)
    matrix = run_bench.load_matrix([LOCAL])
    lines = []
    record = run_bench.measure_local_speeds(matrix, log=lines.append)
    assert record == {LOCAL: pytest.approx(12.0)}
    assert matrix.get(LOCAL).throughput_tps == pytest.approx(12.0)
    assert calls == ["http://localhost:11434/api/generate"]
    assert any(f"{LOCAL} 12 tok/s (measured at start)" in line for line in lines)


def test_a_failed_bench_probe_says_not_measured():
    # tests/conftest.py blocks the real transport, so the probe fails
    matrix = run_bench.load_matrix([LOCAL])
    lines = []
    assert run_bench.measure_local_speeds(matrix, log=lines.append) == {LOCAL: run_bench.NOT_MEASURED}
    assert run_bench.NOT_MEASURED == "not measured, BRT-REG-016"
    assert matrix.get(LOCAL).throughput_tps is None
    assert any("not measured (probe failed, BRT-REG-016)" in line for line in lines)


@pytest.mark.asyncio
async def test_a_stage_start_failure_is_not_probed_again_by_each_run(monkeypatch):
    from src.orchestrator import engine

    calls = _fake(monkeypatch, fail=True)
    matrix = run_bench.load_matrix([LOCAL])
    await run_bench.measure_local_speeds_async(matrix, log=lambda _l: None)   # stage start
    for _run in range(3):                                                    # each run's start
        await engine._measure_local_speeds(matrix)
    assert len(calls) == 1


def test_the_figure_is_in_every_result_file_and_in_the_aggregate_verdict(tmp_path):
    cfg = run_bench.Config(results_root=tmp_path, date="2026_01_01")
    cfg.local_speed = {LOCAL: 12.0}
    job = {"job": "D", "arms": {"borg_cloud": {}, "borg_local": {"local": True}},
           "quality": {"kind": "accuracy", "margin": 2}}
    for arm in ("borg_cloud", "borg_local"):
        spec = run_bench.RunSpec(job="D", arm=arm, n=1)
        path = run_bench.write_result(cfg, spec, {"status": "completed", "cost_dollars": 0.01})
        assert json.loads(path.read_text(encoding="utf-8"))["local_speed"] == {LOCAL: 12.0}
    verdicts = aggregate.aggregate_job(job, cfg.day_dir)
    assert verdicts and all(v.get("local_speed") == {LOCAL: 12.0} for v in verdicts)
