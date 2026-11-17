"""The benchmark runner (work order 3.2).

    python bench/run_bench.py --dry-run          # print the run matrix and the pre-flight estimate; spends nothing
    python bench/run_bench.py                    # run the matrix (THIS SPENDS MONEY; the lead runs it in 3.3)

One run = job x arm x repeat. Each BORG arm goes through ``run_pipeline`` with
the right ``lane_mode`` and the job's folder handed over by reference
(``BORG_DOCS_ROOT`` pointed at ``bench/data`` for the run), on a fresh temp
database. The checked models are a temporary in-memory matrix: this script never
writes ``data/capability_matrix.yaml``. The ``direct`` arm calls the baseline
model once (or the fewest calls that fit its context) with the same text.

Each run writes ``bench/results/<YYYY_MM_DD>/<job>_<arm>_<n>.json``. Spend is
kept in ``bench/results/spend.json`` from the logged dollars of each run, and a
run is refused if that sum plus the run's estimate would pass US$20.

Local arms (``borg_local_workers``, and job D's raw_local arm) are fully
supported, but ``--skip-local`` is on by default in this build: they write a
result file with status "not run" and the reason, never a number.

Exit codes: 0 ok, 1 dry-run estimate over the limit, 2 setup problem, 3 a run
was refused by the budget guard, 4 a run failed.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from bench import budget, checks, judge, llm  # noqa: E402

BENCH_DIR = REPO_ROOT / "bench"

LOCAL_NOT_RUN_REASON = (
    "local arm not run in this build: the local model could not be loaded "
    "(Ollama in Docker, qwen3:4b, timed out twice in work order 3.1, 17-Nov-2026). "
    "No number is recorded."
)

# Rough conversion used only for estimates.
CHARS_PER_TOKEN = 3.5   # conservative: filler text with many numbers tokenises densely
# Upper-bound model of what BORG_ROUTE spends: every call priced at the dearest checked pool prices
# (input and output each at their maximum), plus the answerer system prompt re-sent once per item.
BORG_INPUT_FACTOR = 1.3          # parts, planner manifest, re-sent instruction, retries
BORG_INPUT_OVERHEAD_TOKENS = 40_000
BORG_OUTPUT_FACTOR = 3.0         # reasoning models spend output tokens thinking
BORG_OUTPUT_OVERHEAD_TOKENS = 12_000
DIRECT_CONTEXT_SHARE = 0.85      # a direct call may fill this share of the model's context
DIRECT_OUTPUT_FACTOR = 3.0       # same reason, for the direct arm's priced output
JUDGE_CALL_INPUT_TOKENS = 5_000
JUDGE_CALL_OUTPUT_TOKENS = 300
JUDGE_MAX_TOKENS = 4000      # room for a reasoning judge's private thinking before the one-line verdict
JUDGE_REASK_ALLOWANCE = 1.25  # the estimate allows one re-ask for every four judge calls

LOCAL_ARMS = ("borg_local_workers",)
FINISHED = ("completed", "failed")   # statuses a comparison can use; "refused" and "not run" cannot


# --------------------------------------------------------------------------
# Config, jobs, matrix
# --------------------------------------------------------------------------

@dataclass
class Config:
    bench_dir: Path = BENCH_DIR
    results_root: Path = BENCH_DIR / "results"
    date: str = field(default_factory=lambda: datetime.now().strftime("%Y_%m_%d"))
    skip_local: bool = True
    use_docker: bool = True
    max_run_seconds: int = 1500
    judge_model_ids: list[str] = field(default_factory=list)
    force: bool = False
    phase_cap: float = budget.PHASE_CAP_DOLLARS

    @property
    def data_root(self) -> Path:
        return self.bench_dir / "data"

    @property
    def jobs_dir(self) -> Path:
        return self.bench_dir / "jobs"

    @property
    def day_dir(self) -> Path:
        return self.results_root / self.date

    @property
    def ledger(self) -> budget.Ledger:
        return budget.Ledger(self.results_root / "spend.json")


@dataclass(frozen=True)
class RunSpec:
    job: str
    arm: str
    n: int

    @property
    def key(self) -> str:
        return f"{self.job}_{self.arm}_{self.n}"


def load_jobs(jobs_dir: Path) -> dict[str, dict]:
    jobs: dict[str, dict] = {}
    for p in sorted(Path(jobs_dir).glob("*.yaml")):
        j = yaml.safe_load(p.read_text(encoding="utf-8"))
        j["_file"] = p.name
        jobs[str(j["job"])] = j
    return jobs


def load_matrix(extra_checked: list[str] = (), path: Optional[Path] = None):
    """The capability matrix read from disk, with extra rows checked in memory only. Never saved."""
    from src.registry.capability_matrix import CapabilityMatrix

    matrix = CapabilityMatrix(str(path or REPO_ROOT / "data" / "capability_matrix.yaml"))
    matrix.load()
    for model_id in extra_checked:
        row = matrix.get(model_id)
        if row is None:
            raise SystemExit(f"--check-model: '{model_id}' is not in the matrix")
        row.checked = True
    return matrix


def baseline_model(matrix):
    from src.registry.cost import select_baseline_model

    return select_baseline_model(matrix, os.getenv("BASELINE_MODEL_ID"))


_SOURCES = ("openrouter", "ollama", "lmstudio", "custom_key")


def vendor_of(model_id: str) -> str:
    """The vendor part of a model id: ``openrouter/qwen/qwen4-max`` -> ``qwen``; ``anthropic/x`` -> ``anthropic``."""
    parts = [p for p in str(model_id).split("/") if p]
    if len(parts) >= 2 and parts[0] in _SOURCES:
        parts = parts[1:]
    return parts[0].lower() if parts else ""


def side_vendors(matrix, baseline) -> tuple[set[str], set[str]]:
    """``(BORG side vendors, direct side vendors)``: vendors of the checked pool, and of the baseline."""
    borg = {vendor_of(m.model_id) for m in matrix.all() if m.checked}
    direct = {vendor_of(baseline.model_id)} if baseline is not None else set()
    return borg, direct


def _judge_row(matrix, baseline, model_id: str):
    row = matrix.get(model_id)
    if row is None:
        raise ValueError(f"judge model '{model_id}' is not in the matrix")
    if row.checked or (baseline is not None and row.model_id == baseline.model_id):
        raise ValueError(f"judge model '{model_id}' is in an arm (checked or the baseline)")
    if row.pricing is None:
        raise ValueError(f"judge model '{model_id}' has no price")
    return row


def pick_judge_model(matrix, baseline, explicit: Optional[str] = None):
    """The single judge (ruling 11): a priced model in neither arm (not checked, not the baseline) whose
    vendor has no model in the checked pool and is not the baseline's vendor."""
    borg_v, direct_v = side_vendors(matrix, baseline)
    banned = borg_v | direct_v
    if explicit:
        row = _judge_row(matrix, baseline, explicit)
        if vendor_of(row.model_id) in banned:
            raise ValueError(
                f"judge model '{explicit}': vendor '{vendor_of(row.model_id)}' has a model on one side "
                f"(pool vendors {sorted(borg_v)}, baseline vendor {sorted(direct_v)}); choose another vendor, "
                "or, if none exists, two judges (--judge-model twice, one per side's vendor)")
        return row
    pool = [
        m for m in matrix.all()
        if m.source == "openrouter" and not m.checked and m.pricing is not None
        and (baseline is None or m.model_id != baseline.model_id)
        and "search" not in m.model_id and vendor_of(m.model_id) not in banned
    ]
    if not pool:
        raise ValueError("no judge vendor outside both sides is in the matrix: pass two judges "
                         "(--judge-model twice, one per side's vendor)")
    return min(pool, key=lambda m: (-m.task_scores.get("reasoning", 0.0), m.model_id))


def pick_judges(matrix, baseline, explicit: Optional[list[str]] = None) -> list:
    """One judge (the vendor rule), or, only when no outside vendor exists, two: one per side's vendor."""
    explicit = [e for e in (explicit or []) if e]
    if len(explicit) <= 1:
        return [pick_judge_model(matrix, baseline, explicit[0] if explicit else None)]
    if len(explicit) > 2:
        raise ValueError("at most two judges (one per side's vendor)")
    try:
        outside = pick_judge_model(matrix, baseline)
    except ValueError:
        outside = None
    if outside is not None:
        raise ValueError(f"a judge outside both sides exists ({outside.model_id}); two-judge mode is only "
                         "for when none does")
    rows = [_judge_row(matrix, baseline, e) for e in explicit]
    borg_v, direct_v = side_vendors(matrix, baseline)
    vs = [vendor_of(r.model_id) for r in rows]
    if not ((vs[0] in direct_v and vs[1] in borg_v) or (vs[0] in borg_v and vs[1] in direct_v)) or vs[0] == vs[1]:
        raise ValueError(f"two judges must be one per side: one from the baseline's vendor {sorted(direct_v)} "
                         f"and one from a pool vendor {sorted(borg_v)}; got {vs}")
    return rows


# --------------------------------------------------------------------------
# The run matrix
# --------------------------------------------------------------------------

def arms_of(job: dict, arms_filter: Optional[list[str]] = None) -> list[str]:
    arms = list((job.get("arms") or {}).keys())
    for extra in (job.get("optional_arms") or {}):
        if arms_filter and extra in arms_filter:
            arms.append(extra)
    if arms_filter:
        arms = [a for a in arms if a in arms_filter]
    return arms


def arm_conf(job: dict, arm: str) -> dict:
    return (job.get("arms") or {}).get(arm) or (job.get("optional_arms") or {}).get(arm) or {}


def build_matrix(
    jobs: dict[str, dict],
    only_jobs: Optional[list[str]] = None,
    arms_filter: Optional[list[str]] = None,
    repeats: Optional[int] = None,
    job_repeats: Optional[dict[str, int]] = None,
) -> list[RunSpec]:
    """Every run of the matrix, in job, arm, repeat order. ``repeats`` caps every job; ``job_repeats`` sets one job."""
    runs: list[RunSpec] = []
    for name, job in jobs.items():
        if only_jobs and name not in only_jobs:
            continue
        reps = int((job_repeats or {}).get(name, job.get("repeats", 3)))
        if repeats is not None:
            reps = min(reps, int(repeats))
        for arm in arms_of(job, arms_filter):
            for n in range(1, reps + 1):
                runs.append(RunSpec(name, arm, n))
    return runs


# --------------------------------------------------------------------------
# Texts and keys
# --------------------------------------------------------------------------

def load_answers(job: dict, cfg: Config) -> Optional[dict]:
    f = job.get("answers_file")
    if not f:
        return None
    return json.loads((cfg.data_root / f).read_text(encoding="utf-8"))


def instruction_of(job: dict, cfg: Config) -> str:
    """The short instruction BORG_ROUTE gets. Job B adds its (short) spec; doc jobs hand data over by reference."""
    text = str(job["instruction"]).strip()
    if "{questions}" in text:
        ans = load_answers(job, cfg)
        qs = "\n".join(f"{i}. {q}" for i, q in enumerate(ans["questions"], 1))
        text = text.replace("{questions}", qs)
    if job.get("spec_file"):
        spec = (cfg.bench_dir / job["spec_file"]).read_text(encoding="utf-8")
        text = f"{text}\n\n{spec.strip()}"
    return text


def data_units(job: dict, cfg: Config) -> list[tuple[str, str]]:
    """``[(relative name, text)]`` of the job's data folder in a stable order; empty for job B."""
    folder = job.get("data_folder")
    if not folder:
        return []
    root = cfg.data_root / folder
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file() and p.suffix.lower() in (".txt", ".md"):
            out.append((p.relative_to(root).as_posix(), p.read_text(encoding="utf-8")))
    return out


def unit_text(name: str, text: str) -> str:
    return f"=== FILE: {name} ===\n{text.rstrip()}\n=== END OF FILE ===\n"


def tokens_of(text: str) -> int:
    return max(1, int(len(text) / CHARS_PER_TOKEN))


# --------------------------------------------------------------------------
# Estimates (no network: prices come from the matrix rows)
# --------------------------------------------------------------------------

def _price(model, tin: float, tout: float) -> float:
    from src.registry.cost import call_dollars

    return call_dollars(model, int(tin), int(tout))


def direct_calls(job: dict, cfg: Config, model) -> int:
    units = data_units(job, cfg)
    instr = tokens_of(instruction_of(job, cfg))
    total = instr + sum(tokens_of(unit_text(n, t)) for n, t in units)
    out = int(job.get("est_output_tokens", 4000))
    room = max(1, int(model.context_length * DIRECT_CONTEXT_SHARE) - out)
    return max(1, math.ceil(total / room))


def borg_price_model(matrix, baseline):
    """A price-only row for estimating BORG arms: the highest input price and the highest output price among
    the checked, priced pool models (each taken separately). The baseline when no checked row has a price."""
    from src.registry.models import ModelInfo, Pricing

    priced = [m for m in matrix.all() if m.checked and m.pricing is not None]
    if not priced:
        return baseline
    return ModelInfo(
        model_id="(dearest checked pool prices)", source="openrouter",
        context_length=max(m.context_length for m in priced),
        pricing=Pricing(input_per_token=max(m.pricing.input_per_token for m in priced),
                        output_per_token=max(m.pricing.output_per_token for m in priced)),
    )


def prompt_overhead_tokens(job: dict, cfg: Config) -> int:
    """Tokens of the system prompt every BORG answerer call carries: governance header + answerer prompt +
    the verbatim instruction, measured from src/prompts at run time (read only)."""
    from src.prompts.answerer import ANSWERER_SYSTEM_PROMPT
    from src.prompts.base import build_system_prompt

    return tokens_of(build_system_prompt(ANSWERER_SYSTEM_PROMPT, None, None, instruction_of(job, cfg)))


def estimate_run(job: dict, arm: str, cfg: Config, baseline, borg_model=None) -> float:
    """Upper-bound dollars for one run. Local arms cost 0. The direct arm is priced on the baseline.
    BORG arms are priced at ``borg_model`` (``borg_price_model``: the dearest checked pool prices; the
    baseline if not given) and pay the answerer system prompt once per item (one item for job B)."""
    if arm_conf(job, arm).get("local") or arm in LOCAL_ARMS:
        return 0.0
    if baseline is None:
        raise ValueError("no checked model: nothing to price the estimate with")
    units = data_units(job, cfg)
    instr = tokens_of(instruction_of(job, cfg))
    data = sum(tokens_of(unit_text(n, t)) for n, t in units)
    out = int(job.get("est_output_tokens", 4000))
    if arm == "direct":
        calls = direct_calls(job, cfg, baseline)
        tin = data + calls * instr
        tout = out * DIRECT_OUTPUT_FACTOR * calls
        if calls > 1:  # a merge call over the partial answers
            tin += out * calls
            tout += out * DIRECT_OUTPUT_FACTOR
        return _price(baseline, tin, tout)
    items = max(1, len(units))
    tin = ((data + instr) * BORG_INPUT_FACTOR + BORG_INPUT_OVERHEAD_TOKENS
           + items * prompt_overhead_tokens(job, cfg))
    tout = out * BORG_OUTPUT_FACTOR + BORG_OUTPUT_OVERHEAD_TOKENS
    return _price(borg_model or baseline, tin, tout)


def estimate_judge(job: dict, repeats: int, judge_model) -> float:
    """Per judge: two calls per pair plus two for the sanity case, plus one re-ask in four; every call priced
    at the full ``JUDGE_MAX_TOKENS`` of output (a reasoning judge may think up to its cap).
    ``judge_model`` may be a list (two judges)."""
    models = judge_model if isinstance(judge_model, (list, tuple)) else [judge_model]
    total = 0.0
    for jm in models:
        if jm is None:
            continue
        calls = math.ceil((2 * repeats + 2) * JUDGE_REASK_ALLOWANCE)
        total += _price(jm, calls * JUDGE_CALL_INPUT_TOKENS, calls * JUDGE_MAX_TOKENS)
    return total


def estimate_matrix(runs: list[RunSpec], jobs: dict, cfg: Config, baseline, judge_model, borg_model=None) -> dict:
    per_run = {r.key: estimate_run(jobs[r.job], r.arm, cfg, baseline, borg_model) for r in runs}
    total = sum(per_run.values())
    judge_cost = 0.0
    for name, job in jobs.items():
        if (job.get("quality") or {}).get("kind") == "pairwise_judge":
            reps = max([r.n for r in runs if r.job == name and r.arm == "direct"] or [0])
            if reps:
                judge_cost += estimate_judge(job, reps, judge_model)
    return {"per_run": per_run, "runs_total": total, "judge": judge_cost, "total": total + judge_cost}


# --------------------------------------------------------------------------
# Reading a finished BORG run from its database
# --------------------------------------------------------------------------

_TREE_CTE = """WITH RECURSIVE tree(run_id) AS (
    SELECT ?
    UNION
    SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id
)"""


def read_run_tree(db_path: str, run_id: Optional[str]) -> dict:
    """Dollars and models of the root run and every descendant, read from the logged rows."""
    out: dict[str, Any] = {"dollars": 0.0, "baseline_dollars": 0.0, "models": [], "steps": [], "model_calls": 0}
    if not run_id:
        return out
    with sqlite3.connect(db_path) as conn:
        ids = [r[0] for r in conn.execute(_TREE_CTE + " SELECT run_id FROM tree", (run_id,))]
        marks = ",".join("?" for _ in ids)
        row = conn.execute(
            _TREE_CTE + """ SELECT COALESCE(SUM(dollars),0), COALESCE(SUM(baseline_dollars),0)
                FROM logs WHERE run_id IN (SELECT run_id FROM tree)
                AND event_type IN ('model_call','orchestrator_decision')""",
            (run_id,),
        ).fetchone()
        out["dollars"], out["baseline_dollars"] = float(row[0]), float(row[1])
        out["models"] = [r[0] for r in conn.execute(
            f"SELECT DISTINCT model_id FROM logs WHERE run_id IN ({marks}) AND event_type='model_call' "
            "AND model_id IS NOT NULL AND model_id NOT LIKE 'tool:%' ORDER BY model_id", ids)]
        out["model_calls"] = conn.execute(
            f"SELECT COUNT(*) FROM logs WHERE run_id IN ({marks}) AND event_type='model_call'", ids).fetchone()[0]
        step_dollars = {e: float(d) for e, d in conn.execute(
            f"SELECT envelope_id, COALESCE(SUM(dollars),0) FROM logs WHERE run_id IN ({marks}) "
            "AND event_type='model_call' AND envelope_id IS NOT NULL GROUP BY envelope_id", ids)}
        for env_id, dj in conn.execute(
                f"SELECT envelope_id, decision_json FROM logs WHERE run_id IN ({marks}) "
                "AND event_type='orchestrator_decision' AND envelope_id IS NOT NULL ORDER BY log_id", ids):
            try:
                d = json.loads(dj or "null")
            except (TypeError, ValueError):
                continue
            if isinstance(d, dict) and d.get("mode") == "route":
                out["steps"].append({
                    "step": d.get("node_id") or env_id, "task_type": d.get("task_type"), "tier": d.get("tier"),
                    "chosen": d.get("chosen"), "reason": d.get("reason"), "dollars": step_dollars.get(env_id, 0.0),
                })
    return out


def canary_sent_to_cloud(db_path: str, canaries: list[str]) -> Optional[int]:
    """How many outbound requests that left the machine carried a canary number (None if no ledger)."""
    try:
        with sqlite3.connect(db_path) as conn:
            bodies = [r[0] or "" for r in conn.execute("SELECT body FROM outbound_requests WHERE left_machine=1")]
    except sqlite3.Error:
        return None
    return sum(1 for b in bodies if any(c in b for c in canaries))


# --------------------------------------------------------------------------
# Quality measures
# --------------------------------------------------------------------------

def parse_json_list(text: str) -> list:
    """The longest JSON list of objects found in ``text`` (fences and prose around it are fine)."""
    dec = json.JSONDecoder()
    best: list = []
    i = 0
    text = text or ""
    while True:
        i = text.find("[", i)
        if i < 0:
            return best
        try:
            val, end = dec.raw_decode(text[i:])
        except ValueError:
            i += 1
            continue
        if isinstance(val, list) and sum(1 for v in val if isinstance(v, dict)) > sum(1 for v in best if isinstance(v, dict)):
            best = val
        i += max(end, 1)


def score_classification(answer: str, key: dict, fields: list[str], key_field: str = "file") -> dict:
    expected: dict = key["answers"]
    got: dict[str, dict] = {}
    for item in parse_json_list(answer):
        if isinstance(item, dict) and key_field in item:
            got[Path(str(item[key_field])).name] = item
    per_field = {f: 0 for f in fields}
    all_right = 0
    for name, truth in expected.items():
        item = got.get(name) or {}
        ok = True
        for f in fields:
            have = str(item.get(f, "")).strip().lower()
            if have == str(truth[f]).strip().lower():
                per_field[f] += 1
            else:
                ok = False
        all_right += ok
    n = len(expected)
    return {
        "accuracy": all_right / n if n else 0.0,
        "per_field_accuracy": {f: per_field[f] / n for f in fields},
        "items_expected": n,
        "items_answered": len(got),
    }


def score_facts(answer: str, key: dict) -> dict:
    low = (answer or "").lower()
    facts = [f["answer"] for f in key["facts"]]
    found = [a for a in facts if a.lower() in low]
    contr = [c["topic"] for c in key["contradictions"]
             if c["value_a"].lower() in low and c["value_b"].lower() in low]
    return {
        "facts_found": len(found), "facts_total": len(facts),
        "contradictions_found": len(contr), "contradictions_total": len(key["contradictions"]),
    }


def score_answer(job: dict, answer: str, cfg: Config, runner=None) -> dict:
    q = job.get("quality") or {}
    kind = q.get("kind")
    key = load_answers(job, cfg)
    if kind == "classification":
        return score_classification(answer, key, list(q["fields"]), q.get("key_field", "file"))
    if kind == "machine_checks":
        kw = {"runner": runner} if runner is not None else {}
        return checks.run_checks(answer, with_docker=cfg.use_docker, **kw)
    if kind == "pairwise_judge":
        return score_facts(answer, key)
    return {}


# --------------------------------------------------------------------------
# One run
# --------------------------------------------------------------------------

def result_path(cfg: Config, spec: RunSpec) -> Path:
    return cfg.day_dir / f"{spec.key}.json"


def write_result(cfg: Config, spec: RunSpec, record: dict) -> Path:
    path = result_path(cfg, spec)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {"job": spec.job, "arm": spec.arm, "repeat": spec.n, "date": cfg.date, **record}
    path.write_text(json.dumps(record, indent=1) + "\n", encoding="utf-8")
    return path


def _is_done(cfg: Config, spec: RunSpec) -> bool:
    p = result_path(cfg, spec)
    if not p.exists() or cfg.force:
        return False
    try:
        return json.loads(p.read_text(encoding="utf-8")).get("status") in ("completed", "not run", "failed")
    except ValueError:
        return False


class _Env:
    """Set environment variables for one run and restore them afterwards."""

    def __init__(self, **values: Optional[str]):
        self.values = values
        self.saved: dict[str, Optional[str]] = {}

    def __enter__(self):
        for k, v in self.values.items():
            self.saved[k] = os.environ.get(k)
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def __exit__(self, *exc):
        for k, old in self.saved.items():
            if old is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = old


async def run_borg(job: dict, spec: RunSpec, cfg: Config, matrix) -> dict:
    """One BORG_ROUTE run through ``run_pipeline`` on a fresh temp database."""
    from src.db.schema import create_all
    from src.orchestrator import engine
    from src.pipeline.runner import run_pipeline

    conf = arm_conf(job, spec.arm)
    mode = conf.get("lane_mode", "off")
    folder = job.get("data_folder")
    if folder and mode == "off":
        raise ValueError(f"job {spec.job} hands over documents; arm {spec.arm} needs a lane mode")
    db_dir = tempfile.mkdtemp(prefix="borg_bench_")
    db_path = str(Path(db_dir) / "run.db")
    await create_all(db_path)
    engine._shared_matrix = matrix
    instruction = instruction_of(job, cfg)
    documents = {"folder": folder} if folder else None

    with _Env(BORG_DOCS_ROOT=str(cfg.data_root), BORG_LANE_MODE=mode if mode != "off" else None,
              BORG_MAX_RUN_SECONDS=str(cfg.max_run_seconds)):
        t0 = time.monotonic()
        result = await run_pipeline(instruction, db_path=db_path, documents=documents)
        wall = time.monotonic() - t0

    tree = read_run_tree(db_path, result.get("run_id"))
    record = {
        "status": result.get("status", "failed"),
        "error": result.get("error"),
        "lane_mode": mode,
        "run_id": result.get("run_id"),
        "cost_dollars": tree["dollars"],
        "digest_cost_dollars": result.get("cost_dollars"),
        "baseline_dollars": tree["baseline_dollars"],
        "wall_seconds": round(wall, 3),
        "tokens": result.get("usage") or {},
        "models": tree["models"],
        "models_per_step": tree["steps"],
        "model_calls": tree["model_calls"],
        "answer": result.get("response", ""),
    }
    if (job.get("quality") or {}).get("canary_check"):
        ans = load_answers(job, cfg)
        record["canaries_sent_to_cloud"] = canary_sent_to_cloud(db_path, ans.get("canaries", []))
        record["canaries_in_answer"] = sum(1 for c in ans.get("canaries", []) if c in record["answer"])
    return record


class _DirectCut(Exception):
    """A direct-arm answer that is empty or still cut off after the retry: the run is "failed"."""


def direct_caps(model, est_output_tokens: int) -> tuple[int, Optional[int]]:
    """The direct arm's ``(first_cap, retry_cap)``: BORG answerers' policy, with the job's expected
    output as the floor (the role a step's ``max_output_tokens`` plays inside BORG_ROUTE)."""
    return llm.answerer_caps(model, est_output_tokens)


async def run_direct(job: dict, spec: RunSpec, cfg: Config, baseline, chat=llm.chat) -> dict:
    """The direct arm: the same text, one call to the baseline model, or the fewest calls that fit.

    Every call follows BORG answerers' cap policy (``direct_caps``): one retry at the
    retry cap on ``finish_reason == "length"``. An answer that is empty or still cut
    off makes the run "failed"; its dollars are kept.
    """
    instr = instruction_of(job, cfg)
    units = data_units(job, cfg)
    out = int(job.get("est_output_tokens", 4000))
    first_cap, retry_cap = direct_caps(baseline, out)
    max_tokens = retry_cap or first_cap  # room is kept for the larger of the two attempts
    room = max(1, int(baseline.context_length * DIRECT_CONTEXT_SHARE) - max_tokens - tokens_of(instr))
    chunks: list[list[tuple[str, str]]] = []
    cur: list[tuple[str, str]] = []
    used = 0
    for u in units:
        t = tokens_of(unit_text(*u))
        if cur and used + t > room:
            chunks.append(cur)
            cur, used = [], 0
        cur.append(u)
        used += t
    if cur or not chunks:
        chunks.append(cur)

    system = "You answer using only the text you are given. Follow the instruction exactly."
    steps, tin, tout, dollars = [], 0, 0, 0.0
    answers: list[str] = []
    status, error = "completed", None
    t0 = time.monotonic()

    async def call(step: str, text: str) -> str:
        nonlocal tin, tout, dollars
        try:
            r = await llm.chat_capped(baseline, system, text, out, send=chat)
        except llm.ChatError as exc:
            dollars += exc.dollars
            raise
        tin += r["tokens_in"]; tout += r["tokens_out"]; dollars += r["dollars"]
        steps.append({"step": step, "chosen": baseline.model_id, "dollars": r["dollars"],
                      "finish_reason": r["finish_reason"], "attempts": r["attempts"]})
        if r["finish_reason"] == "length":
            raise _DirectCut(f"{step}: answer still cut off at max_tokens={r['attempts'][-1]['max_tokens']} "
                             "(finish_reason=length) after the retry")
        if not r["content"].strip():
            raise _DirectCut(f"{step}: empty answer (finish_reason={r['finish_reason'] or 'none'})")
        return r["content"]

    try:
        for i, chunk in enumerate(chunks, 1):
            body = instr + ("\n\n" + "\n".join(unit_text(*u) for u in chunk) if chunk else "")
            answers.append(await call(f"direct_call_{i}", body))
        if len(answers) == 1:
            final = answers[0]
        elif (job.get("quality") or {}).get("kind") == "classification":
            merged: list = []
            for a in answers:
                merged.extend(parse_json_list(a))
            final = json.dumps(merged)
        else:
            merge_prompt = (
                f"{instr}\n\nThe documents were too long for one call, so they were read in {len(answers)} parts. "
                "These are the partial answers, one per part. Combine them into one final answer to the instruction.\n\n"
                + "\n\n".join(f"--- PART {i} ---\n{a}" for i, a in enumerate(answers, 1))
            )
            final = await call("direct_merge", merge_prompt)
    except (llm.ChatError, _DirectCut) as exc:
        status, error, final = "failed", str(exc), "\n".join(answers)
    return {
        "status": status,
        "error": error,
        "lane_mode": "n/a (direct call)",
        "run_id": None,
        "cost_dollars": dollars,
        "wall_seconds": round(time.monotonic() - t0, 3),
        "tokens": {"prompt_tokens": tin, "completion_tokens": tout, "total_tokens": tin + tout},
        "models": [baseline.model_id],
        "models_per_step": steps,
        "model_calls": len(steps),
        "answer": final,
    }


def local_model_available(matrix) -> bool:
    return any(m.checked and m.source in ("ollama", "lmstudio", "custom_key") and not m.remote for m in matrix.all())


async def execute_run(
    spec: RunSpec, jobs: dict, cfg: Config, matrix, baseline, chat=llm.chat, docker_runner=None,
) -> dict:
    """Run one cell of the matrix and write its result file. Returns the record (with ``path``)."""
    job = jobs[spec.job]
    conf = arm_conf(job, spec.arm)
    is_local = bool(conf.get("local")) or spec.arm in LOCAL_ARMS

    if is_local and (cfg.skip_local or not local_model_available(matrix)):
        reason = LOCAL_NOT_RUN_REASON if cfg.skip_local else (
            "local arm not run: no checked local model in the matrix. No number is recorded.")
        rec = {"status": "not run", "reason": reason, "cost_dollars": 0.0, "answer": None,
               "quality": None, "models": [], "models_per_step": [], "wall_seconds": None, "tokens": None}
        rec["path"] = str(write_result(cfg, spec, {k: v for k, v in rec.items()}))
        return rec

    estimate = estimate_run(job, spec.arm, cfg, baseline, borg_price_model(matrix, baseline))
    try:
        cfg.ledger.check(estimate, cfg.phase_cap)
    except budget.BudgetRefused as exc:
        rec = {"status": "refused", "reason": str(exc), "estimate_dollars": estimate, "cost_dollars": 0.0,
               "answer": None, "quality": None}
        rec["path"] = str(write_result(cfg, spec, {k: v for k, v in rec.items()}))
        return rec

    try:
        if spec.arm == "direct":
            rec = await run_direct(job, spec, cfg, baseline, chat=chat)
        else:
            rec = await run_borg(job, spec, cfg, matrix)
    except Exception as exc:  # a run must never take the matrix down
        rec = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "cost_dollars": 0.0,
               "answer": "", "models": [], "models_per_step": []}
    rec["estimate_dollars"] = estimate
    rec["quality"] = score_answer(job, rec.get("answer") or "", cfg, runner=docker_runner)
    cfg.ledger.record(spec.key, rec.get("cost_dollars") or 0.0)
    rec["path"] = str(write_result(cfg, spec, {k: v for k, v in rec.items()}))
    return rec


async def run_matrix(runs: list[RunSpec], jobs: dict, cfg: Config, matrix, baseline, chat=llm.chat,
                     docker_runner=None, log=print) -> list[dict]:
    out = []
    for spec in runs:
        if _is_done(cfg, spec):
            log(f"skip {spec.key}: result file exists (use --force to redo)")
            continue
        rec = await execute_run(spec, jobs, cfg, matrix, baseline, chat=chat, docker_runner=docker_runner)
        out.append({"spec": spec, **rec})
        log(f"{spec.key}: {rec['status']} cost=${(rec.get('cost_dollars') or 0):.4f}"
            + (f" ({rec.get('reason')})" if rec["status"] in ("refused", "not run") else ""))
        if rec["status"] == "refused":
            log("budget guard refused a run; stopping the matrix")
            break
    return out


# --------------------------------------------------------------------------
# Judging job C
# --------------------------------------------------------------------------

async def run_judge(jobs: dict, cfg: Config, matrix, baseline, chat=llm.chat, log=print) -> Optional[dict]:
    """Judge direct against borg_cloud for every judged job. Writes ``judge_<job>.json``. Counted in the ledger."""
    results = None
    for name, job in jobs.items():
        if (job.get("quality") or {}).get("kind") != "pairwise_judge":
            continue
        pairs, incomplete = [], []
        for n in range(1, 10):
            d, b = (cfg.day_dir / f"{name}_direct_{n}.json"), (cfg.day_dir / f"{name}_borg_cloud_{n}.json")
            if not (d.exists() or b.exists()):
                continue
            dr = json.loads(d.read_text(encoding="utf-8")) if d.exists() else {}
            br = json.loads(b.read_text(encoding="utf-8")) if b.exists() else {}
            if dr.get("status") in FINISHED and br.get("status") in FINISHED:
                pairs.append({"id": f"{name}_{n}", "direct": dr.get("answer") or "", "borg": br.get("answer") or "",
                              "direct_failed": dr["status"] == "failed", "borg_failed": br["status"] == "failed"})
            else:
                incomplete.append({"id": f"{name}_{n}", "direct": dr.get("status"), "borg": br.get("status")})
        if not pairs:
            log(f"judge {name}: no finished direct/borg_cloud pair; nothing to judge")
            continue
        jms = pick_judges(matrix, baseline, cfg.judge_model_ids)
        to_judge = sum(1 for p in pairs if not p["direct_failed"] and not p["borg_failed"])
        estimate = estimate_judge(job, to_judge, jms) if to_judge else 0.0
        cfg.ledger.check(estimate, cfg.phase_cap)
        key = load_answers(job, cfg)
        spent = {"dollars": 0.0}

        def make_ask(jm):
            async def ask(system: str, user: str) -> dict:
                r = await chat(jm, system, user, JUDGE_MAX_TOKENS)
                spent["dollars"] += r["dollars"]
                return r
            return ask

        try:
            results = await judge.judge_all([make_ask(jm) for jm in jms], key, pairs)
            results["status"] = "completed"
        except judge.JudgeRefused as exc:
            results = {"status": "refused", "reason": str(exc)}
        results.update({"job": name, "judge_models": [jm.model_id for jm in jms], "estimate_dollars": estimate,
                        "cost_dollars": spent["dollars"], "incomplete_repeats": incomplete})
        cfg.ledger.record(f"judge_{name}", spent["dollars"], kind="judge")
        cfg.day_dir.mkdir(parents=True, exist_ok=True)
        (cfg.day_dir / f"judge_{name}.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
        log(f"judge {name}: {results['status']}"
            + (f" direct_wins={results['direct_wins']} borg_wins={results['borg_wins']} ties={results['ties']}"
               if results["status"] == "completed" else f" ({results['reason']})"))
    return results


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def _per_mtok(model) -> str:
    if model is None or model.pricing is None:
        return "no price"
    return f"${model.pricing.input_per_token * 1e6:.2f} / ${model.pricing.output_per_token * 1e6:.2f} per MTok"


def dry_run(jobs, cfg, matrix, baseline, only_jobs, arms_filter, repeats, job_repeats, rules, out=print) -> int:
    if baseline is None:
        out("pre-flight: no checked model in the matrix, so the direct arm has no baseline. Nothing was spent.")
        return 2
    try:
        jms = pick_judges(matrix, baseline, cfg.judge_model_ids)
    except ValueError as exc:
        out(f"pre-flight: {exc}")
        return 2
    limit = float(rules.get("estimate_limit_dollars", budget.ESTIMATE_LIMIT_DOLLARS))
    runs = build_matrix(jobs, only_jobs, arms_filter, repeats, job_repeats)
    pool = borg_price_model(matrix, baseline)
    est = estimate_matrix(runs, jobs, cfg, baseline, jms, pool)

    out(f"BORG_ROUTE bench: dry run (date {cfg.date}; nothing is sent, nothing is spent)")
    out(f"baseline, direct arm : {baseline.model_id}  ({_per_mtok(baseline)})")
    for jm in jms:
        out(f"judge, job C         : {jm.model_id}  ({_per_mtok(jm)})  [in neither arm; vendor '{vendor_of(jm.model_id)}']")
    checked = [m.model_id for m in matrix.all() if m.checked]
    out(f"checked models ({len(checked)}): {', '.join(checked)}")
    out(f"local arms           : {'NOT RUN in this build (--skip-local); no cost, no numbers' if cfg.skip_local else 'will run if a local model is checked'}")
    out("")
    out(f"{'job':<4}{'arm':<20}{'lane_mode':<14}{'repeats':<9}{'est $/run':>10}{'est $ total':>13}")
    n_run = n_skip = 0
    for name, job in jobs.items():
        if only_jobs and name not in only_jobs:
            continue
        for arm in arms_of(job, arms_filter):
            rs = [r for r in runs if r.job == name and r.arm == arm]
            if not rs:
                continue
            e = est["per_run"][rs[0].key]
            local = bool(arm_conf(job, arm).get("local"))
            skipped = local and cfg.skip_local
            n_skip += len(rs) if skipped else 0
            n_run += 0 if skipped else len(rs)
            lane = arm_conf(job, arm).get("lane_mode", "n/a")
            tag = "  not run" if skipped else ""
            out(f"{name:<4}{arm:<20}{lane:<14}{len(rs):<9}{e:>10.4f}{e * len(rs):>13.4f}{tag}")
    out("")
    out(f"runs: {n_run + n_skip} cells = {n_run} to run + {n_skip} written as 'not run'")
    out(f"BORG arms priced at   : {_per_mtok(pool)} (dearest checked pool input and output prices) "
        "+ the answerer system prompt once per item")
    out(f"estimate, runs ({n_run})  : US${est['runs_total']:.2f}   (an upper bound)")
    out(f"estimate, judge       : US${est['judge']:.2f}")
    out(f"estimate, total       : US${est['total']:.2f}   limit US${limit:.2f}   phase cap US${cfg.phase_cap:.2f}")
    out(f"spent so far (ledger) : US${cfg.ledger.spent():.4f}")
    if est["total"] <= limit:
        out("VERDICT: within the limit; the full matrix may run.")
        return 0
    out("VERDICT: OVER the limit. Shrink in this order (rules in bench/shrink_rules.yaml, apply with --shrink-steps N):")
    for i in range(1, len(rules.get("steps") or []) + 1):
        o = budget.apply_shrink(rules, i)
        r2 = build_matrix(jobs, only_jobs, arms_filter, o["repeats"] if o["repeats"] is not None else repeats,
                          {**(job_repeats or {}), **o["job_repeats"]})
        e2 = estimate_matrix(r2, jobs, cfg, baseline, jms, pool)["total"]
        out(f"  {i}. {rules['steps'][i - 1].get('name')}: estimate US${e2:.2f} "
            f"({'within' if e2 <= limit else 'still over'} the limit)")
    return 1


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="BORG_ROUTE benchmark runner (work order 3.2).")
    ap.add_argument("--dry-run", action="store_true", help="print the matrix and the estimate; spend nothing")
    ap.add_argument("--jobs", help="comma list, e.g. A,C")
    ap.add_argument("--arms", help="comma list, e.g. direct,borg_cloud (also enables optional arms such as D's direct)")
    ap.add_argument("--repeats", type=int, help="cap repeats for every job")
    ap.add_argument("--shrink-steps", type=int, default=0, help="apply the first N steps of bench/shrink_rules.yaml")
    ap.add_argument("--skip-local", action=argparse.BooleanOptionalAction, default=True,
                    help="write 'not run' for local arms (default on in this build)")
    ap.add_argument("--judge-model", action="append", default=[],
                    help="model id for the job C judge (in neither arm, vendor outside both sides); "
                         "give it twice for two-judge mode (one per side's vendor), only when no outside vendor exists")
    ap.add_argument("--check-model", action="append", default=[], help="check this matrix row in memory for the run (never saved)")
    ap.add_argument("--prior-spend", type=float, help="set the dollars already spent earlier in the phase (ledger)")
    ap.add_argument("--no-docker", action="store_true", help="skip job B's container test step")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="redo runs whose result file exists")
    ap.add_argument("--date", help="results folder date YYYY_MM_DD (default today)")
    ap.add_argument("--results-root")
    args = ap.parse_args(argv)

    os.chdir(REPO_ROOT)  # the matrix, skills and prompts are read by relative path
    cfg = Config(skip_local=args.skip_local, use_docker=not args.no_docker,
                 judge_model_ids=list(args.judge_model), force=args.force)
    if args.date:
        cfg.date = args.date
    if args.results_root:
        cfg.results_root = Path(args.results_root)
    if args.prior_spend is not None:
        cfg.ledger.set_prior(args.prior_spend)

    rules = budget.load_shrink_rules(cfg.bench_dir / "shrink_rules.yaml")
    o = budget.apply_shrink(rules, args.shrink_steps)
    repeats = o["repeats"] if o["repeats"] is not None else args.repeats
    if o["repeats"] is not None and args.repeats is not None:
        repeats = min(o["repeats"], args.repeats)
    job_repeats = o["job_repeats"]
    only = args.jobs.split(",") if args.jobs else None
    arms = args.arms.split(",") if args.arms else None

    jobs = load_jobs(cfg.jobs_dir)
    matrix = load_matrix(args.check_model)
    baseline = baseline_model(matrix)

    if args.dry_run:
        return dry_run(jobs, cfg, matrix, baseline, only, arms, repeats, job_repeats, rules)

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # the key is never printed
    if not os.getenv("OPENROUTER_API_KEY"):
        print("preflight: OPENROUTER_API_KEY is not set (put it in .env). Nothing was spent.")
        return 2
    if baseline is None:
        print("preflight: no checked model in the matrix. Nothing was spent.")
        return 2

    runs = build_matrix(jobs, only, arms, repeats, job_repeats)
    rc = 0
    if not args.judge_only:
        done = asyncio.run(run_matrix(runs, jobs, cfg, matrix, baseline))
        if any(r["status"] == "refused" for r in done):
            rc = 3
        elif any(r["status"] == "failed" for r in done):
            rc = 4
    if not args.no_judge and rc != 3:
        asyncio.run(run_judge(jobs, cfg, matrix, baseline))
    print(f"ledger: US${cfg.ledger.spent():.4f} spent of US${cfg.phase_cap:.2f}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
