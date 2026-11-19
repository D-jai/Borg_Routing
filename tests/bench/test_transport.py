"""Round-2 review (5), (6), (7), (9): pinned routing settings, web search off, transport-error reruns, and the
ledger's max(cost_dollars, logged) path.

Transport error (planner condition, 17-Nov-2026) means ONLY a provider or network failure: HTTP 429, HTTP 5xx,
a connection error, a connect/read timeout. A BRT-* failure of BORG_ROUTE's own (lane refusal, truncation, a
malformed answer, a plan error), a refusal, a cut-off answer or a bad answer is never rerun.

No network and no spend: fakes and the stand-in model server only.
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import httpx
import pytest

from bench import budget, llm, run_bench
from bench.run_bench import Config, RunSpec
from tests.bench.test_runner import _install_stub, _read, bench  # noqa: F401  (the bench fixture)
from tests.e2e.stub_model_server import drain_log_writes, lane_matrix, three_tier_matrix

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
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS logs (event_type TEXT, dollars REAL)")
        conn.executemany("INSERT INTO logs VALUES ('model_call', ?)", [(d,) for d in dollars])


def entries(cfg):
    return json.loads((cfg.results_root / "spend.json").read_text(encoding="utf-8"))["entries"]


def borg_sequence(*outcomes):
    """A fake run_borg: each call takes the next ``(status, error, dollars)``; logs the dollars in its database."""
    queue = list(outcomes)
    calls = []

    async def fake(job, spec, cfg_, matrix, db_path, baseline=None):
        status, error, dollars = queue.pop(0)
        calls.append(str(db_path))
        fake_logged_db(Path(db_path), dollars)
        return {"status": status, "error": error, "cost_dollars": dollars, "answer": "[]",
                "models": [], "models_per_step": []}

    fake.calls = calls
    return fake


async def _borg(cfg, monkeypatch, *outcomes):
    fake = borg_sequence(*outcomes)
    monkeypatch.setattr(run_bench, "run_borg", fake)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m))
    return rec, fake


# -- the classifier ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("error", [
    "BRT-PIP-002: orchestration failed: BRT-ORC-001: HTTP 503 from orchestrator",
    "BRT-PIP-002: orchestration failed: BRT-ORC-001: HTTP 429 from orchestrator",
    "BRT-ANS-002: HTTP 502 from https://openrouter.ai/api/v1/chat/completions: bad gateway",
    "BRT-PIP-002: orchestration failed: BRT-ORC-001: orchestrator call timed out",
    "BRT-ANS-006: model call timed out (https://openrouter.ai/api/v1/chat/completions)",
    "BRT-PIP-002: orchestration failed: BRT-ORC-001: request error calling orchestrator: ConnectError",
    "BRT-ANS-002: request error calling https://openrouter.ai/api/v1/chat/completions: connection reset",
    "ConnectError: [Errno 11001] getaddrinfo failed",
    "ReadTimeout: timed out",
])
def test_provider_and_network_failures_are_transport(error):
    assert run_bench.is_transport_failure({"status": "failed", "error": error}) is True


@pytest.mark.parametrize("error", [
    "BRT-LANE-002: no local model clears the mid floor for a raw step",
    "BRT-ANS-009: answer still cut off even after one retry at double the cap (8000 -> 16000)",
    "BRT-ANS-002: HTTP 400 from https://openrouter.ai/api/v1/chat/completions: bad request",
    "BRT-ANS-002: HTTP 401 from https://openrouter.ai/api/v1/chat/completions: no auth",
    "BRT-ANS-002: malformed response from https://openrouter.ai (no usable choices): KeyError('choices')",
    "BRT-ANS-011: local connection pool exhausted (x); too many calls at once, not a model timeout",
    "BRT-PIP-008: run exceeded BORG_MAX_RUN_SECONDS",
    "BRT-ORC-007: no orchestrator model available",
    "direct_call_1: answer still cut off at max_tokens=16000 (finish_reason=length) after the retry",
    "direct_call_1: empty answer (finish_reason=stop)",
    "the model refused",
    None,
])
def test_brt_failures_refusals_truncations_and_bad_answers_are_not_transport(error):
    assert run_bench.is_transport_failure({"status": "failed", "error": error}) is False


def test_a_completed_run_is_never_a_transport_failure():
    assert run_bench.is_transport_failure({"status": "completed", "error": "HTTP 503"}) is False


def test_the_direct_arms_chat_error_says_whether_it_was_transport():
    req = httpx.Request("POST", "https://x")
    for code, transport in ((429, True), (500, True), (503, True), (400, False), (401, False), (404, False)):
        exc = httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, request=req))
        assert llm.is_transport_exception(exc) is transport, code
    assert llm.is_transport_exception(httpx.ConnectError("x")) is True
    assert llm.is_transport_exception(httpx.ReadTimeout("x")) is True
    assert llm.is_transport_exception(httpx.ConnectTimeout("x")) is True
    assert llm.is_transport_exception(ValueError("bad json")) is False


# -- BORG arm reruns -----------------------------------------------------------------------------------

async def test_a_borg_transport_failure_is_rerun_once_and_both_attempts_are_recorded(cfg, monkeypatch):
    rec, fake = await _borg(cfg, monkeypatch,
                            ("failed", "BRT-PIP-002: orchestration failed: BRT-ORC-001: HTTP 503 from orchestrator", 0.25),
                            ("completed", None, 0.5))
    assert len(fake.calls) == 2 and fake.calls[0] != fake.calls[1]           # each attempt its own database
    assert rec["status"] == "completed" and rec["cost_dollars"] == pytest.approx(0.5)
    (ab,) = rec["aborted_attempts"]
    assert ab["failure_kind"] == "transport" and ab["cost_dollars"] == pytest.approx(0.25)
    assert ab["db_path"] == fake.calls[0] and "HTTP 503" in ab["error"]
    assert _read(cfg, "T_borg_cloud_1")["aborted_attempts"][0]["cost_dollars"] == pytest.approx(0.25)
    es = entries(cfg)
    assert [e["dollars"] for e in es] == [pytest.approx(0.25), pytest.approx(0.5)]   # both attempts in the ledger
    assert cfg.ledger.spent() == pytest.approx(0.75)
    assert es[0].get("note") and "transport" in es[0]["note"]


async def test_a_second_transport_failure_counts_as_a_failed_run(cfg, monkeypatch):
    err = "BRT-ANS-006: model call timed out (https://openrouter.ai/api/v1/chat/completions)"
    rec, fake = await _borg(cfg, monkeypatch, ("failed", err, 0.1), ("failed", err, 0.2))
    assert len(fake.calls) == 2                                            # never a third attempt
    assert rec["status"] == "failed" and rec["failure_kind"] == "transport"
    assert rec["cost_dollars"] == pytest.approx(0.2) and len(rec["aborted_attempts"]) == 1
    assert cfg.ledger.spent() == pytest.approx(0.3)


async def test_a_brt_lane_failure_is_not_rerun(cfg, monkeypatch):
    rec, fake = await _borg(cfg, monkeypatch,
                            ("failed", "BRT-LANE-002: no local model clears the mid floor for a raw step", 0.05),
                            ("completed", None, 9.0))
    assert len(fake.calls) == 1
    assert rec["status"] == "failed" and rec["failure_kind"] == "other" and rec["aborted_attempts"] == []


async def test_a_crash_with_a_connection_error_is_rerun(cfg, monkeypatch):
    n = {"i": 0}

    async def crashing_then_ok(job, spec, cfg_, matrix, db_path, baseline=None):
        n["i"] += 1
        fake_logged_db(Path(db_path), 0.125)
        if n["i"] == 1:
            raise httpx.ConnectError("connection refused")
        return {"status": "completed", "cost_dollars": 0.125, "answer": "[]", "models": [], "models_per_step": []}

    monkeypatch.setattr(run_bench, "run_borg", crashing_then_ok)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m))
    assert n["i"] == 2 and rec["status"] == "completed" and rec["aborted_attempts"][0]["cost_dollars"] == 0.125


async def test_the_rerun_is_refused_by_the_guard_when_it_does_not_fit(cfg, monkeypatch):
    m = three_tier_matrix()
    est = run_bench.estimate_run(JOB, "borg_cloud", cfg, run_bench.baseline_model(m),
                                 run_bench.borg_price_model(m, run_bench.baseline_model(m)))
    cfg.phase_cap = est + 0.25 - 0.001        # the first attempt fits; after its US$0.25, the rerun does not
    rec, fake = await _borg(cfg, monkeypatch,
                            ("failed", "BRT-ANS-002: HTTP 500 from https://openrouter.ai: oops", 0.25),
                            ("completed", None, 0.01))
    assert rec["status"] == "refused" and len(fake.calls) == 1
    assert rec["aborted_attempts"][0]["cost_dollars"] == pytest.approx(0.25)
    assert cfg.ledger.spent() == pytest.approx(0.25)


# -- direct arm reruns ---------------------------------------------------------------------------------

def chat_sequence(*outcomes):
    """A fake chat: each call takes the next outcome: an exception to raise, or dollars to charge."""
    queue = list(outcomes)
    calls = []

    async def chat(model, system, user, max_tokens, timeout=0):
        o = queue.pop(0)
        calls.append(max_tokens)
        if isinstance(o, Exception):
            raise o
        return {"content": json.dumps([{"file": "a.txt", "risk": "high"}]), "tokens_in": 10, "tokens_out": 5,
                "dollars": o, "finish_reason": "stop", "model": model.model_id}

    chat.calls = calls
    return chat


async def test_a_direct_transport_failure_is_rerun_once(cfg):
    req = httpx.Request("POST", "https://x")
    exc = llm.ChatError("call failed: HTTPStatusError: 429", transport=True)
    chat = chat_sequence(exc, 0.25)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "direct", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m),
                                      chat=chat)
    assert len(chat.calls) == 2 and rec["status"] == "completed" and rec["cost_dollars"] == pytest.approx(0.25)
    (ab,) = rec["aborted_attempts"]
    assert ab["failure_kind"] == "transport" and ab["calls_log"] and ab["calls_log"] != rec["calls_log"]
    assert req is not None


async def test_a_direct_bad_request_is_not_rerun(cfg):
    chat = chat_sequence(llm.ChatError("call failed: HTTPStatusError: 400", transport=False), 0.25)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "direct", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m),
                                      chat=chat)
    assert len(chat.calls) == 1 and rec["status"] == "failed" and rec["aborted_attempts"] == []


async def test_llm_chat_marks_transport_errors(monkeypatch):
    """The real ``llm.chat`` sets ``transport`` from the httpx failure (no network: the client is faked)."""
    m = three_tier_matrix()
    model = run_bench.baseline_model(m)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-never-sent")

    def fake_client(status=None, exc=None):
        class C:
            def __init__(self, *a, **kw):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def post(self, url, json=None, headers=None):
                if exc:
                    raise exc
                r = httpx.Response(status, request=httpx.Request("POST", url), json={"error": "x"})
                return r
        return C

    for status, transport in ((503, True), (429, True), (400, False)):
        monkeypatch.setattr(llm.httpx, "AsyncClient", fake_client(status=status))
        with pytest.raises(llm.ChatError) as ei:
            await llm.chat(model, "s", "u", 10)
        assert ei.value.transport is transport, status
    monkeypatch.setattr(llm.httpx, "AsyncClient", fake_client(exc=httpx.ReadTimeout("slow")))
    with pytest.raises(llm.ChatError) as ei:
        await llm.chat(model, "s", "u", 10)
    assert ei.value.transport is True


# -- round-2 review (9): the ledger records max(cost_dollars, logged) ----------------------------------------

async def test_the_ledger_records_dollars_logged_outside_the_run_tree(cfg, monkeypatch):
    """The run tree says US$0.40, but the run's database logged US$0.90 in all (rows outside the tree, e.g. a
    call with no run id): the ledger must record the larger figure."""
    async def fake_borg(job, spec, cfg_, matrix, db_path, baseline=None):
        fake_logged_db(Path(db_path), 0.4, 0.5)
        return {"status": "completed", "cost_dollars": 0.4, "answer": "[]", "models": [], "models_per_step": []}

    monkeypatch.setattr(run_bench, "run_borg", fake_borg)
    m = three_tier_matrix()
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB}, cfg, m, run_bench.baseline_model(m))
    assert rec["cost_dollars"] == pytest.approx(0.4)
    assert entries(cfg)[0]["dollars"] == pytest.approx(0.9) and cfg.ledger.spent() == pytest.approx(0.9)


# -- round-2 review (5) and (6): pinned settings and web search off --------------------------------------

async def test_every_routing_setting_is_pinned_for_a_borg_run_and_recorded(bench, monkeypatch):  # noqa: F811
    cfg, mp = bench
    strays = {"ANSWERER_MAX_TOKENS": "50", "ORCHESTRATOR_MAX_TOKENS": "50", "ORCHESTRATOR_QUALITY_REQUIREMENT": "2",
              "BORG_RULE_ZERO_SHORTCUT": "false", "BORG_LANE_MAX_OUTPUT_CHARS": "9", "BORG_LANE_MAX_VERBATIM_CHARS": "9",
              "BORG_LANE_MAX_NGRAM_SHARE": "0.9", "BORG_LANE_MANIFEST": "ids", "BORG_LANE_LOCAL_ALLOWLIST": "x/y",
              "BORG_LANE_PART_CHARS": "99", "BORG_LOCAL_HOSTS": "10.0.0.9", "LEAF_QUALITY_MID": "1",
              "TIME_COST_PER_SEC": "5", "BORG_ANSWERER_CALL_TIMEOUT": "3", "BORG_ORCHESTRATOR_CALL_TIMEOUT": "3"}
    for k, v in strays.items():
        mp.setenv(k, v)
    matrix = lane_matrix()
    baseline = run_bench.baseline_model(matrix)
    _install_stub(mp)
    seen = {}
    tools = {}
    import src.pipeline.runner as runner_mod
    from src.tools.registry import TOOL_REGISTRY
    real = runner_mod.run_pipeline

    async def spy(*a, **kw):
        for k in list(strays) + ["BORG_LANE_MODE", "BORG_MAX_RUN_SECONDS"]:
            seen[k] = os.environ.get(k)
        tools["web_search_available"] = TOOL_REGISTRY["web_search"].is_available()
        return await real(*a, **kw)

    mp.setattr(runner_mod, "run_pipeline", spy)
    JOB_T = dict(JOB, arms={"borg_cloud": {"lane_mode": "by_reference"}})
    await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB_T}, cfg, matrix, baseline)
    await drain_log_writes()
    expected = {k: v for k, v in run_bench.PINNED_ENV.items()}
    for k in strays:
        assert seen[k] == expected[k], k                          # the stray value never reached the run
    assert seen["ANSWERER_MAX_TOKENS"] == "6000" and seen["ORCHESTRATOR_MAX_TOKENS"] == "6000"
    assert seen["BORG_ANSWERER_CALL_TIMEOUT"] == "7200" and seen["BORG_ORCHESTRATOR_CALL_TIMEOUT"] == "7200"
    assert seen["BORG_LANE_LOCAL_ALLOWLIST"] is None and seen["BORG_LOCAL_HOSTS"] is None
    assert tools["web_search_available"] is False                  # web search off during the run
    assert TOOL_REGISTRY["web_search"].is_available() is True      # and restored afterwards (key set by the fixture)
    rc = _read(cfg, "T_borg_cloud_1")["routing_config"]
    assert rc["tools_enabled"] is False
    for k, v in expected.items():
        assert rc["env"][k] == v, k
    assert rc["env"]["BORG_LANE_MODE"] == "by_reference" and rc["env"]["BORG_MAX_RUN_SECONDS"] == "7200"
    assert os.environ["ANSWERER_MAX_TOKENS"] == "50"                 # the caller's environment is restored


async def test_a_bench_run_cannot_reach_a_search_provider(bench, monkeypatch):  # noqa: F811
    """With web search off, a plan step that asks for web_search gets a failed tool result and no request
    leaves for Tavily or Perplexity."""
    from src.envelopes.schema import ToolRequest
    from src.tools.registry import run_tool_requests

    sent = []

    async def no_network(*a, **kw):
        sent.append(a)
        raise AssertionError("a search provider was called")

    import src.tools.web_search as ws
    monkeypatch.setattr(ws, "send_outbound", no_network, raising=False)
    monkeypatch.setenv("TAVILY_API_KEY", "tvly-test-never-sent")
    with run_bench.tools_off():
        out = await run_tool_requests([ToolRequest(tool="web_search", args={"query": "x"})])
    assert out["web_search"][0].status == "failed" and "BRT-TOOL-001" in out["web_search"][0].error
    assert sent == []


async def test_the_direct_arm_uses_the_pinned_answer_cap(cfg, monkeypatch):
    monkeypatch.setenv("ANSWERER_MAX_TOKENS", "50")
    chat = chat_sequence(0.01)
    m = three_tier_matrix()
    base = run_bench.baseline_model(m)
    rec = await run_bench.execute_run(RunSpec("T", "direct", 1), {"T": JOB}, cfg, m, base, chat=chat)
    from src.agents.executor import _answerer_caps
    monkeypatch.setenv("ANSWERER_MAX_TOKENS", run_bench.PINNED_ENV["ANSWERER_MAX_TOKENS"])
    assert chat.calls == [_answerer_caps(base, JOB["est_output_tokens"])[0]]
    assert rec["routing_config"]["tools_enabled"] is False
    assert rec["routing_config"]["env"]["ANSWERER_MAX_TOKENS"] == "6000"


def test_the_readme_states_the_pinned_values_and_the_transport_rule():
    readme = (run_bench.BENCH_DIR / "README.md").read_text(encoding="utf-8")
    for k, v in run_bench.PINNED_ENV.items():
        assert f"`{k}`" in readme, k
    for needle in ("ANSWERER_MAX_TOKENS` 6000", "Web search is off", "tools_enabled: false",
                   "Transport errors", "never a BRT-", "are **not** the product defaults", "120 s", "180 s"):
        assert needle in readme, needle
    assert "(default 2000), raised to at least 8000" not in readme
