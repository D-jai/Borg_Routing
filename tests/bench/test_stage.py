"""Planner ruling 14: the staged bench plan (stage 1, the tripwire, stage 2 by whole job, cost-only decisions),
the run time limit, the fixed routing configuration, and the README that pre-registers all of it.

No network and no spend: calls are answered by a fake chat or the stand-in model server.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

from bench import aggregate, budget, run_bench
from bench.run_bench import Config, RunSpec
from tests.bench.test_runner import _install_stub, _read, bench  # noqa: F401  (the bench fixture)
from tests.e2e.stub_model_server import drain_log_writes, lane_matrix, three_tier_matrix

OUT_TOKENS = {"A": 1000, "D": 2000, "B": 3000, "C": 4000}


def make_jobs(names="ADBC"):
    return {
        n: {"job": n, "instruction": f"JOB-{n} do the thing.", "est_output_tokens": OUT_TOKENS[n],
            "arms": {"direct": {}, "borg_local_workers": {"lane_mode": "raw_local", "local": True}},
            "repeats": 3, "quality": {"kind": "none"}}
        for n in names
    }


class Scripted:
    """A fake chat: the dollars of each call come from ``dollars[job]`` (a list, then its last value repeats)."""

    def __init__(self, dollars):
        self.dollars = {k: list(v) for k, v in dollars.items()}
        self.jobs, self.timeouts = [], []

    async def __call__(self, model, system, user, max_tokens, timeout=0):
        job = re.search(r"JOB-(\w)", user).group(1)
        self.jobs.append(job)
        self.timeouts.append(timeout)
        q = self.dollars[job]
        d = q.pop(0) if len(q) > 1 else q[0]
        return {"content": "ok", "tokens_in": 10, "tokens_out": 5, "dollars": d,
                "finish_reason": "stop", "model": model.model_id}


@pytest.fixture
def staged(bench):  # noqa: F811
    cfg, mp = bench
    cfg.skip_local = True            # local cells are written "not run" at US$0
    matrix = three_tier_matrix()
    baseline = run_bench.baseline_model(matrix)
    jobs = make_jobs()
    est = {n: run_bench.estimate_run(jobs[n], "direct", cfg, baseline) for n in jobs}
    return cfg, jobs, matrix, baseline, est


def logs():
    out = []
    return out, out.append


# -- stage 1 ---------------------------------------------------------------------

def test_stage1_is_one_repeat_of_every_cell_cheapest_estimate_first(staged):
    cfg, jobs, matrix, baseline, est = staged
    plan = run_bench.stage1_plan(jobs, cfg, matrix, baseline)
    assert [(r.job, r.arm, r.n) for r, _ in plan] == (
        [(j, "borg_local_workers", 1) for j in "ADBC"] + [(j, "direct", 1) for j in "ADBC"])
    ests = [e for _, e in plan]
    assert ests == sorted(ests)
    assert est["A"] < est["D"] < est["B"] < est["C"]


async def test_stage1_runs_each_cell_once_in_estimate_order(staged):
    cfg, jobs, matrix, baseline, est = staged
    chat = Scripted({j: [est[j] * 0.5] for j in jobs})
    out, log = logs()
    res = await run_bench.run_stage(1, jobs, cfg, matrix, baseline, chat=chat, log=log)
    assert res["rc"] == 0
    assert chat.jobs == list("ADBC")
    assert sorted(p.name for p in cfg.day_dir.glob("*.json")) == sorted(
        [f"{j}_{a}_1.json" for j in "ADBC" for a in ("direct", "borg_local_workers")])


async def test_stage1_guard_failure_stops_the_whole_stage(staged):
    cfg, jobs, matrix, baseline, est = staged
    cfg.phase_cap = 0.5 * (est["A"] + est["D"] + est["B"]) + est["C"] - 0.0001   # C cannot fit at the end
    chat = Scripted({j: [est[j] * 0.5] for j in jobs})
    out, log = logs()
    res = await run_bench.run_stage(1, jobs, cfg, matrix, baseline, chat=chat, log=log)
    assert res["rc"] == 3 and res["stopped"] == "guard"
    assert chat.jobs == ["A", "D", "B"]                       # C refused, nothing after it ran
    assert _read(cfg, "C_direct_1")["status"] == "refused"
    assert any("stopping the stage" in line for line in out)


async def test_a_run_over_its_own_estimate_trips_the_wire_and_the_stage_continues_if_it_fits(staged):
    cfg, jobs, matrix, baseline, est = staged
    cfg.phase_cap = 20.0
    chat = Scripted({"A": [est["A"] * 3], "D": [est["D"] * 0.5], "B": [est["B"] * 0.5], "C": [est["C"] * 0.5]})
    out, log = logs()
    res = await run_bench.run_stage(1, jobs, cfg, matrix, baseline, chat=chat, log=log)
    assert res["rc"] == 0 and chat.jobs == list("ADBC")
    re1 = json.loads((cfg.day_dir / "reestimate_s1_1.json").read_text(encoding="utf-8"))
    assert re1["tripped_by"] == "A_direct_1"
    assert set(re1["remaining"]) == {"D_direct_1", "B_direct_1", "C_direct_1"}
    # none of the remaining cells is measured yet: each keeps its conservative estimate
    assert re1["remaining"]["D_direct_1"] == pytest.approx(est["D"])
    assert any("tripwire" in line for line in out)


async def test_the_tripwire_stops_when_spend_plus_the_re_estimated_remainder_does_not_fit(staged):
    cfg, jobs, matrix, baseline, est = staged
    cfg.phase_cap = est["A"] * 3 + est["D"] + est["B"] + est["C"] - 0.001
    chat = Scripted({"A": [est["A"] * 3], "D": [0.001], "B": [0.001], "C": [0.001]})
    out, log = logs()
    res = await run_bench.run_stage(1, jobs, cfg, matrix, baseline, chat=chat, log=log)
    assert res["rc"] == 5 and res["stopped"] == "tripwire"
    assert chat.jobs == ["A"]
    assert (cfg.day_dir / "reestimate_s1_1.json").exists()


def test_reestimate_uses_the_dearest_observed_run_of_the_same_cell_rounded_up_to_the_cent():
    remaining = [RunSpec("A", "direct", 3), RunSpec("A", "borg_local_workers", 3), RunSpec("B", "direct", 2)]
    conservative = {"A_direct_3": 9.0, "A_borg_local_workers_3": 9.0, "B_direct_2": 0.77}
    measured = {("A", "direct"): [0.011, 0.0301], ("A", "borg_local_workers"): [0.0]}
    got = run_bench.reestimate(remaining, conservative, measured)
    assert got["A_direct_3"] == pytest.approx(0.04)
    assert got["A_borg_local_workers_3"] == 0.0              # a run that logged US$0 counts as US$0
    assert got["B_direct_2"] == 0.77                          # unmeasured cell keeps the conservative estimate
    assert run_bench.ceil_cent(0.02) == pytest.approx(0.02) and run_bench.ceil_cent(0.0200001) == pytest.approx(0.03)


# -- stage 2 ---------------------------------------------------------------------

def test_stage2_adds_whole_jobs_in_order_a_d_b_c_and_skips_what_does_not_fit_but_tries_later_jobs():
    jobs = make_jobs()
    measured = {("A", "direct"): [1.0], ("A", "borg_local_workers"): [0.0],
                ("D", "direct"): [2.0], ("D", "borg_local_workers"): [0.0],
                ("B", "direct"): [3.0], ("B", "borg_local_workers"): [0.0],
                ("C", "direct"): [1.0], ("C", "borg_local_workers"): [0.0]}
    plan = run_bench.plan_stage2(jobs, measured, {}, spent=10.0, cap=20.0)
    assert [j["job"] for j in plan["jobs"]] == ["A", "D", "C"]
    assert [j["job"] for j in plan["skipped"]] == ["B"]
    assert [j["extra_estimate"] for j in plan["jobs"]] == [2.0, 4.0, 2.0]
    # every arm of a job gets the same repeat count (2 and 3)
    for name in "ADC":
        got = {(r.arm, r.n) for r in plan["runs"] if r.job == name}
        assert got == {(a, n) for a in ("direct", "borg_local_workers") for n in (2, 3)}
    assert not [r for r in plan["runs"] if r.job == "B"]
    # fixed order A, D, (B), C
    order = [r.job for r in plan["runs"]]
    assert order == sorted(order, key="ADBC".index)


def test_stage2_rounds_measured_cost_up_and_uses_the_conservative_estimate_for_an_unmeasured_cell():
    jobs = make_jobs("A")
    plan = run_bench.plan_stage2(jobs, {("A", "direct"): [0.011]}, {("A", "borg_local_workers"): 0.5},
                                 spent=0.0, cap=20.0)
    assert plan["jobs"][0]["extra_estimate"] == pytest.approx(2 * (0.02 + 0.5))


def _write_result(cfg, job, arm, n, cost, accuracy, answer):
    cfg.day_dir.mkdir(parents=True, exist_ok=True)
    rec = {"job": job, "arm": arm, "repeat": n, "status": "completed", "cost_dollars": cost,
           "quality": {"accuracy": accuracy, "machine_checks_passed": int(accuracy * 10)}, "answer": answer}
    (cfg.day_dir / f"{job}_{arm}_{n}.json").write_text(json.dumps(rec), encoding="utf-8")


def test_the_stage2_decision_uses_cost_only(staged):
    cfg, jobs, matrix, baseline, est = staged
    costs = {"A": 0.40, "D": 0.90, "B": 1.10, "C": 0.30}

    def decision(ahead):
        for j in jobs:
            for arm in ("direct", "borg_local_workers"):
                good = (arm == "direct") == ahead
                _write_result(cfg, j, arm, 1, costs[j] if arm == "direct" else 0.0, 0.95 if good else 0.10,
                              "answer-" + str(good))
        measured = run_bench.load_measured(cfg, jobs)
        p = run_bench.plan_stage2(jobs, measured, {}, spent=18.5, cap=20.0)
        return ([j["job"] for j in p["jobs"]], [j["job"] for j in p["skipped"]], [str(r.key) for r in p["runs"]])

    assert decision(True) == decision(False)                  # quality results swapped: same decision
    assert decision(True)[1] == ["D", "B"]                    # and it is a real decision, not "everything"


async def test_a_stage2_tripwire_re_estimates_from_the_new_dearest_run(staged):
    cfg, jobs, matrix, baseline, est = staged
    jobs = make_jobs("A")
    chat1 = Scripted({"A": [0.03]})
    await run_bench.run_stage(1, jobs, cfg, matrix, baseline, chat=chat1, log=lambda *_: None)
    chat2 = Scripted({"A": [0.06, 0.02]})                    # repeat 2 costs more than the US$0.03 estimate
    out, log = logs()
    res = await run_bench.run_stage(2, jobs, cfg, matrix, baseline, chat=chat2, log=log)
    assert res["rc"] == 0 and chat2.jobs == ["A", "A"]
    re1 = json.loads((cfg.day_dir / "reestimate_s2_1.json").read_text(encoding="utf-8"))
    assert re1["tripped_by"] == "A_direct_2"
    assert re1["remaining"]["A_direct_3"] == pytest.approx(0.06)
    assert {p.name for p in cfg.day_dir.glob("A_direct_*.json")} == {f"A_direct_{n}.json" for n in (1, 2, 3)}


def test_dry_run_stage2_prints_the_plan_and_spends_nothing(tmp_path, capsys):
    root = tmp_path / "results"
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    cfg = Config(results_root=root, date="2026_01_01")
    jobs = run_bench.load_jobs(run_bench.BENCH_DIR / "jobs")
    for name, job in jobs.items():
        for arm in job["arms"]:
            _write_result(cfg, name, arm, 1, 0.0 if "local" in arm else 0.5, 0.9, "x")
    argv = ["--dry-run", "--stage", "2", "--env-file", str(empty), "--date", "2026_01_01", "--results-root", str(root),
            "--matrix", str(run_bench.DEFAULT_MATRIX), "--baseline", "openrouter/anthropic/claude-opus-5.5",
            "--judge-model", "openrouter/qwen/qwen4-max", "--check-model", "ollama/llama3.2:3b"]
    rc = run_bench.main(argv)
    out = capsys.readouterr().out
    assert rc == 0 and "stage 2" in out and "order A, D, B, C" in out
    assert not (root / "spend.json").exists()
    assert not any(p.name.endswith("_2.json") for p in (root / "2026_01_01").glob("*.json"))


def test_dry_run_stage1_prints_the_order_without_spending(tmp_path, capsys):
    root = tmp_path / "results"
    empty = tmp_path / "empty.env"
    empty.write_text("", encoding="utf-8")
    rc = run_bench.main(["--dry-run", "--stage", "1", "--env-file", str(empty), "--date", "2026_01_01",
                         "--results-root", str(root), "--matrix", str(run_bench.DEFAULT_MATRIX),
                         "--baseline", "openrouter/anthropic/claude-opus-5.5",
                         "--judge-model", "openrouter/qwen/qwen4-max", "--check-model", "ollama/llama3.2:3b"])
    out = capsys.readouterr().out
    assert rc == 0 and "stage 1" in out and "cheapest" in out
    assert "US$14" not in out and "VERDICT" not in out
    assert not (root / "spend.json").exists()


# -- run time limit, wall time, routing configuration -----------------------------------

def test_the_run_time_limit_is_7200_seconds_for_every_arm():
    cfg = Config()
    assert cfg.max_run_seconds == 7200 and cfg.direct_timeout_s == 7200


async def test_the_direct_arm_gets_the_long_http_timeout_and_wall_time_is_recorded(staged):
    cfg, jobs, matrix, baseline, est = staged
    chat = Scripted({"A": [0.01]})
    rec = await run_bench.execute_run(RunSpec("A", "direct", 1), jobs, cfg, matrix, baseline, chat=chat)
    assert chat.timeouts == [7200]
    assert rec["wall_seconds"] is not None and rec["wall_seconds"] >= 0
    assert _read(cfg, "A_direct_1")["wall_seconds"] == rec["wall_seconds"]


JOB_T = {
    "job": "T", "data_folder": "job_t", "answers_file": "answers/t.json",
    "instruction": "Label each note low, medium or high risk. Return a JSON list.",
    "arms": {"borg_cloud": {"lane_mode": "by_reference"}}, "repeats": 1, "est_output_tokens": 200,
    "quality": {"kind": "classification", "key_field": "file", "fields": ["risk"]},
}


async def test_a_borg_run_uses_the_fixed_routing_configuration_and_writes_it_into_the_result(bench):  # noqa: F811
    cfg, mp = bench
    mp.setenv("LEAF_QUALITY_HEAVY", "3.0")          # a stray override must not reach the run
    mp.setenv("TIME_COST_PER_SEC", "0.5")
    mp.setenv("BASELINE_MODEL_ID", "something/else")
    matrix = lane_matrix()
    baseline = run_bench.baseline_model(matrix)
    _install_stub(mp)
    seen = {}
    import src.pipeline.runner as runner_mod
    real = runner_mod.run_pipeline

    async def spy(*a, **kw):
        for k in ("BORG_MAX_RUN_SECONDS", "LEAF_QUALITY_HEAVY", "LEAF_QUALITY_MID", "LEAF_QUALITY_LIGHT",
                  "TIME_COST_PER_SEC", "BASELINE_MODEL_ID", "BORG_LANE_MODE"):
            seen[k] = os.environ.get(k)
        return await real(*a, **kw)

    mp.setattr(runner_mod, "run_pipeline", spy)
    rec = await run_bench.execute_run(RunSpec("T", "borg_cloud", 1), {"T": JOB_T}, cfg, matrix, baseline)
    await drain_log_writes()
    assert seen["BORG_MAX_RUN_SECONDS"] == "7200"
    assert seen["LEAF_QUALITY_HEAVY"] is None and seen["TIME_COST_PER_SEC"] is None   # engine defaults apply
    assert seen["BASELINE_MODEL_ID"] == baseline.model_id and seen["BORG_LANE_MODE"] == "by_reference"
    rc = _read(cfg, "T_borg_cloud_1")["routing_config"]
    assert rc["leaf_floors"] == {"light": 6.0, "mid": 7.5, "heavy": 9.0}
    assert rc["time_cost_per_sec"] == 0.0001
    assert rc["pool"] == sorted(m.model_id for m in matrix.all() if m.checked)
    assert rc["lane_mode"] == "by_reference" and rc["max_run_seconds"] == 7200
    assert rc["baseline_model_id"] == baseline.model_id and rc["baseline_resolved_by_engine"] is True
    assert rec["status"] in ("completed", "failed")


def test_the_receipt_baseline_falls_back_to_the_engines_default_when_it_cannot_be_resolved():
    matrix = three_tier_matrix()
    baseline = run_bench.baseline_model(matrix)
    outside = baseline.model_copy(update={"model_id": "openrouter/not/in_the_matrix"})
    rc = run_bench.routing_config(Config(), matrix, outside, "by_reference")
    assert rc["baseline_resolved_by_engine"] is False
    assert "engine's own default" in rc["baseline_note"] and rc["baseline_model_id"] == baseline.model_id


# -- one repeat is reported as one run ---------------------------------------------------

JOB_A = {"job": "A", "arms": {"direct": {}, "borg_cloud": {}}, "quality": {"kind": "classification", "win_margin_points": 2}}


def _ok(cost, acc):
    return {"status": "completed", "cost_dollars": cost, "quality": {"accuracy": acc}}


def test_a_job_with_one_repeat_is_marked_one_run_and_reports_that_runs_numbers():
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", {1: _ok(10.0, 0.9)}, {1: _ok(6.0, 0.9)})
    assert v["evidence"] == "one run (thinner evidence)" and v["basis"] == "one run"
    assert v["base_run_cost"] == 10.0 and v["cand_run_cost"] == 6.0
    assert v["base_median_cost"] is None and v["cand_median_cost"] is None
    assert v["cost_ratio"] == pytest.approx(0.6) and v["wins"] is True


def test_a_job_with_several_repeats_still_uses_the_median():
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", {1: _ok(10, 0.9), 2: _ok(12, 0.9)},
                               {1: _ok(6, 0.9), 2: _ok(7, 0.9)})
    assert v["basis"] == "median of 2" and "evidence" not in v
    assert v["base_median_cost"] == 11 and v["cand_median_cost"] == 6.5


# -- the README --------------------------------------------------------------------------

README = (run_bench.BENCH_DIR / "README.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("needle", [
    "The decision to add or stop repeats looks at cost only, never at which arm is ahead.",
    "order A, D, B, C", "tripwire", "a guard failure is a stop", "one run (thinner evidence)",
    "BORG_MAX_RUN_SECONDS", "7200", "BRT-LANE-002", "never a reason to move a floor",
    "6.0 / 7.5 / 9.0", "--stage 1", "--stage 2",
])
def test_the_readme_pre_registers_ruling_14(needle):
    assert needle.lower() in README.lower()


def test_the_readme_no_longer_carries_the_old_preflight_rule_or_the_shrink_rules():
    assert "US$14" not in README and "--shrink-steps" not in README
    assert "superseded" in README.lower()
    assert "superseded" in (run_bench.BENCH_DIR / "shrink_rules.yaml").read_text(encoding="utf-8").lower()
