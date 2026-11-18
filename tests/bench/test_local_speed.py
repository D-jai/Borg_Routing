"""Planner ruling 28 (f): the bench measures its checked local model with the product's own function."""
import pytest

from bench import run_bench
from src.registry import throughput


def test_the_bench_measures_its_local_model_at_start_and_prints_the_figure(monkeypatch):
    calls = []

    async def fake(url, body, timeout):
        calls.append(url)
        return {"eval_count": 24, "eval_duration": 2_000_000_000}

    monkeypatch.setattr(throughput, "_post_json", fake)
    matrix = run_bench.load_matrix(["ollama/llama3.2:3b"])
    lines = []
    figures = run_bench.measure_local_speeds(matrix, log=lines.append)
    assert figures == {"ollama/llama3.2:3b": pytest.approx(12.0)}
    assert matrix.get("ollama/llama3.2:3b").throughput_tps == pytest.approx(12.0)
    assert calls == ["http://localhost:11434/api/generate"]
    assert any("ollama/llama3.2:3b 12 tok/s (measured at start)" in line for line in lines)


def test_a_failed_bench_probe_says_not_measured():
    # tests/conftest.py blocks the real transport, so the probe fails
    matrix = run_bench.load_matrix(["ollama/llama3.2:3b"])
    lines = []
    assert run_bench.measure_local_speeds(matrix, log=lines.append) == {"ollama/llama3.2:3b": None}
    assert matrix.get("ollama/llama3.2:3b").throughput_tps is None
    assert any("not measured (probe failed, BRT-REG-016)" in line for line in lines)
