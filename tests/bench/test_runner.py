"""bench/run_bench.py against the stand-in model server: result files, budget guard, local arms, dry run.

No network and no spend: every model call is answered by tests/e2e/stub_model_server.py.
"""
from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest

from bench import budget, llm, make_data, run_bench
from bench.run_bench import Config, RunSpec
from tests.e2e.stub_model_server import StubModels, drain_log_writes, lane_matrix, three_tier_matrix
from tests.e2e.test_privacy_lane import _LANE_ENV, EXTRACT, PLAN_RAW

ANSWER_KEY = {
    "job": "T",
    "answers": {"a.txt": {"risk": "high"}, "b.txt": {"risk": "low"}},
    "canaries": ["ACCT-CANARY-0000-1111"],
}
FINAL = json.dumps([{"file": "a.txt", "risk": "high"}, {"file": "b.txt", "risk": "low"}])

JOBS = {
    "T": {
        "job": "T", "title": "tiny", "data_folder": "job_t", "answers_file": "answers/t.json",
        "instruction": "Label each note low, medium or high risk. Return a JSON list.",
        "arms": {
            "direct": {},
            "borg_cloud": {"lane_mode": "by_reference"},
            "borg_local_workers": {"lane_mode": "raw_local", "local": True},
        },
        "repeats": 2, "est_output_tokens": 200,
        "quality": {"kind": "classification", "key_field": "file", "fields": ["risk"], "canary_check": True},
    }
}


@pytest.fixture
def bench(tmp_path, monkeypatch):
    """A tiny bench folder, a temp results root and a clean lane environment."""
    root = tmp_path / "bench"
    (root / "data" / "job_t").mkdir(parents=True)
    (root / "data" / "answers").mkdir(parents=True)
    (root / "data" / "job_t" / "a.txt").write_text("Client A. Two payments are 60 days overdue.", encoding="utf-8")
    (root / "data" / "job_t" / "b.txt").write_text("Client B. All payments on time.", encoding="utf-8")
    (root / "data" / "answers" / "t.json").write_text(json.dumps(ANSWER_KEY), encoding="utf-8")
    for name in _LANE_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-bench")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("TIME_COST_PER_SEC", "0.0001")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    cfg = Config(bench_dir=root, results_root=root / "results", date="2026_01_01", use_docker=False)
    return cfg, monkeypatch


def _install_stub(mp, **kw):
    waiting = [n for n in PLAN_RAW if n.get("depends_on")]
    sent = {"done": False}

    def replan_for(req):
        if sent["done"]:
            return {"action": "compose", "rationale": "done"}
        sent["done"] = True
        return {"action": "continue", "next_nodes": waiting, "rationale": "wave 2"}

    kw.setdefault("replan_for", replan_for)
    return StubModels(plan_nodes=PLAN_RAW, compose_text=FINAL, **kw).install(mp)


def _fake_chat(content=FINAL):
    calls = []

    async def chat(model, system, user, max_tokens, timeout=0):
        calls.append({"model": model.model_id, "user": user, "max_tokens": max_tokens})
        return {"content": content, "tokens_in": 1000, "tokens_out": 50,
                "dollars": 1000 * model.pricing.input_per_token + 50 * model.pricing.output_per_token,
                "finish_reason": "stop", "model": model.model_id}

    chat.calls = calls
    return chat


def _read(cfg, key):
    return json.loads((cfg.day_dir / f"{key}.json").read_text(encoding="utf-8"))


# -- the runner writes a result file per run ----------------------------------

async def test_runner_writes_one_result_file_per_run(bench):
    cfg, mp = bench
    matrix = lane_matrix()
    baseline = run_bench.baseline_model(matrix)
    stub = _install_stub(mp)
    chat = _fake_chat()
    runs = run_bench.build_matrix(JOBS)
    assert len(runs) == 6  # 3 arms x 2 repeats

    done = await run_bench.run_matrix(runs, JOBS, cfg, matrix, baseline, chat=chat, log=lambda *_: None)
    await drain_log_writes()

    assert sorted(p.name for p in cfg.day_dir.glob("T_*.json")) == sorted(f"{r.key}.json" for r in runs)
    assert len(done) == 6

    borg = _read(cfg, "T_borg_cloud_1")
    assert borg["status"] == "completed", borg.get("error")
    assert borg["lane_mode"] == "by_reference"
    assert borg["cost_dollars"] > 0                      # from the logged rows
    assert borg["wall_seconds"] > 0
    assert borg["tokens"]["total_tokens"] > 0
    assert borg["models"] and borg["models_per_step"]
    assert all({"step", "chosen", "dollars"} <= set(s) for s in borg["models_per_step"])
    assert borg["answer"] == FINAL
    assert borg["quality"]["accuracy"] == 1.0
    assert borg["canaries_sent_to_cloud"] == 0
    # the documents went by reference: the instruction carries no note text, the planner got a manifest
    plan_req = stub.of_kind("plan")[0]
    assert "a.txt" in plan_req.system and "Two payments are 60 days overdue" not in plan_req.system

    direct = _read(cfg, "T_direct_1")
    assert direct["status"] == "completed" and direct["models"] == [baseline.model_id]
    assert direct["cost_dollars"] == pytest.approx(1000 * baseline.pricing.input_per_token
                                                   + 50 * baseline.pricing.output_per_token)
    assert direct["quality"]["accuracy"] == 1.0
    # same text: instruction plus both notes, in one call
    assert len(chat.calls) == 2  # one per repeat
    assert "Two payments are 60 days overdue" in chat.calls[0]["user"] and "Label each note" in chat.calls[0]["user"]

    # the ledger holds exactly the logged dollars of the runs that spent
    ledger = json.loads((cfg.results_root / "spend.json").read_text(encoding="utf-8"))
    assert ledger["total_dollars"] == pytest.approx(
        sum(_read(cfg, r.key)["cost_dollars"] for r in runs))
    # no run touched the repo's matrix file
    assert not (cfg.results_root.parent / "capability_matrix.yaml").exists()


async def test_finished_runs_are_skipped_unless_forced(bench):
    cfg, mp = bench
    matrix = three_tier_matrix()
    baseline = run_bench.baseline_model(matrix)
    chat = _fake_chat()
    runs = [RunSpec("T", "direct", 1)]
    await run_bench.run_matrix(runs, JOBS, cfg, matrix, baseline, chat=chat, log=lambda *_: None)
    await run_bench.run_matrix(runs, JOBS, cfg, matrix, baseline, chat=chat, log=lambda *_: None)
    assert len(chat.calls) == 1
    cfg.force = True
    await run_bench.run_matrix(runs, JOBS, cfg, matrix, baseline, chat=chat, log=lambda *_: None)
    assert len(chat.calls) == 2


async def test_direct_arm_uses_the_fewest_calls_that_fit(bench):
    cfg, mp = bench
    matrix = three_tier_matrix()
    baseline = run_bench.baseline_model(matrix)
    baseline.context_length = 1500  # room for a single note per call, not two
    chat = _fake_chat(json.dumps([{"file": "a.txt", "risk": "high"}]))
    rec = await run_bench.run_direct(JOBS["T"], RunSpec("T", "direct", 1), cfg, baseline, chat=chat)
    assert len(chat.calls) == 2 and rec["status"] == "completed"
    assert rec["model_calls"] == 2 and rec["answer"].count("a.txt") == 2  # classification partials are joined


# -- the budget guard ------------------------------------------------------------

def test_ledger_guard_math(tmp_path):
    led = budget.Ledger(tmp_path / "spend.json")
    assert led.spent() == 0.0
    led.set_prior(5.0)
    led.record("T_direct_1", 1.25)
    led.record("T_direct_1", 1.5)  # same key replaces, never doubles
    assert led.spent() == pytest.approx(6.5)
    led.check(13.5)  # exactly at the cap is allowed
    with pytest.raises(budget.BudgetRefused):
        led.check(13.51)


async def test_over_budget_run_is_refused_and_nothing_is_sent(bench):
    cfg, mp = bench
    matrix = lane_matrix()
    baseline = run_bench.baseline_model(matrix)
    stub = _install_stub(mp)
    chat = _fake_chat()
    cfg.ledger.set_prior(19.9999)

    runs = [RunSpec("T", "borg_cloud", 1), RunSpec("T", "direct", 1)]
    done = await run_bench.run_matrix(runs, JOBS, cfg, matrix, baseline, chat=chat, log=lambda *_: None)

    assert [r["status"] for r in done] == ["refused"], "the matrix stops at the first refusal"
    assert stub.requests == [] and chat.calls == []
    rec = _read(cfg, "T_borg_cloud_1")
    assert rec["status"] == "refused" and "US$20.00 cap" in rec["reason"]
    assert cfg.ledger.spent() == pytest.approx(19.9999)  # nothing added
    # a refused run is not "done": with budget back, it can run
    assert not run_bench._is_done(cfg, runs[0])


async def test_run_that_fits_is_allowed_and_ledger_grows(bench):
    cfg, mp = bench
    matrix = three_tier_matrix()
    baseline = run_bench.baseline_model(matrix)
    cfg.ledger.set_prior(1.0)
    rec = await run_bench.execute_run(RunSpec("T", "direct", 1), JOBS, cfg, matrix, baseline, chat=_fake_chat())
    assert rec["status"] == "completed"
    assert cfg.ledger.spent() == pytest.approx(1.0 + rec["cost_dollars"])


# -- local arms -------------------------------------------------------------------

async def test_local_arms_write_not_run_with_the_reason_and_no_number(bench):
    cfg, mp = bench
    matrix = lane_matrix()  # a local model IS checked: skipping is the flag, not the matrix
    baseline = run_bench.baseline_model(matrix)
    stub = _install_stub(mp)
    assert cfg.skip_local is True  # on by default

    rec = await run_bench.execute_run(RunSpec("T", "borg_local_workers", 1), JOBS, cfg, matrix, baseline)
    out = _read(cfg, "T_borg_local_workers_1")
    assert out["status"] == "not run" and "could not be loaded" in out["reason"]
    assert out["cost_dollars"] == 0.0 and out["answer"] is None and out["quality"] is None
    assert out["wall_seconds"] is None and out["tokens"] is None
    assert stub.requests == []
    assert cfg.ledger.spent() == 0.0
    assert rec["status"] == "not run"


async def test_local_arm_without_a_checked_local_model_is_not_run_even_when_not_skipped(bench):
    cfg, mp = bench
    cfg.skip_local = False
    matrix = three_tier_matrix()  # cloud only
    baseline = run_bench.baseline_model(matrix)
    stub = _install_stub(mp)
    await run_bench.execute_run(RunSpec("T", "borg_local_workers", 1), JOBS, cfg, matrix, baseline)
    out = _read(cfg, "T_borg_local_workers_1")
    assert out["status"] == "not run" and "no checked local model" in out["reason"]
    assert stub.requests == []


async def test_local_arm_runs_in_raw_local_when_enabled_and_a_local_model_is_checked(bench):
    cfg, mp = bench
    cfg.skip_local = False
    matrix = lane_matrix()
    baseline = run_bench.baseline_model(matrix)
    stub = _install_stub(mp)
    rec = await run_bench.execute_run(RunSpec("T", "borg_local_workers", 1), JOBS, cfg, matrix, baseline)
    await drain_log_writes()
    assert rec["lane_mode"] == "raw_local"
    # every request that carried a document went to the local stand-in server
    doc_reqs = [r for r in stub.of_kind("answerer") if EXTRACT in r.system]
    assert doc_reqs and all(r.is_local for r in doc_reqs)


# -- dry run, jobs, scoring, shrink rules -----------------------------------------

def test_real_jobs_load_with_the_arms_after_ruling_2():
    jobs = run_bench.load_jobs(run_bench.BENCH_DIR / "jobs")
    assert set(jobs) == {"A", "B", "C", "D"}
    arms = {k: list(j["arms"]) for k, j in jobs.items()}
    assert arms["A"] == ["direct", "borg_cloud", "borg_local_workers"]
    assert arms["B"] == ["direct", "borg_cloud"]
    assert arms["C"] == ["direct", "borg_cloud"]
    assert arms["D"] == ["borg_cloud", "borg_local_workers"]
    for k in "ACD":
        assert jobs[k]["arms"]["borg_cloud"]["lane_mode"] == "by_reference"
    assert jobs["A"]["arms"]["borg_local_workers"]["lane_mode"] == "raw_local"
    assert jobs["D"]["arms"]["borg_local_workers"]["lane_mode"] == "raw_local"
    for j in jobs.values():
        assert j["instruction"].strip() and j["quality"]["kind"]


def test_dry_run_prints_the_matrix_and_the_estimate(capsys):
    rc = run_bench.main(["--dry-run", "--date", "2026_01_01"])
    out = capsys.readouterr().out
    assert rc == 0
    for row in ("A   direct", "A   borg_cloud", "A   borg_local_workers", "B   direct", "C   borg_cloud",
                "D   borg_local_workers"):
        assert row in out
    assert out.count("not run") >= 2 and "27 cells = 21 to run + 6" in out
    m = [ln for ln in out.splitlines() if ln.startswith("estimate, total")][0]
    assert float(m.split("US$")[1].split()[0]) <= 14.0
    assert "within the limit" in out


def test_dry_run_over_limit_says_what_to_shrink(capsys):
    jobs = run_bench.load_jobs(run_bench.BENCH_DIR / "jobs")
    cfg = Config(date="2026_01_01", results_root=Path(tempfile.mkdtemp()))
    matrix = run_bench.load_matrix()
    baseline = run_bench.baseline_model(matrix)
    rules = {"estimate_limit_dollars": 2.0, "steps": [
        {"name": "job C once", "job_repeats": {"C": 1}}, {"name": "two repeats", "repeats": 2}]}
    rc = run_bench.dry_run(jobs, cfg, matrix, baseline, None, None, None, {}, rules)
    out = capsys.readouterr().out
    assert rc == 1 and "OVER the limit" in out
    assert "1. job C once" in out and "2. two repeats" in out and "--shrink-steps" in out


def test_shrink_rules_are_data_and_apply_in_order():
    rules = budget.load_shrink_rules(run_bench.BENCH_DIR / "shrink_rules.yaml")
    assert rules["estimate_limit_dollars"] == 14.0 and rules["phase_cap_dollars"] == 20.0
    assert rules["steps"][0]["job_repeats"] == {"C": 1}      # job C first
    assert rules["steps"][1]["repeats"] == 2                  # then repeats 3 -> 2
    jobs = run_bench.load_jobs(run_bench.BENCH_DIR / "jobs")
    full = run_bench.build_matrix(jobs)
    o1 = budget.apply_shrink(rules, 1)
    s1 = run_bench.build_matrix(jobs, repeats=o1["repeats"], job_repeats=o1["job_repeats"])
    assert len([r for r in s1 if r.job == "C"]) == 2 and len([r for r in s1 if r.job == "A"]) == 9
    o2 = budget.apply_shrink(rules, 2)
    s2 = run_bench.build_matrix(jobs, repeats=o2["repeats"], job_repeats=o2["job_repeats"])
    assert len([r for r in s2 if r.job == "A"]) == 6 and len([r for r in s2 if r.job == "C"]) == 2
    assert len(full) > len(s1) > len(s2)


def test_scoring_helpers():
    key = {"answers": {"a.txt": {"category": "billing", "urgency": 3}, "b.txt": {"category": "shipping", "urgency": 1}}}
    text = 'Here you go:\n```json\n[{"file": "a.txt", "category": "billing", "urgency": 3}, ' \
           '{"file": "email/b.txt", "category": "billing", "urgency": 1}]\n```'
    s = run_bench.score_classification(text, key, ["category", "urgency"])
    assert s["accuracy"] == 0.5 and s["per_field_accuracy"] == {"category": 0.5, "urgency": 1.0}
    assert run_bench.score_classification("no list", key, ["category"])["accuracy"] == 0.0
    ck = make_data.make_job_c.__module__  # noqa: F841 (module exists)
    facts = {"facts": [{"answer": "14 March 2027"}, {"answer": "412 loading bays"}],
             "contradictions": [{"topic": "t", "value_a": "2011", "value_b": "2014"}]}
    f = run_bench.score_facts("It ends 14 March 2027. Opened 2011 in one, 2014 in another.", facts)
    assert f == {"facts_found": 1, "facts_total": 2, "contradictions_found": 1, "contradictions_total": 1}


def test_judge_model_is_in_neither_arm():
    matrix = run_bench.load_matrix()
    baseline = run_bench.baseline_model(matrix)
    j = run_bench.pick_judge_model(matrix, baseline)
    assert not j.checked and j.model_id != baseline.model_id
    with pytest.raises(ValueError):
        run_bench.pick_judge_model(matrix, baseline, baseline.model_id)
    with pytest.raises(ValueError):
        run_bench.pick_judge_model(matrix, baseline, "openrouter/anthropic/claude-sonnet-4.6")  # checked: in the BORG arm


async def test_llm_chat_goes_through_the_stand_in_and_is_priced(bench):
    cfg, mp = bench
    matrix = three_tier_matrix()
    top = run_bench.baseline_model(matrix)
    stub = StubModels(plan_nodes=[], answer_for=lambda r: {"result": "hello"}, completion_tokens=40).install(mp)
    r = await llm.chat(top, "sys text here", "user text", 100)
    assert r["tokens_out"] == 40 and r["dollars"] == pytest.approx(
        r["tokens_in"] * top.pricing.input_per_token + 40 * top.pricing.output_per_token)
    assert stub.requests[0].body["max_tokens"] == 100


async def test_run_judge_counts_its_dollars_in_the_ledger(bench):
    cfg, mp = bench
    key = json.loads(json.dumps({
        "questions": ["Q1?"], "facts": [{"question": "Q1?", "answer": "A1 value", "doc": "d"}],
        "contradictions": [{"topic": "t", "doc_a": "d", "value_a": "1", "doc_b": "e", "value_b": "2"}]}))
    cjob = {"job": "C", "data_folder": "job_t", "answers_file": "answers/c.json", "instruction": "x",
            "arms": {"direct": {}, "borg_cloud": {}}, "quality": {"kind": "pairwise_judge"}}
    (cfg.data_root / "answers" / "c.json").write_text(json.dumps(key), encoding="utf-8")
    cfg.day_dir.mkdir(parents=True)
    for arm, text in (("direct", "A1 value, 1 versus 2"), ("borg_cloud", "no idea")):
        (cfg.day_dir / f"C_{arm}_1.json").write_text(json.dumps({"status": "completed", "answer": text}), encoding="utf-8")
    matrix = three_tier_matrix()
    from tests.e2e.stub_model_server import stub_model
    judge_row = stub_model("judge", 9.0, 1e-6, 2e-6, checked=False)
    matrix.upsert(judge_row.model_copy(update={"model_id": "openrouter/judgeco/judge"}))  # vendor outside both sides
    baseline = run_bench.baseline_model(matrix)

    async def chat(model, system, user, max_tokens, timeout=0):
        import re
        one = re.search(r"=== ANSWER 1 ===\n(.*?)\n=== END OF ANSWER 1", user, re.DOTALL).group(1)
        two = re.search(r"=== ANSWER 2 ===\n(.*?)\n=== END OF ANSWER 2", user, re.DOTALL).group(1)
        return {"content": json.dumps({"better": "1" if ("A1 value" in one) > ("A1 value" in two) else "2"}),
                "tokens_in": 100, "tokens_out": 10, "dollars": 0.5, "model": model.model_id}

    res = await run_bench.run_judge({"C": cjob}, cfg, matrix, baseline, chat=chat, log=lambda *_: None)
    assert res["status"] == "completed" and res["direct_wins"] == 1 and res["judge_models"] == ["openrouter/judgeco/judge"]
    assert cfg.ledger.spent() == pytest.approx(0.5 * 4)  # two sanity calls + two for the pair
    assert (cfg.day_dir / "judge_C.json").exists()
