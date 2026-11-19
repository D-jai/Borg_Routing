"""Round 3's route reasons, as the README's "What was measured" states them (planner ruling 46).

bench/results/2026_11_19/route_reasons.json is read from the run databases (git-ignored).
These tests bind the README sentence to that tracked file.
"""
import json
from pathlib import Path

ROUTES = Path(__file__).resolve().parents[2] / "bench" / "results" / "2026_11_19" / "route_reasons.json"


def _runs():
    return json.loads(ROUTES.read_text(encoding="utf-8"))["runs"]


def test_round_3_planner_named_every_step_of_jobs_a_b_and_d_and_the_router_chose_job_c():
    reasons = {}
    for run in _runs():
        for step in run["steps"]:
            reasons.setdefault(run["run"][0], set()).add(step["reason"])
    assert reasons == {"A": {"planner_pick"}, "B": {"planner_pick"}, "C": {"routed"}, "D": {"planner_pick"}}
    c_steps = [s for r in _runs() if r["run"].startswith("C_") for s in r["steps"]]
    assert len(c_steps) == 2


def test_a_routed_step_went_to_the_lowest_effective_cost_candidate():
    # the router's own rule: the cheapest candidate that clears the floor (candidates are recorded cheapest first)
    routed = [s for r in _runs() for s in r["steps"] if s["reason"] == "routed"]
    assert routed
    for s in routed:
        cleared = [c for c in s["candidates"] if c["score"] >= s["floor"]]
        assert s["chosen"] == min(cleared, key=lambda c: c["est_cost"])["model_id"]
