"""Judge choice (ruling 11: vendor rule, two-judge mode) and the runner's judge step (max_tokens, failed runs)."""
from __future__ import annotations

import json
import re

import pytest

from bench import run_bench
from bench.run_bench import Config
from src.registry.capability_matrix import CapabilityMatrix
from tests.e2e.stub_model_server import stub_model


def row(model_id: str, score: float = 9.0, checked: bool = False, price=(1e-6, 2e-6)):
    m = stub_model("x", score, price[0], price[1], checked=checked)
    return m.model_copy(update={"model_id": model_id})


def matrix_of(*rows) -> CapabilityMatrix:
    m = CapabilityMatrix("unused_never_saved.yaml")
    for r in rows:
        m.upsert(r)
    return m


POOL = [row("openrouter/anthropic/claude-sonnet-4.6", 9.1, checked=True),
        row("openrouter/moonshotai/kimi-k2-thinking", 8.7, checked=True)]
BASE = row("openrouter/anthropic/claude-opus-5.5", 9.0)


def test_vendor_of():
    assert run_bench.vendor_of("openrouter/qwen/qwen4-max") == "qwen"
    assert run_bench.vendor_of("anthropic/claude-opus-5.5") == "anthropic"
    assert run_bench.vendor_of("ollama/qwen3:4b") == "qwen3:4b"


def test_judge_vendor_must_be_outside_the_pool_and_the_baseline():
    m = matrix_of(*POOL, BASE, row("openrouter/qwen/qwen4-max", 8.0),
                  row("openrouter/anthropic/claude-opus-4.7", 9.8),
                  row("openrouter/moonshotai/kimi-k3", 9.9))
    assert run_bench.pick_judge_model(m, BASE).model_id == "openrouter/qwen/qwen4-max"  # higher scores are banned vendors
    assert run_bench.pick_judge_model(m, BASE, "openrouter/qwen/qwen4-max").model_id == "openrouter/qwen/qwen4-max"
    with pytest.raises(ValueError, match="vendor"):
        run_bench.pick_judge_model(m, BASE, "openrouter/anthropic/claude-opus-4.7")  # baseline's vendor
    with pytest.raises(ValueError, match="vendor"):
        run_bench.pick_judge_model(m, BASE, "openrouter/moonshotai/kimi-k3")         # a pool vendor
    with pytest.raises(ValueError):
        run_bench.pick_judge_model(m, BASE, "openrouter/anthropic/claude-sonnet-4.6")  # checked: in the BORG arm


def test_no_outside_vendor_means_two_judges_one_per_side():
    m = matrix_of(*POOL, BASE, row("openrouter/anthropic/claude-opus-4.7", 9.8),
                  row("openrouter/moonshotai/kimi-k3", 9.9))
    with pytest.raises(ValueError, match="two"):
        run_bench.pick_judges(m, BASE, [])
    two = run_bench.pick_judges(m, BASE, ["openrouter/anthropic/claude-opus-4.7", "openrouter/moonshotai/kimi-k3"])
    assert [j.model_id for j in two] == ["openrouter/anthropic/claude-opus-4.7", "openrouter/moonshotai/kimi-k3"]
    with pytest.raises(ValueError, match="side"):  # both from the baseline's side
        run_bench.pick_judges(m, BASE, ["openrouter/anthropic/claude-opus-4.7", "openrouter/anthropic/claude-opus-4.7"])
    with pytest.raises(ValueError):  # a judge may never be the baseline itself
        run_bench.pick_judges(m, BASE, [BASE.model_id, "openrouter/moonshotai/kimi-k3"])


def test_two_judge_mode_is_refused_while_an_outside_vendor_exists():
    m = matrix_of(*POOL, BASE, row("openrouter/qwen/qwen4-max"), row("openrouter/anthropic/claude-opus-4.7"),
                  row("openrouter/moonshotai/kimi-k3"))
    with pytest.raises(ValueError, match="outside"):
        run_bench.pick_judges(m, BASE, ["openrouter/anthropic/claude-opus-4.7", "openrouter/moonshotai/kimi-k3"])


KEY = {"questions": ["Q1?"], "facts": [{"question": "Q1?", "answer": "A1 value", "doc": "d"}],
       "contradictions": [{"topic": "t", "doc_a": "d", "value_a": "1", "doc_b": "e", "value_b": "2"}]}


@pytest.fixture
def judged(tmp_path):
    root = tmp_path / "bench"
    (root / "data" / "answers").mkdir(parents=True)
    (root / "data" / "answers" / "c.json").write_text(json.dumps(KEY), encoding="utf-8")
    cfg = Config(bench_dir=root, results_root=root / "results", date="2026_01_01", use_docker=False)
    cfg.day_dir.mkdir(parents=True)
    job = {"job": "C", "answers_file": "answers/c.json", "instruction": "x", "repeats": 3,
           "arms": {"direct": {}, "borg_cloud": {}}, "quality": {"kind": "pairwise_judge"}}
    m = matrix_of(*POOL, BASE, row("openrouter/qwen/qwen4-max", 8.0))
    return cfg, job, m


def _write(cfg, key, status, answer):
    (cfg.day_dir / f"{key}.json").write_text(json.dumps({"status": status, "answer": answer}), encoding="utf-8")


def judge_chat():
    calls = []

    async def chat(model, system, user, max_tokens, timeout=0):
        calls.append({"model": model.model_id, "max_tokens": max_tokens})
        one = re.search(r"=== ANSWER 1 ===\n(.*?)\n=== END OF ANSWER 1", user, re.DOTALL).group(1)
        two = re.search(r"=== ANSWER 2 ===\n(.*?)\n=== END OF ANSWER 2", user, re.DOTALL).group(1)
        return {"content": json.dumps({"better": "1" if ("A1 value" in one) >= ("A1 value" in two) else "2"}),
                "tokens_in": 100, "tokens_out": 10, "dollars": 0.5, "finish_reason": "stop", "model": model.model_id}

    chat.calls = calls
    return chat


async def test_run_judge_uses_an_adequate_cap_and_counts_failed_runs(judged):
    cfg, job, m = judged
    _write(cfg, "C_direct_1", "completed", "A1 value; 1 versus 2")
    _write(cfg, "C_borg_cloud_1", "completed", "no idea")
    _write(cfg, "C_direct_2", "completed", "A1 value")
    _write(cfg, "C_borg_cloud_2", "failed", "")
    _write(cfg, "C_direct_3", "failed", "")
    _write(cfg, "C_borg_cloud_3", "completed", "A1 value")
    chat = judge_chat()
    res = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=chat, log=lambda *_: None)
    assert res["status"] == "completed" and res["judge_models"] == ["openrouter/qwen/qwen4-max"]
    assert all(c["max_tokens"] == run_bench.JUDGE_MAX_TOKENS >= 4000 for c in chat.calls)
    assert len(chat.calls) == 4  # sanity (2) + the one pair that needed a judge (2)
    assert res["comparisons"] == 3 and res["direct_wins"] == 2 and res["borg_wins"] == 1
    assert res["direct_preferred_in_majority"] is True


async def test_run_judge_lists_unfinished_repeats_as_incomplete(judged):
    cfg, job, m = judged
    _write(cfg, "C_direct_1", "completed", "A1 value")
    _write(cfg, "C_borg_cloud_1", "refused", None)
    chat = judge_chat()
    res = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=chat, log=lambda *_: None)
    assert res is None and chat.calls == []  # nothing comparable: nothing judged, nothing spent
