"""Ruling 22: the receipt's comparison figure is a floor, ``direct_floor_dollars``.

    (operator message chars + every document's chars, each once) / 4 x baseline input price
    + (final answer chars) / 4 x baseline output price

The expected values below are pinned from the round-1 stored data (bench/data and the direct
arm's answer in bench/results/2026_11_17). Planner ruling 22a fixed the output basis at the
answer's chars / 4; if the planner picks another basis, change ONLY the EXPECTED_* values and
the formula in ``src/registry/cost.py``.
"""
import json
import pathlib
from types import SimpleNamespace

import pytest

from src.registry.cost import (
    DIRECT_FLOOR_RATIO_RANGE,
    direct_floor_dollars,
)
from src.registry.models import ModelInfo, Pricing

# Baseline prices as recorded in round 1 (anthropic/claude-opus-5.5): US$4 / US$20 per million.
OPUS = ModelInfo(model_id="openrouter/anthropic/claude-opus-5.5", source="openrouter",
                 endpoint="https://openrouter.ai/api", context_length=200000,
                 pricing=Pricing(input_per_token=4e-6, output_per_token=20e-6), checked=True)

# Char counts only (the fixture): operator message + documents once, and the final answer.
FIXTURE = {
    "A": {"input_chars": 31_598, "answer_chars": 12_279, "real_direct": 0.208472},
    "B": {"input_chars": 2_009, "answer_chars": 14_670, "real_direct": 0.328144},
    "C": {"input_chars": 595_959, "answer_chars": 1_049, "real_direct": 0.7977},
}
EXPECTED_FLOOR_A = 0.0930
EXPECTED_FLOOR_C = 0.6012
EXPECTED_FLOOR_B = 0.0754


def _floor(job):
    f = FIXTURE[job]
    return direct_floor_dollars(f["input_chars"], f["answer_chars"], OPUS)


def test_round1_floor_job_a():
    assert _floor("A") == pytest.approx(EXPECTED_FLOOR_A, abs=5e-5)


def test_round1_floor_job_c():
    assert _floor("C") == pytest.approx(EXPECTED_FLOOR_C, abs=5e-5)


def test_round1_floor_job_b():
    assert _floor("B") == pytest.approx(EXPECTED_FLOOR_B, abs=5e-5)


def test_the_published_ratio_range_is_the_range_of_the_three_round_1_jobs():
    ratios = [FIXTURE[j]["real_direct"] / _floor(j) for j in "ABC"]
    lo, hi = DIRECT_FLOOR_RATIO_RANGE
    assert round(min(ratios), 1) == lo and round(max(ratios), 1) == hi
    assert (lo, hi) == (1.3, 4.4)


def test_a_floor_is_below_the_real_direct_cost_on_every_round_1_job():
    for j in "ABC":
        assert _floor(j) < FIXTURE[j]["real_direct"]


def test_no_baseline_or_no_price_is_unknown_not_zero():
    assert direct_floor_dollars(100, 100, None) is None
    local = ModelInfo(model_id="ollama/x", source="ollama", endpoint="http://127.0.0.1:11434",
                      context_length=8192, checked=True)
    assert direct_floor_dollars(100, 100, local) is None


def test_the_fixture_char_counts_match_the_committed_round_1_data():
    """Guards the fixture: the char counts are what bench/data and the stored answers really hold."""
    root = pathlib.Path(__file__).resolve().parents[2]
    if not (root / "bench" / "data" / "job_a").is_dir():
        pytest.skip("bench data not on disk")
    import yaml
    from bench import run_bench as rb
    cfg = SimpleNamespace(data_root=root / "bench" / "data", bench_dir=root / "bench")
    for j in "ABC":
        job = yaml.safe_load((root / "bench" / "jobs" / f"job_{j.lower()}.yaml").read_text(encoding="utf-8"))
        chars = len(rb.instruction_of(job, cfg)) + sum(len(t) for _n, t in rb.data_units(job, cfg))
        ans = json.loads((root / "bench" / "results" / "2026_11_17" / f"{j}_direct_1.json").read_text(encoding="utf-8"))
        assert chars == FIXTURE[j]["input_chars"], j
        assert len(ans["answer"]) == FIXTURE[j]["answer_chars"], j
