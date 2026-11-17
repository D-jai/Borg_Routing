"""The pre-registered win rule, applied to finished result files (bench/README.md, "How results are read").

Per job, each BORG arm (the candidate) is compared with its base arm: ``direct``
for jobs A, B and C, ``borg_cloud`` for job D's local arm.

- **Cost:** the median of ``cost_dollars`` over the arm's repeats (a job with one repeat reports that
  run's cost and is marked "one run (thinner evidence)"). Failed runs are
  in the median with the dollars they spent. The cost test holds when the
  candidate's median is at most 70% of the base's.
- **Quality:** per-run pairing. Repeat n of the base is compared with repeat n
  of the candidate, and every repeat pair is one comparison, failed runs
  included. A failed run loses to a completed one; two failed runs give no
  verdict (the pair is left out of the comparisons; it is not a tie). Between
  two completed runs: jobs A and D, a preference needs a gap of more than the
  job's margin (2 accuracy points), else a tie; job B, more machine checks
  passed, else a tie; job C, the judge's verdict for that pair. The quality test
  holds unless the base is preferred in a majority of all comparisons.
- **A cost win** also needs at least one completed candidate run and a base
  cost above zero.
- **A win** needs both tests to hold. Any unfinished repeat (missing, refused)
  means no verdict ("incomplete"); a local arm written as "not run" gives "not
  run"; a job B run without the container steps makes all of job B "void".
"""
from __future__ import annotations

import json
import statistics
from pathlib import Path
from typing import Optional

COST_SHARE = 0.70
FINISHED = ("completed", "failed")


def arm_pairs(job: dict) -> list[tuple[str, str]]:
    """``[(base arm, candidate arm)]`` for the job's default arms."""
    arms = list((job.get("arms") or {}).keys())
    if "direct" in arms:
        return [("direct", a) for a in arms if a != "direct"]
    if "borg_cloud" in arms:
        return [("borg_cloud", a) for a in arms if a != "borg_cloud"]
    return []


def _failed(rec: dict) -> bool:
    return rec.get("status") == "failed"


def compare_repeat(job: dict, base: dict, cand: dict) -> str:
    """``"base"``, ``"cand"``, ``"tie"`` or ``"none"`` (both failed: no verdict) for one repeat pair of finished
    runs (not job C)."""
    bf, cf = _failed(base), _failed(cand)
    if bf and cf:
        return "none"   # no verdict: excluded from the comparisons, never counted as a tie
    if cf:
        return "base"
    if bf:
        return "cand"
    q = job.get("quality") or {}
    bq, cq = base.get("quality") or {}, cand.get("quality") or {}
    if q.get("kind") == "machine_checks":
        b, c = int(bq.get("machine_checks_passed") or 0), int(cq.get("machine_checks_passed") or 0)
        return "base" if b > c else ("cand" if c > b else "tie")
    margin = float(q.get("win_margin_points", 2)) / 100.0
    b, c = float(bq.get("accuracy") or 0.0), float(cq.get("accuracy") or 0.0)
    if b - c > margin + 1e-12:
        return "base"
    if c - b > margin + 1e-12:
        return "cand"
    return "tie"


def compare_arms(job: dict, base_arm: str, cand_arm: str, base: dict, cand: dict,
                 judge: Optional[dict] = None) -> dict:
    """The verdict for one candidate arm against its base. ``base``/``cand``: ``{repeat: record}``."""
    out: dict = {"job": job.get("job"), "base": base_arm, "cand": cand_arm, "status": "complete",
                 "base_median_cost": None, "cand_median_cost": None, "cost_ratio": None,
                 "comparisons": 0, "base_preferred": 0, "cand_preferred": 0, "ties": 0,
                 "cost_holds": None, "quality_holds": None, "wins": None, "outcomes": {}}
    if cand and all(r.get("status") == "not run" for r in cand.values()):
        out["status"] = "not run"
        return out
    reps = sorted(set(base) | set(cand))
    unfinished = [n for n in reps
                  if (base.get(n) or {}).get("status") not in FINISHED
                  or (cand.get(n) or {}).get("status") not in FINISHED]
    if not reps or unfinished:
        out["status"] = "incomplete"
        out["unfinished_repeats"] = unfinished
        return out
    kind = (job.get("quality") or {}).get("kind")
    if kind == "machine_checks" and any(((r.get("quality") or {}).get("void"))
                                        for arm in (base, cand) for r in arm.values()):
        out["status"] = "void"
        return out

    base_cost = [float(base[n].get("cost_dollars") or 0.0) for n in reps]
    cand_cost = [float(cand[n].get("cost_dollars") or 0.0) for n in reps]
    if len(reps) == 1:
        # One repeat (ruling 14): report that run's numbers, not a median, and say the evidence is thinner.
        out["basis"], out["evidence"] = "one run", "one run (thinner evidence)"
        out["base_run_cost"], out["cand_run_cost"] = base_cost[0], cand_cost[0]
        b_cost, c_cost = base_cost[0], cand_cost[0]
    else:
        out["basis"] = f"median of {len(reps)}"
        b_cost, c_cost = statistics.median(base_cost), statistics.median(cand_cost)
        out["base_median_cost"], out["cand_median_cost"] = b_cost, c_cost
    out["aborted_attempt_dollars"] = {
        "base": sum(_aborted_dollars(base[n]) for n in reps), "cand": sum(_aborted_dollars(cand[n]) for n in reps)}
    if b_cost > 0:
        out["cost_ratio"] = c_cost / b_cost
    # A cost win needs at least one completed candidate run and a base cost above zero (round-2 review (1)):
    # an arm whose every run failed, or a comparison against a zero cost, cannot buy a win.
    cand_completed = any(cand[n].get("status") == "completed" for n in reps)
    out["cost_holds"] = bool(cand_completed and b_cost > 0 and c_cost <= COST_SHARE * b_cost)

    if kind == "pairwise_judge":
        if not judge or judge.get("status") != "completed":
            out["status"] = "incomplete"
            out["cost_holds"] = None
            out["reason"] = "the judge did not complete"
            return out
        by_id = {p["id"]: p["winner"] for p in judge.get("pairs") or []}
        name = job.get("job")
        outcomes = {}
        for n in reps:
            w = by_id.get(f"{name}_{n}")
            if w is None:
                out["status"] = "incomplete"
                out["cost_holds"] = None
                out["reason"] = f"no judge verdict for repeat {n}"
                return out
            outcomes[n] = {"direct": "base", "borg": "cand", "tie": "tie", "none": "none"}[w]
    else:
        outcomes = {n: compare_repeat(job, base[n], cand[n]) for n in reps}

    out["outcomes"] = outcomes
    out["no_verdict"] = sum(1 for v in outcomes.values() if v == "none")
    out["comparisons"] = len(outcomes) - out["no_verdict"]
    out["base_preferred"] = sum(1 for v in outcomes.values() if v == "base")
    out["cand_preferred"] = sum(1 for v in outcomes.values() if v == "cand")
    out["ties"] = sum(1 for v in outcomes.values() if v == "tie")
    if out["comparisons"] == 0:
        # every pair failed on both sides: there is nothing to compare, so no verdict for the job
        out["status"] = "no verdict"
        out["reason"] = "every repeat pair failed on both sides"
        out["quality_holds"] = None
        return out
    out["quality_holds"] = out["base_preferred"] * 2 <= out["comparisons"]
    out["wins"] = bool(out["cost_holds"] and out["quality_holds"])
    return out


def _aborted_dollars(rec: dict) -> float:
    """Real spend of the transport-failed attempts a run replaced (listed beside it, not in its cost)."""
    return sum(float(a.get("cost_dollars") or 0.0) for a in rec.get("aborted_attempts") or [])


def _load_arm(day_dir: Path, job: str, arm: str) -> dict:
    recs = {}
    for p in sorted(Path(day_dir).glob(f"{job}_{arm}_*.json")):
        tail = p.stem[len(f"{job}_{arm}_"):]
        if tail.isdigit():
            recs[int(tail)] = json.loads(p.read_text(encoding="utf-8"))
    return recs


def aggregate_job(job: dict, day_dir: Path) -> list[dict]:
    """Every verdict for one job, read from ``<day_dir>/<job>_<arm>_<n>.json`` (and ``judge_<job>.json``)."""
    name = str(job.get("job"))
    judge = None
    jp = Path(day_dir) / f"judge_{name}.json"
    if jp.exists():
        judge = json.loads(jp.read_text(encoding="utf-8"))
    return [compare_arms(job, b, c, _load_arm(day_dir, name, b), _load_arm(day_dir, name, c), judge=judge)
            for b, c in arm_pairs(job)]
