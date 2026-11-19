"""The pre-flight estimate is conservative: BORG arms at the dearest checked pool prices plus per-item
prompt overhead; the judge priced at its full reasoning cap; the budget guard uses the same numbers."""
from __future__ import annotations

import json
import math

import pytest

from bench import run_bench
from bench.run_bench import Config, RunSpec
from src.registry.capability_matrix import CapabilityMatrix
from tests.e2e.stub_model_server import stub_model


def row(model_id, price, checked=True, score=9.0):
    return stub_model("x", score, price[0], price[1], checked=checked).model_copy(update={"model_id": model_id})


@pytest.fixture
def setup(tmp_path):
    root = tmp_path / "bench"
    (root / "data" / "job_t").mkdir(parents=True)
    for i in range(3):
        (root / "data" / "job_t" / f"n{i}.txt").write_text("Client note " * 20, encoding="utf-8")
    cfg = Config(bench_dir=root, results_root=root / "results", date="2026_01_01", use_docker=False)
    job = {"job": "T", "data_folder": "job_t", "instruction": "Label each note.", "est_output_tokens": 500,
           "arms": {"direct": {}, "borg_cloud": {"lane_mode": "by_reference"}}, "repeats": 1,
           "quality": {"kind": "classification", "fields": ["risk"]}}
    m = CapabilityMatrix("never_saved.yaml")
    m.upsert(row("openrouter/a/cheap", (1e-7, 2e-7)))
    m.upsert(row("openrouter/b/dear_in", (9e-6, 1e-6)))
    m.upsert(row("openrouter/c/dear_out", (1e-6, 4e-5)))
    m.upsert(row("openrouter/d/unchecked_dearest", (1e-3, 1e-3), checked=False))
    base = row("openrouter/e/base", (2e-6, 8e-6), checked=False)
    return cfg, job, m, base


def test_borg_prices_are_the_dearest_checked_pool_prices(setup):
    cfg, job, m, base = setup
    p = run_bench.borg_price_model(m, base)
    assert p.pricing.input_per_token == 9e-6 and p.pricing.output_per_token == 4e-5  # unchecked rows ignored
    empty = CapabilityMatrix("never_saved.yaml")
    assert run_bench.borg_price_model(empty, base).pricing == base.pricing


def test_prompt_overhead_is_measured_from_the_real_prompts(setup):
    from src.prompts.answerer import ANSWERER_SYSTEM_PROMPT
    from src.prompts.base import build_system_prompt

    cfg, job, m, base = setup
    text = build_system_prompt(ANSWERER_SYSTEM_PROMPT, None, None, run_bench.instruction_of(job, cfg))
    assert run_bench.prompt_overhead_tokens(job, cfg) == run_bench.tokens_of(text)
    assert run_bench.prompt_overhead_tokens(job, cfg) > 1000  # governance + answerer prompt are not small


def test_borg_estimate_adds_overhead_per_item_at_pool_prices(setup):
    cfg, job, m, base = setup
    pool = run_bench.borg_price_model(m, base)
    e3 = run_bench.estimate_run(job, "borg_cloud", cfg, base, pool)
    (cfg.data_root / "job_t" / "n3.txt").write_text("Client note " * 20, encoding="utf-8")
    e4 = run_bench.estimate_run(job, "borg_cloud", cfg, base, pool)
    item = run_bench.tokens_of(run_bench.unit_text("n3.txt", "Client note " * 20))
    overhead = run_bench.prompt_overhead_tokens(job, cfg)
    expected = (item * run_bench.BORG_INPUT_FACTOR + overhead) * pool.pricing.input_per_token
    assert e4 - e3 == pytest.approx(expected, rel=1e-3)
    assert e4 > run_bench.estimate_run(job, "borg_cloud", cfg, base, base)  # dearer than baseline pricing


def test_judge_estimate_prices_every_call_at_the_reasoning_cap(setup):
    cfg, job, m, base = setup
    jm = row("openrouter/q/judge", (1e-6, 3e-6), checked=False)
    e = run_bench.estimate_judge(job, 3, jm)
    calls = math.ceil((2 * 3 + 2) * run_bench.JUDGE_REASK_ALLOWANCE)
    assert e == pytest.approx(calls * (run_bench.JUDGE_CALL_INPUT_TOKENS * 1e-6 + run_bench.JUDGE_MAX_TOKENS * 3e-6))
    assert run_bench.estimate_judge(job, 3, [jm, jm]) == pytest.approx(2 * e)


async def test_the_guard_refuses_on_the_conservative_estimate(setup):
    cfg, job, m, base = setup
    pool = run_bench.borg_price_model(m, base)
    conservative = run_bench.estimate_run(job, "borg_cloud", cfg, base, pool)
    cheap = run_bench.estimate_run(job, "borg_cloud", cfg, base, base)
    assert conservative > cheap
    cfg.ledger.set_prior(cfg.phase_cap - (conservative + cheap) / 2)  # fits the cheap figure, not the conservative one
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": job}, cfg, m, base)
    assert rec["status"] == "refused" and rec["estimate_dollars"] == pytest.approx(conservative)


def test_matrix_estimate_uses_pool_prices_for_borg_and_baseline_for_direct(setup):
    cfg, job, m, base = setup
    jm = row("openrouter/q/judge", (1e-6, 3e-6), checked=False)
    runs = [RunSpec("T", "direct", 1), RunSpec("T", "borg_cloud", 1)]
    est = run_bench.estimate_matrix(runs, {"T": job}, cfg, base, [jm], run_bench.borg_price_model(m, base))
    assert est["per_run"]["T_direct_1"] == pytest.approx(run_bench.estimate_run(job, "direct", cfg, base))
    assert est["per_run"]["T_borg_cloud_1"] == pytest.approx(
        run_bench.estimate_run(job, "borg_cloud", cfg, base, run_bench.borg_price_model(m, base)))
