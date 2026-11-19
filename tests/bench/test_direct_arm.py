"""Direct arm: the same output-cap policy as BORG answerers, finish_reason recorded, cut-off or empty = failed.

No network: the model calls are scripted fakes.
"""
from __future__ import annotations

import json

import pytest

from bench import llm, run_bench
from bench.run_bench import Config, RunSpec
from tests.e2e.stub_model_server import stub_model

FINAL = json.dumps([{"file": "a.txt", "risk": "high"}])

JOB = {
    "job": "T", "data_folder": "job_t", "answers_file": "answers/t.json",
    "instruction": "Label each note. Return a JSON list.",
    "arms": {"direct": {}}, "repeats": 1, "est_output_tokens": 3000,
    "quality": {"kind": "classification", "key_field": "file", "fields": ["risk"]},
}


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    root = tmp_path / "bench"
    (root / "data" / "job_t").mkdir(parents=True)
    (root / "data" / "answers").mkdir(parents=True)
    (root / "data" / "job_t" / "a.txt").write_text("Client A. Overdue.", encoding="utf-8")
    (root / "data" / "answers" / "t.json").write_text(
        json.dumps({"answers": {"a.txt": {"risk": "high"}}}), encoding="utf-8")
    monkeypatch.delenv("ANSWERER_MAX_TOKENS", raising=False)
    return Config(bench_dir=root, results_root=root / "results", date="2026_01_01", use_docker=False)


def scripted(*replies):
    """A fake chat: each call returns the next (content, finish_reason, tokens_out)."""
    calls = []
    queue = list(replies)

    async def chat(model, system, user, max_tokens, timeout=0):
        content, finish, tout = queue.pop(0)
        calls.append({"max_tokens": max_tokens})
        return {"content": content, "tokens_in": 1000, "tokens_out": tout,
                "dollars": 1000 * model.pricing.input_per_token + tout * model.pricing.output_per_token,
                "finish_reason": finish, "model": model.model_id}

    chat.calls = calls
    return chat


def reasoning_model():
    m = stub_model("thinker", 9.5, 1e-6, 4e-6)
    m.specialty_tags = ["reasoning"]
    return m


@pytest.fixture(autouse=True)
def _no_cap_override(monkeypatch):
    """main.py loads .env (ANSWERER_MAX_TOKENS=6000) when other suites import it at collection; these tests
    assume the engine default unless they set the value themselves."""
    monkeypatch.delenv("ANSWERER_MAX_TOKENS", raising=False)


def test_direct_caps_are_the_engines_answerer_caps():
    from src.agents.executor import _answerer_caps

    m = reasoning_model()
    first, retry = run_bench.direct_caps(m, 3000)
    assert (first, retry) == _answerer_caps(m, 3000)
    assert first >= 8000 and retry == min(first * 2, 16000)
    plain = stub_model("plain", 9.0, 1e-6, 4e-6)
    assert run_bench.direct_caps(plain, 3000) == _answerer_caps(plain, 3000) == (3000, 6000)


async def test_length_cut_is_retried_once_at_double_the_cap_and_both_attempts_are_recorded(cfg):
    m = reasoning_model()
    chat = scripted(("[{\"file\": \"a.t", "length", 8000), (FINAL, "stop", 9000))
    rec = await run_bench.run_direct(JOB, RunSpec("T", "direct", 1), cfg, m, chat=chat)
    assert [c["max_tokens"] for c in chat.calls] == [8000, 16000]
    assert rec["status"] == "completed" and rec["answer"] == FINAL
    step = rec["models_per_step"][0]
    assert step["finish_reason"] == "stop"
    assert [a["finish_reason"] for a in step["attempts"]] == ["length", "stop"]
    assert [a["max_tokens"] for a in step["attempts"]] == [8000, 16000]
    assert [a["tokens_out"] for a in step["attempts"]] == [8000, 9000]
    both = sum(a["dollars"] for a in step["attempts"])
    assert rec["cost_dollars"] == pytest.approx(both) and step["dollars"] == pytest.approx(both)
    assert rec["tokens"]["completion_tokens"] == 17000


async def test_still_cut_off_after_the_retry_is_a_failed_run_with_its_cost(cfg):
    m = reasoning_model()
    chat = scripted(("[{", "length", 8000), ("[{\"file\"", "length", 16000))
    rec = await run_bench.run_direct(JOB, RunSpec("T", "direct", 1), cfg, m, chat=chat)
    assert rec["status"] == "failed" and "length" in rec["error"]
    assert rec["cost_dollars"] > 0 and len(chat.calls) == 2


async def test_empty_answer_is_a_failed_run(cfg):
    m = stub_model("plain", 9.0, 1e-6, 4e-6)
    chat = scripted(("   ", "stop", 10))
    rec = await run_bench.run_direct(JOB, RunSpec("T", "direct", 1), cfg, m, chat=chat)
    assert rec["status"] == "failed" and "empty" in rec["error"]
    assert rec["models_per_step"][0]["finish_reason"] == "stop"


async def test_no_retry_when_the_first_cap_is_at_the_ceiling(cfg, monkeypatch):
    monkeypatch.setenv("ANSWERER_MAX_TOKENS", "16000")
    m = stub_model("plain", 9.0, 1e-6, 4e-6)
    chat = scripted(("[{", "length", 16000))
    rec = await run_bench.run_direct(JOB, RunSpec("T", "direct", 1), cfg, m, chat=chat)
    assert len(chat.calls) == 1 and rec["status"] == "failed"


async def test_chat_capped_keeps_first_attempt_dollars_when_the_retry_errors():
    m = reasoning_model()
    n = {"i": 0}

    async def chat(model, system, user, max_tokens, timeout=0):
        n["i"] += 1
        if n["i"] == 2:
            raise llm.ChatError("boom", dollars=0.0)
        return {"content": "x", "tokens_in": 10, "tokens_out": 8000, "dollars": 0.25,
                "finish_reason": "length", "model": model.model_id}

    with pytest.raises(llm.ChatError) as ei:
        await llm.chat_capped(m, "s", "u", 100, send=chat)
    assert ei.value.dollars == pytest.approx(0.25)
