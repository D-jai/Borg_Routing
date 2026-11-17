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
    assert run_bench.vendor_of("ollama/qwen3:4b") == "qwen"
    assert run_bench.vendor_of("ollama/llama3.2:3b") == "meta"
    assert run_bench.vendor_of("openrouter/meta-llama/llama-4-maverick") == "meta"
    assert run_bench.vendor_of("ollama/someodd2:7b") == "someodd"


def test_a_meta_judge_is_refused_while_a_local_llama_is_in_the_pool():
    local = row("ollama/llama3.2:3b", 7.0, checked=True)
    meta = row("openrouter/meta-llama/llama-4-maverick", 9.5)
    m = matrix_of(*POOL, local, BASE, meta, row("openrouter/qwen/qwen4-max", 8.0))
    with pytest.raises(ValueError, match="vendor 'meta'"):
        run_bench.pick_judge_model(m, BASE, meta.model_id)
    assert run_bench.pick_judge_model(m, BASE).model_id == "openrouter/qwen/qwen4-max"   # meta is not picked either


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


# -- ruling 14 adjustment: repeat-1 verdicts are kept and reused, never re-judged in stage 2 ------------------

async def test_stage2_judging_reuses_the_repeat_1_verdict_and_judges_only_new_pairs(judged):
    cfg, job, m = judged
    _write(cfg, "C_direct_1", "completed", "A1 value; 1 versus 2")
    _write(cfg, "C_borg_cloud_1", "completed", "no idea")
    first = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=judge_chat(), log=lambda *_: None)
    assert first["pairs"][0]["winner"] == "direct"
    spent_after_1 = cfg.ledger.spent()
    for n in (2, 3):
        _write(cfg, f"C_direct_{n}", "completed", "no idea")
        _write(cfg, f"C_borg_cloud_{n}", "completed", "A1 value")
    chat = judge_chat()
    res = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=chat, log=lambda *_: None)
    assert len(chat.calls) == 2 + 2 * 2               # sanity + two new pairs; repeat 1 is not judged again
    assert [p["id"] for p in res["pairs"]] == ["C_1", "C_2", "C_3"]
    assert res["pairs"][0] == first["pairs"][0]       # the repeat-1 verdict is the stored one, unchanged
    assert res["reused_pairs"] == ["C_1"] and res["status"] == "completed"
    assert (res["direct_wins"], res["borg_wins"], res["comparisons"]) == (1, 2, 3)
    assert res["cost_dollars"] == pytest.approx(first["cost_dollars"] + 6 * 0.5)
    assert cfg.ledger.spent() == pytest.approx(spent_after_1 + 6 * 0.5)   # only the new calls are new spend
    again = judge_chat()
    res2 = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=again, log=lambda *_: None)
    assert again.calls == [] and res2["pairs"] == res["pairs"]   # nothing new: nothing is judged or paid


async def test_a_new_judging_failure_keeps_the_stored_verdicts(judged):
    cfg, job, m = judged
    _write(cfg, "C_direct_1", "completed", "A1 value")
    _write(cfg, "C_borg_cloud_1", "completed", "no idea")
    first = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=judge_chat(), log=lambda *_: None)
    _write(cfg, "C_direct_2", "completed", "A1 value")
    _write(cfg, "C_borg_cloud_2", "completed", "x")

    async def biased(model, system, user, max_tokens, timeout=0):
        return {"content": '{"better": "2"}', "tokens_in": 1, "tokens_out": 1, "dollars": 0.1,
                "finish_reason": "stop", "model": model.model_id}

    res = await run_bench.run_judge({"C": job}, cfg, m, BASE, chat=biased, log=lambda *_: None)
    assert res["status"] == "refused"
    assert res["pairs"] == first["pairs"]            # the stored repeat-1 verdict is not lost


# -- round-2 review (10): a judge failure exits non-zero ------------------------------------------------------

@pytest.mark.parametrize("status", ["refused", "failed"])
def test_a_judge_failure_or_sanity_failure_exits_non_zero(tmp_path, monkeypatch, capsys, status):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-never-sent")

    async def fake_run_judge(*a, **kw):
        return {"status": status, "reason": "judge sanity case failed" if status == "refused" else "boom"}

    monkeypatch.setattr(run_bench, "run_judge", fake_run_judge)
    rc = run_bench.main(["--judge-only", "--env-file", str(empty), "--date", "2026_01_01",
                         "--results-root", str(tmp_path / "results"), "--matrix", str(run_bench.DEFAULT_MATRIX),
                         "--baseline", "openrouter/anthropic/claude-opus-5.5",
                         "--judge-model", "openrouter/qwen/qwen4-max"])
    assert rc == run_bench.JUDGE_FAILED_RC != 0
    assert "judge" in capsys.readouterr().out


def test_a_completed_judge_exits_zero(tmp_path, monkeypatch):
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-never-sent")

    async def fake_run_judge(*a, **kw):
        return {"status": "completed"}

    monkeypatch.setattr(run_bench, "run_judge", fake_run_judge)
    rc = run_bench.main(["--judge-only", "--env-file", str(empty), "--date", "2026_01_01",
                         "--results-root", str(tmp_path / "results"), "--matrix", str(run_bench.DEFAULT_MATRIX),
                         "--baseline", "openrouter/anthropic/claude-opus-5.5",
                         "--judge-model", "openrouter/qwen/qwen4-max"])
    assert rc == 0
