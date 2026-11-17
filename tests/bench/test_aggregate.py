"""bench/aggregate.py: the pre-registered win rule over repeats, with failed runs counted (README, "How results are read")."""
from __future__ import annotations

import pytest

from bench import aggregate

JOB_A = {"job": "A", "arms": {"direct": {}, "borg_cloud": {}, "borg_local_workers": {"local": True}},
         "quality": {"kind": "classification", "win_margin_points": 2}}
JOB_B = {"job": "B", "arms": {"direct": {}, "borg_cloud": {}}, "quality": {"kind": "machine_checks"}}
JOB_C = {"job": "C", "arms": {"direct": {}, "borg_cloud": {}}, "quality": {"kind": "pairwise_judge"}}
JOB_D = {"job": "D", "arms": {"borg_cloud": {}, "borg_local_workers": {"local": True}},
         "quality": {"kind": "classification", "win_margin_points": 2}}


def ok(cost, acc=None, checks=None, void=False):
    q = {}
    if acc is not None:
        q["accuracy"] = acc
    if checks is not None:
        q["machine_checks_passed"] = checks
    if void:
        q["void"] = True
    return {"status": "completed", "cost_dollars": cost, "quality": q}


def failed(cost):
    return {"status": "failed", "cost_dollars": cost, "quality": {"accuracy": 0.99, "machine_checks_passed": 99}}


def test_pairs_per_job():
    assert aggregate.arm_pairs(JOB_A) == [("direct", "borg_cloud"), ("direct", "borg_local_workers")]
    assert aggregate.arm_pairs(JOB_C) == [("direct", "borg_cloud")]
    assert aggregate.arm_pairs(JOB_D) == [("borg_cloud", "borg_local_workers")]


def test_accuracy_within_two_points_is_a_tie_and_beyond_is_a_preference():
    assert aggregate.compare_repeat(JOB_A, ok(1, 0.90), ok(1, 0.885)) == "tie"
    assert aggregate.compare_repeat(JOB_A, ok(1, 0.90), ok(1, 0.875)) == "base"
    assert aggregate.compare_repeat(JOB_A, ok(1, 0.80), ok(1, 0.83)) == "cand"
    assert aggregate.compare_repeat(JOB_B, ok(1, checks=7), ok(1, checks=6)) == "base"
    assert aggregate.compare_repeat(JOB_B, ok(1, checks=7), ok(1, checks=7)) == "tie"


def test_a_failed_run_is_a_quality_loss_whatever_its_score_says():
    assert aggregate.compare_repeat(JOB_A, ok(1, 0.10), failed(1)) == "base"
    assert aggregate.compare_repeat(JOB_A, failed(1), ok(1, 0.10)) == "cand"
    assert aggregate.compare_repeat(JOB_A, failed(1), failed(1)) == "tie"


def test_a_win_needs_both_cost_and_quality():
    recs = {"direct": {1: ok(10, 0.9), 2: ok(10, 0.9), 3: ok(10, 0.9)},
            "borg_cloud": {1: ok(5, 0.9), 2: ok(6, 0.89), 3: ok(7, 0.95)}}
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", recs["direct"], recs["borg_cloud"])
    assert v["status"] == "complete"
    assert v["base_median_cost"] == 10 and v["cand_median_cost"] == 6
    assert v["cost_ratio"] == pytest.approx(0.6) and v["cost_holds"] is True
    assert (v["base_preferred"], v["cand_preferred"], v["ties"], v["comparisons"]) == (0, 1, 2, 3)
    assert v["quality_holds"] is True and v["wins"] is True


def test_failed_borg_runs_lose_quality_and_keep_their_cost_in_the_median():
    d = {1: ok(10, 0.9), 2: ok(10, 0.9), 3: ok(10, 0.9)}
    b = {1: failed(9.5), 2: failed(9.0), 3: ok(1, 0.95)}
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", d, b)
    assert v["cand_median_cost"] == 9.0, "the failed runs' dollars are in the median"
    assert v["base_preferred"] == 2 and v["quality_holds"] is False and v["wins"] is False


def test_a_cheap_failure_cannot_buy_a_cost_win():
    d = {1: ok(10, 0.9), 2: ok(10, 0.9), 3: ok(10, 0.9)}
    b = {1: failed(0.1), 2: failed(0.1), 3: ok(1, 0.95)}  # cheap because they died early
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", d, b)
    assert v["cost_holds"] is True and v["quality_holds"] is False and v["wins"] is False


def test_direct_failures_count_against_direct():
    d = {1: failed(10), 2: failed(10), 3: ok(10, 0.99)}
    b = {1: ok(5, 0.5), 2: ok(5, 0.5), 3: ok(5, 0.5)}
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", d, b)
    assert v["cand_preferred"] == 2 and v["base_preferred"] == 1 and v["quality_holds"] is True


def test_unfinished_repeats_give_no_verdict():
    d = {1: ok(10, 0.9), 2: {"status": "refused", "cost_dollars": 0.0}}
    b = {1: ok(5, 0.9), 2: ok(5, 0.9)}
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", d, b)
    assert v["status"] == "incomplete" and v["wins"] is None
    v = aggregate.compare_arms(JOB_A, "direct", "borg_cloud", {1: ok(10, 0.9)}, {1: ok(5, 0.9), 2: ok(5, 0.9)})
    assert v["status"] == "incomplete"


def test_local_arm_not_run_is_reported_as_not_run():
    nr = {"status": "not run", "cost_dollars": 0.0}
    v = aggregate.compare_arms(JOB_D, "borg_cloud", "borg_local_workers", {1: ok(1, 0.9)}, {1: nr})
    assert v["status"] == "not run" and v["wins"] is None


def test_job_b_is_void_if_any_run_lacked_docker():
    d = {1: ok(10, checks=9), 2: ok(10, checks=9)}
    b = {1: ok(1, checks=9), 2: ok(1, checks=4, void=True)}
    v = aggregate.compare_arms(JOB_B, "direct", "borg_cloud", d, b)
    assert v["status"] == "void" and v["wins"] is None


def test_job_c_takes_its_comparisons_from_the_judge():
    d = {n: ok(10) for n in (1, 2, 3)}
    b = {1: ok(2), 2: failed(3), 3: ok(2)}
    judge = {"status": "completed", "pairs": [
        {"id": "C_1", "winner": "borg"}, {"id": "C_2", "winner": "direct"}, {"id": "C_3", "winner": "tie"}]}
    v = aggregate.compare_arms(JOB_C, "direct", "borg_cloud", d, b, judge=judge)
    assert (v["base_preferred"], v["cand_preferred"], v["ties"]) == (1, 1, 1)
    assert v["quality_holds"] is True and v["cand_median_cost"] == 2 and v["wins"] is True
    judge_majority = {"status": "completed", "pairs": [
        {"id": "C_1", "winner": "direct"}, {"id": "C_2", "winner": "direct"}, {"id": "C_3", "winner": "borg"}]}
    assert aggregate.compare_arms(JOB_C, "direct", "borg_cloud", d, b, judge=judge_majority)["quality_holds"] is False
    refused = aggregate.compare_arms(JOB_C, "direct", "borg_cloud", d, b, judge={"status": "refused"})
    assert refused["status"] == "incomplete" and refused["wins"] is None
    missing_pair = {"status": "completed", "pairs": [{"id": "C_1", "winner": "borg"}]}
    assert aggregate.compare_arms(JOB_C, "direct", "borg_cloud", d, b, judge=missing_pair)["status"] == "incomplete"


def test_aggregate_job_reads_result_files(tmp_path):
    import json
    for arm, costs in (("direct", (10, 10, 10)), ("borg_cloud", (5, 6, 7))):
        for n, c in enumerate(costs, 1):
            (tmp_path / f"A_{arm}_{n}.json").write_text(json.dumps(ok(c, 0.9)), encoding="utf-8")
        (tmp_path / f"A_borg_local_workers_{n}.json").write_text(
            json.dumps({"status": "not run", "cost_dollars": 0.0}), encoding="utf-8")
    out = aggregate.aggregate_job(JOB_A, tmp_path)
    assert out[0]["cand"] == "borg_cloud" and out[0]["wins"] is True
    assert out[1]["cand"] == "borg_local_workers" and out[1]["status"] == "not run"
