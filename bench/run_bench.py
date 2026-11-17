"""The benchmark runner (work order 3.2).

    python bench/run_bench.py --dry-run          # print the run matrix and the estimate; spends nothing
    python bench/run_bench.py --dry-run --stage 1|2   # print the staged plan (ruling 14); spends nothing
    python bench/run_bench.py --stage 1          # one repeat of every cell, cheapest estimate first (SPENDS MONEY)
    python bench/run_bench.py --stage 2          # repeats 2 and 3 by whole job, order A, D, B, C (SPENDS MONEY)

One run = job x arm x repeat. Each BORG arm goes through ``run_pipeline`` with
the right ``lane_mode`` and the job's folder handed over by reference
(``BORG_DOCS_ROOT`` pointed at ``bench/data`` for the run), on a fresh temp
database. The checked models are a temporary in-memory matrix: this script never
writes ``data/capability_matrix.yaml``. The ``direct`` arm calls the baseline
model once (or the fewest calls that fit its context) with the same text.

Each run writes ``bench/results/<YYYY_MM_DD>/<job>_<arm>_<n>.json``. Spend is
kept in ``bench/results/spend.json`` from the logged dollars of each run, and a
run is refused if that sum plus the run's estimate would pass US$20.

Local arms (``borg_local_workers``: job A, and job D's raw_local arm) run when a
local model is checked (``--check-model ollama/llama3.2:3b``, ruling 13). With
``--skip-local``, or with no checked local model, they write a result file with
status "not run" and the reason, never a number.

Staged plan (planner ruling 14, written in bench/README.md): every guard failure STOPS the stage; a run
whose real cost passes its own estimate trips the wire (re-estimate of the rest, continue only if it fits).
The add-or-stop decision looks at cost only.

Exit codes: 0 ok, 2 setup problem, 3 a run was refused by the budget guard (the stage stopped),
4 a run failed (the stage finished), 5 the tripwire stopped the stage, 6 the judge failed or failed its
sanity case.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import yaml  # noqa: E402

from bench import budget, checks, facts, judge, llm  # noqa: E402

BENCH_DIR = REPO_ROOT / "bench"
DEFAULT_MATRIX = BENCH_DIR / "pool_matrix.yaml"   # the bench's own matrix (3.1 ruling); never written

LOCAL_NOT_RUN_REASON = "local arm skipped with --skip-local. No number is recorded."

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
# The run time limit (ruling 14), set once: every BORG arm runs under BORG_MAX_RUN_SECONDS=7200 (two hours, so
# the local arms on a CPU-only machine can finish and be timed as measured); the direct arm's HTTP timeout is the
# same 7200 s, so a long answer from a reasoning model is never cut by the client.
MAX_RUN_SECONDS = 7200
DIRECT_TIMEOUT_S = 7200.0
STAGE_JOB_ORDER = ("A", "D", "B", "C")   # ruling 14: the order repeats are added by whole job
TRIPWIRE_STOP_RC = 5
JUDGE_FAILED_RC = 6   # the judge failed or refused (sanity case): no job C verdict, so the invocation fails
FINISHED = ("completed", "failed")   # statuses a comparison can use; "refused" and "not run" cannot

# Every routing-relevant setting, pinned by the runner for each run (round-2 review (5)); written into every
# result file as routing_config.env. The values are today's effective values (.env and the engine defaults,
# read 17-Nov-2026), so the bench measures the product as it is configured; None means "removed from the
# environment" (the engine's built-in behaviour). Per-run values (BORG_LANE_MODE, BORG_DOCS_ROOT,
# BORG_MAX_RUN_SECONDS, BASELINE_MODEL_ID) are added in run_borg.
PINNED_ENV: dict[str, Optional[str]] = {
    "ANSWERER_MAX_TOKENS": "6000",               # .env; reasoning-family models get 4x = 24000 (no retry above 16000)
    "ORCHESTRATOR_MAX_TOKENS": "6000",           # .env; reasoning-family orchestrators get 24000
    "ORCHESTRATOR_QUALITY_REQUIREMENT": "9.0",   # .env and engine default
    "BORG_RULE_ZERO_SHORTCUT": "true",           # .env and engine default (a run with documents never takes it)
    "LEAF_QUALITY_LIGHT": "6.0",                 # engine default floors (node_router.DEFAULT_FLOORS)
    "LEAF_QUALITY_MID": "7.5",
    "LEAF_QUALITY_HEAVY": "9.0",
    "TIME_COST_PER_SEC": "0.0001",               # .env and engine default
    "BORG_LANE_MAX_OUTPUT_CHARS": "1500",        # lane defaults (src/lane/settings.py); none is set in .env
    "BORG_LANE_MAX_VERBATIM_CHARS": "120",
    "BORG_LANE_MAX_NGRAM_SHARE": "0.10",
    "BORG_LANE_MANIFEST": "names",
    "BORG_LANE_PART_CHARS": "8000",
    "BORG_LANE_LOCAL_ALLOWLIST": None,           # unset: only the built-in local hosts count as local
    "BORG_LOCAL_HOSTS": None,
    "BORG_ANSWERER_CALL_TIMEOUT": "7200",        # bench only: the product defaults are 120 s and 180 s
    "BORG_ORCHESTRATOR_CALL_TIMEOUT": "7200",
}
# Web search is off for every bench run, both arms (round-2 review (6)): the direct arm has no tools, and for
# a BORG run the web_search tool is swapped for an unavailable stand-in (``tools_off``), so a step that asks for
# it gets a failed tool result (BRT-TOOL-001) and no search provider is called. Production is unchanged.
TOOLS_ENABLED = False


class _ToolOff:
    """Stands in for a registered tool during a bench run: same name, never available, never runs."""

    def __init__(self, name: str):
        self.name = name

    def is_available(self) -> bool:
        return False

    async def run(self, args: dict):  # pragma: no cover - the registry never calls an unavailable tool
        raise RuntimeError(f"{self.name} is off for bench runs")


class tools_off:
    """Context manager: every registered tool is unavailable inside it (names stay registered, so a plan that
    names a tool still validates); the real tools are put back on exit."""

    def __enter__(self):
        from src.tools.registry import TOOL_REGISTRY

        self.saved = dict(TOOL_REGISTRY)
        for name in list(TOOL_REGISTRY):
            TOOL_REGISTRY[name] = _ToolOff(name)
        return self

    def __exit__(self, *exc):
        from src.tools.registry import TOOL_REGISTRY

        TOOL_REGISTRY.clear()
        TOOL_REGISTRY.update(self.saved)


# Transport errors (round-2 review (7); planner condition, 17-Nov-2026): ONLY a provider or network failure,
# that is HTTP 429, HTTP 5xx, a connection error, a connect/read timeout. BORG_ROUTE reports these under its
# own wrapping codes (BRT-ANS-002 "HTTP 503 from ...", BRT-ANS-006 "model call timed out", BRT-ORC-001 "HTTP 429
# from orchestrator" / "orchestrator call timed out" / "request error calling ..."), and the message text is
# what is matched. Every other failure (a BRT-* failure of BORG_ROUTE's own making such as a lane refusal,
# BRT-ANS-009 truncation, a malformed answer, a plan error, the local pool BRT-ANS-011; a refusal; a cut-off
# or empty direct answer; a 4xx other than 429) is never rerun.
_TRANSPORT_PATTERNS = (
    r"\bHTTP (?:429|5\d\d)\b",
    r"\bmodel call timed out\b",
    r"\borchestrator call timed out\b",
    r"\brequest error calling\b",
    r"^(?:ConnectError|ConnectTimeout|ReadTimeout|WriteTimeout|ReadError|WriteError|RemoteProtocolError|"
    r"NetworkError|TimeoutException)\b",
)


def is_transport_failure(rec: dict) -> bool:
    """True when a failed run failed on a transport error (see ``_TRANSPORT_PATTERNS``); False otherwise."""
    import re

    if rec.get("status") != "failed":
        return False
    if rec.get("failure_kind") in ("transport", "other"):
        return rec["failure_kind"] == "transport"
    err = str(rec.get("error") or "")
    if not err or "BRT-ANS-011" in err:
        return False
    return any(re.search(p, err, re.MULTILINE) for p in _TRANSPORT_PATTERNS)


# --------------------------------------------------------------------------
# Config, jobs, matrix
# --------------------------------------------------------------------------

@dataclass
class Config:
    bench_dir: Path = BENCH_DIR
    results_root: Path = BENCH_DIR / "results"
    date: str = field(default_factory=lambda: datetime.now().strftime("%Y_%m_%d"))
    skip_local: bool = False   # ruling 13: the local model serves; the local arms run
    use_docker: bool = True
    max_run_seconds: int = MAX_RUN_SECONDS    # BORG_MAX_RUN_SECONDS for every BORG arm (ruling 14)
    direct_timeout_s: float = DIRECT_TIMEOUT_S  # the direct arm's HTTP timeout for one call
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


def _refuse_save() -> None:
    raise RuntimeError("the bench matrix is an in-memory copy and is never saved")


def load_matrix(extra_checked: list[str] = (), path: Optional[Path] = None):
    """The bench matrix (``bench/pool_matrix.yaml`` unless ``path``), with extra rows checked in memory only.
    Never saved: its ``save`` raises, so neither this file nor data/capability_matrix.yaml can be written."""
    from src.registry.capability_matrix import CapabilityMatrix

    matrix = CapabilityMatrix(str(path or DEFAULT_MATRIX))
    matrix.load()
    matrix.save = _refuse_save
    for model_id in extra_checked:
        row = matrix.get(model_id)
        if row is None:
            raise SystemExit(f"--check-model: '{model_id}' is not in the matrix")
        row.checked = True
    return matrix


def baseline_model(matrix, pinned: Optional[str] = None):
    """The direct arm's model: ``pinned`` (``--baseline``), else ``BASELINE_MODEL_ID`` (read from .env first),
    else the checked model with the highest reasoning score in the matrix file."""
    from src.registry.cost import select_baseline_model

    if pinned:
        row = matrix.get(pinned)
        if row is None:
            raise ValueError(f"--baseline: '{pinned}' is not in the matrix")
        return row
    return select_baseline_model(matrix, os.getenv("BASELINE_MODEL_ID"))


_SOURCES = ("openrouter", "ollama", "lmstudio", "custom_key")


# One name per vendor, whatever the id spells it as (OpenRouter's vendor ids, and local model families).
_VENDOR_ALIASES = {"meta-llama": "meta", "mistralai": "mistral", "x-ai": "xai", "moonshotai": "moonshot",
                   "z-ai": "zhipu", "thudm": "zhipu"}
# A local row (``ollama/llama3.2:3b``) names a model family, not a vendor: the family's maker is the vendor,
# so a judge from that maker is refused while the local model is in the pool (round-2 review (10)).
_LOCAL_FAMILY_VENDORS = (
    ("llama", "meta"), ("codellama", "meta"), ("qwen", "qwen"), ("qwq", "qwen"), ("gemma", "google"),
    ("mistral", "mistral"), ("mixtral", "mistral"), ("codestral", "mistral"), ("ministral", "mistral"),
    ("phi", "microsoft"), ("deepseek", "deepseek"), ("gpt-oss", "openai"), ("granite", "ibm"),
    ("command-r", "cohere"), ("kimi", "moonshot"), ("glm", "zhipu"), ("olmo", "allenai"),
)


def vendor_of(model_id: str) -> str:
    """The vendor of a model id: ``openrouter/qwen/qwen4-max`` -> ``qwen``; ``openrouter/meta-llama/x`` -> ``meta``;
    a local row is mapped from its family to its maker: ``ollama/llama3.2:3b`` -> ``meta``,
    ``ollama/qwen3:4b`` -> ``qwen`` (an unknown family keeps the family's name)."""
    parts = [p for p in str(model_id).split("/") if p]
    source = parts[0].lower() if len(parts) >= 2 and parts[0] in _SOURCES else None
    if source:
        parts = parts[1:]
    if not parts:
        return ""
    if source in ("ollama", "lmstudio") or (source and len(parts) == 1):
        name = parts[-1].lower()
        for family, vendor in _LOCAL_FAMILY_VENDORS:
            if name.startswith(family):
                return vendor
        if len(parts) == 1:
            return name.split(":")[0].rstrip("0123456789.-_") or name
    head = parts[0].lower()
    return _VENDOR_ALIASES.get(head, head)


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
    """Upper-bound dollars for one run. The direct arm is priced on the baseline. Local arms are priced like
    the cloud BORG arm (their planner and compose steps run on cloud models; local calls cost nothing, so
    this is an upper bound).
    BORG arms are priced at ``borg_model`` (``borg_price_model``: the dearest checked pool prices; the
    baseline if not given) and pay the answerer system prompt once per item (one item for job B)."""
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
    """Job C's machine fact score (bench/facts.py). Informational only: never quoted."""
    return facts.score_facts(answer, key)


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
        return facts.informational(answer, key)
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


def new_db_path(cfg: Config, spec: RunSpec) -> Path:
    """``<results>/<date>/dbs/<job>_<arm>_<n>.db``; a rerun of the same key gets ``..__2.db`` and so on,
    so an earlier run's database (and its ledger entry) is never overwritten."""
    folder = cfg.day_dir / "dbs"
    path = folder / f"{spec.key}.db"
    k = 2
    while path.exists():
        path = folder / f"{spec.key}__{k}.db"
        k += 1
    return path


def new_calls_log(cfg: Config, key: str) -> Path:
    """The calls log of a run outside BORG_ROUTE's database (direct arm, judge), next to the databases."""
    folder = cfg.day_dir / "dbs"
    path = folder / f"{key}.calls.jsonl"
    k = 2
    while path.exists():
        path = folder / f"{key}__{k}.calls.jsonl"
        k += 1
    return path


def logging_chat(chat, calls_log: Path):
    """Wrap a chat callable so every paid call (and the dollars of a failed one) is appended to ``calls_log``."""
    async def wrapped(model, system, user, max_tokens, *a, **kw):
        try:
            r = await chat(model, system, user, max_tokens, *a, **kw)
        except llm.ChatError as exc:
            if exc.dollars:
                budget.append_call(calls_log, exc.dollars, model=model.model_id, error=str(exc)[:200])
            raise
        budget.append_call(calls_log, r.get("dollars") or 0.0, model=model.model_id,
                           finish_reason=r.get("finish_reason"), max_tokens=max_tokens)
        return r
    return wrapped


def routing_config(cfg: Config, matrix, baseline, mode: str) -> dict:
    """The routing configuration, fixed for every arm and written into each result file (ruling 14).

    Leaf floors and ``time_cost_per_sec`` are the engine's own defaults (the runner removes any override from
    the environment for a BORG run). The pool is the checked rows passed to the run. The receipts' baseline is
    the bench baseline when the engine can resolve it (it looks the id up in the matrix it was given, which
    holds every row of ``--matrix``, checked or not); if it cannot, the engine's own default is used and the
    note says so.
    """
    from src.registry.cost import _DEFAULT_TIME_COST_PER_SEC, select_baseline_model
    from src.registry.node_router import DEFAULT_FLOORS

    resolved = baseline is not None and matrix.get(baseline.model_id) is not None
    if resolved:
        bid, note = baseline.model_id, "the bench baseline; the engine resolves it from the matrix it is given"
    else:
        fallback = select_baseline_model(matrix, None)
        bid = fallback.model_id if fallback else None
        note = (f"the bench baseline '{baseline.model_id if baseline else None}' cannot be resolved by the engine "
                "(not a row of its matrix); the engine's own default (highest-reasoning checked model) is used")
    env = dict(PINNED_ENV)
    env.update({"BORG_LANE_MODE": mode if mode not in ("off", None) and not str(mode).startswith("n/a") else None,
                "BORG_MAX_RUN_SECONDS": str(cfg.max_run_seconds),
                "BASELINE_MODEL_ID": bid if resolved else None})
    return {
        "leaf_floors": dict(DEFAULT_FLOORS),
        "time_cost_per_sec": _DEFAULT_TIME_COST_PER_SEC,
        "pool": sorted(m.model_id for m in matrix.all() if m.checked),
        "lane_mode": mode,
        "max_run_seconds": cfg.max_run_seconds,
        "direct_timeout_seconds": cfg.direct_timeout_s,
        "answerer_call_timeout_seconds": float(PINNED_ENV["BORG_ANSWERER_CALL_TIMEOUT"]),
        "orchestrator_call_timeout_seconds": float(PINNED_ENV["BORG_ORCHESTRATOR_CALL_TIMEOUT"]),
        "tools_enabled": TOOLS_ENABLED,
        "env": env,
        "baseline_model_id": bid,
        "baseline_resolved_by_engine": resolved,
        "baseline_note": note,
    }


async def run_borg(job: dict, spec: RunSpec, cfg: Config, matrix, db_path=None, baseline=None) -> dict:
    """One BORG_ROUTE run through ``run_pipeline`` on a fresh database at ``db_path`` (kept, git-ignored)."""
    from src.db.schema import create_all
    from src.orchestrator import engine
    from src.pipeline.runner import run_pipeline

    conf = arm_conf(job, spec.arm)
    mode = conf.get("lane_mode", "off")
    folder = job.get("data_folder")
    if folder and mode == "off":
        raise ValueError(f"job {spec.job} hands over documents; arm {spec.arm} needs a lane mode")
    db_path = str(db_path or new_db_path(cfg, spec))
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    await create_all(db_path)
    engine._shared_matrix = matrix
    instruction = instruction_of(job, cfg)
    documents = {"folder": folder} if folder else None

    rc = routing_config(cfg, matrix, baseline, mode)
    # every routing-relevant setting pinned (rc["env"]): no stray value from the shell or .env reaches the run;
    # tools (web search) off for the run
    with _Env(BORG_DOCS_ROOT=str(cfg.data_root), **rc["env"]), tools_off():
        t0 = time.monotonic()
        result = await run_pipeline(instruction, db_path=db_path, documents=documents)
        wall = time.monotonic() - t0

    tree = read_run_tree(db_path, result.get("run_id"))
    record = {
        "status": result.get("status", "failed"),
        "error": result.get("error"),
        "lane_mode": mode,
        "routing_config": rc,
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

    async def send(model, sys_, user, cap):   # the long HTTP timeout of ruling 14 on every direct call
        return await chat(model, sys_, user, cap, timeout=cfg.direct_timeout_s)

    steps, tin, tout, dollars = [], 0, 0, 0.0
    answers: list[str] = []
    status, error, failure_kind = "completed", None, None
    t0 = time.monotonic()

    async def call(step: str, text: str) -> str:
        nonlocal tin, tout, dollars
        try:
            r = await llm.chat_capped(baseline, system, text, out, send=send)
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
        failure_kind = "transport" if getattr(exc, "transport", False) else "other"
    return {
        "status": status,
        "error": error,
        **({"failure_kind": failure_kind} if status == "failed" else {}),
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
    estimate: Optional[float] = None,
) -> dict:
    """Run one cell of the matrix and write its result file. Returns the record (with ``path``).

    ``estimate`` replaces the conservative estimate the guard uses (a stage's re-estimate from measured costs)."""
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

    if estimate is None:
        estimate = estimate_run(job, spec.arm, cfg, baseline, borg_price_model(matrix, baseline))

    aborted: list[dict] = []
    for attempt in (1, 2):
        try:
            cfg.ledger.check(estimate, cfg.phase_cap)
        except budget.BudgetRefused as exc:
            reason = str(exc) if attempt == 1 else f"the transport rerun was refused: {exc}"
            rec = {"status": "refused", "reason": reason, "estimate_dollars": estimate, "cost_dollars": 0.0,
                   "answer": None, "quality": None, "aborted_attempts": aborted}
            rec["path"] = str(write_result(cfg, spec, {k: v for k, v in rec.items()}))
            return rec
        rec = await _one_attempt(spec, job, conf, cfg, matrix, baseline, chat, attempt)
        if rec["status"] == "failed":
            rec.setdefault("failure_kind", "transport" if is_transport_failure(rec) else "other")
        if attempt == 1 and rec["status"] == "failed" and rec["failure_kind"] == "transport":
            # a provider or network failure: rerun once, the same way on both arms; the aborted attempt's
            # real spend is in the ledger and listed beside the run that replaces it
            aborted.append({**{k: rec.get(k) for k in ("status", "error", "failure_kind", "wall_seconds",
                                                        "db_path", "calls_log", "run_id")},
                            "cost_dollars": rec["ledger_dollars"]})
            continue
        break
    rec["aborted_attempts"] = aborted
    rec["estimate_dollars"] = estimate
    rec["quality"] = score_answer(job, rec.get("answer") or "", cfg, runner=docker_runner)
    rec["path"] = str(write_result(cfg, spec, {k: v for k, v in rec.items()}))
    return rec


async def _one_attempt(spec: RunSpec, job: dict, conf: dict, cfg: Config, matrix, baseline, chat,
                       attempt: int) -> dict:
    """One attempt of a run: its own database or calls log, its own ledger entry (opened at start, closed with
    ``max(cost_dollars, dollars logged)``). Returns the run's record (not yet scored or written)."""
    db_path = calls_log = None
    if spec.arm == "direct":
        calls_log = new_calls_log(cfg, spec.key)
        calls_log.parent.mkdir(parents=True, exist_ok=True)
        calls_log.touch()   # claimed now, so a rerun gets its own log even when this attempt paid for nothing
    else:
        db_path = new_db_path(cfg, spec)
    entry = cfg.ledger.start(spec.key, db_path=db_path, calls_log=calls_log, date=cfg.date)
    t0 = time.monotonic()
    try:
        if spec.arm == "direct":
            with _Env(ANSWERER_MAX_TOKENS=PINNED_ENV["ANSWERER_MAX_TOKENS"]):   # the same cap policy as BORG
                rec = await run_direct(job, spec, cfg, baseline, chat=logging_chat(chat, calls_log))
        else:
            rec = await run_borg(job, spec, cfg, matrix, db_path, baseline)
    except Exception as exc:  # a run must never take the matrix down; its spend is still counted
        rec = {"status": "failed", "error": f"{type(exc).__name__}: {str(exc)[:300]}",
               "cost_dollars": budget.logged_dollars(db_path) + budget.calls_log_dollars(calls_log),
               "wall_seconds": round(time.monotonic() - t0, 3),
               "answer": "", "models": [], "models_per_step": []}
        rec["failure_kind"] = "transport" if llm.is_transport_exception(exc) else "other"
    rec.setdefault("routing_config", routing_config(
        cfg, matrix, baseline, rec.get("lane_mode") or conf.get("lane_mode", "n/a (direct call)")))
    rec["db_path"] = str(db_path) if db_path else None
    rec["calls_log"] = str(calls_log) if calls_log else None
    rec["attempt"] = attempt
    logged = budget.logged_dollars(db_path) + budget.calls_log_dollars(calls_log)
    rec["ledger_dollars"] = max(float(rec.get("cost_dollars") or 0.0), logged)
    rerun = attempt == 1 and is_transport_failure(rec)
    cfg.ledger.finish(entry, rec["ledger_dollars"],
                      note="attempt 1 failed on a transport error; rerun once" if rerun else None)
    return rec


async def run_matrix(runs: list[RunSpec], jobs: dict, cfg: Config, matrix, baseline, chat=llm.chat,
                     docker_runner=None, log=print) -> list[dict]:
    out = []
    for e in cfg.ledger.recover():
        log(f"ledger: recovered unfinished entry {e['key']} (US${e['dollars']:.4f} from its database)")
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
# The staged plan (planner ruling 14)
# --------------------------------------------------------------------------

def ceil_cent(x: float) -> float:
    """Round up to the next cent (a cost already at a whole cent stays)."""
    return math.ceil(round(float(x) * 100, 6)) / 100


def _cells_to_run(jobs, cfg, matrix, only=None, arms=None):
    return [(name, arm) for name, job in jobs.items() if not only or name in only
            for arm in arms_of(job, arms) if not local_skipped(job, arm, cfg, matrix)]


def conservative_estimates(runs: list[RunSpec], jobs, cfg, matrix, baseline) -> dict[str, float]:
    """The conservative estimate of each run (0 for a local cell that will be written "not run")."""
    pool = borg_price_model(matrix, baseline)
    return {r.key: 0.0 if local_skipped(jobs[r.job], r.arm, cfg, matrix)
            else estimate_run(jobs[r.job], r.arm, cfg, baseline, pool) for r in runs}


def stage1_plan(jobs, cfg, matrix, baseline, only=None, arms=None) -> list[tuple[RunSpec, float]]:
    """Stage 1: one repeat of every cell (job x arm), cheapest conservative estimate first."""
    runs = build_matrix(jobs, only, arms, repeats=1)
    est = conservative_estimates(runs, jobs, cfg, matrix, baseline)
    return sorted(((r, est[r.key]) for r in runs), key=lambda p: p[1])   # stable: ties keep job order


def load_measured(cfg: Config, jobs: dict) -> dict[tuple[str, str], list[float]]:
    """Measured cost per cell from the result files. Reads only the run's state and ``cost_dollars``;
    no quality field is ever read here (the stage decisions look at cost only)."""
    out: dict[tuple[str, str], list[float]] = {}
    for name, job in jobs.items():
        for arm in arms_of(job, list(arm_names_all(job))):
            for p in sorted(cfg.day_dir.glob(f"{name}_{arm}_*.json")):
                if not p.stem[len(f"{name}_{arm}_"):].isdigit():
                    continue
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                if rec.get("status") in FINISHED or rec.get("status") == "not run":
                    out.setdefault((name, arm), []).append(run_spend(rec))
    return out


def arm_names_all(job: dict) -> list[str]:
    return list((job.get("arms") or {}).keys()) + list((job.get("optional_arms") or {}).keys())


def reestimate(remaining: list[RunSpec], conservative: dict[str, float],
               measured: dict[tuple[str, str], list[float]], worst_ratio: float = 1.0) -> dict[str, float]:
    """Tripwire re-estimate: each remaining run is the dearest observed run of its own cell, rounded up to
    the next cent (a run that logged US$0 counts as US$0); a cell not yet measured is its conservative estimate
    times ``max(1, worst_ratio)`` (the worst real-to-estimate ratio observed so far: an estimate that was beaten
    4x once is not trusted again at face value; round-2 review (4))."""
    scale = max(1.0, float(worst_ratio or 0.0))
    out = {}
    for r in remaining:
        seen = measured.get((r.job, r.arm))
        out[r.key] = ceil_cent(max(seen)) if seen else conservative[r.key] * scale
    return out


def worst_ratio(cfg: Config, jobs: dict) -> float:
    """The worst real-to-estimate ratio over every finished result file of the date (cost fields only):
    ``spend_dollars`` (the run plus any aborted transport attempts) over ``estimate_dollars``. At least 1."""
    worst = 1.0
    for name, job in jobs.items():
        for arm in arm_names_all(job):
            for p in cfg.day_dir.glob(f"{name}_{arm}_*.json"):
                if not p.stem[len(f"{name}_{arm}_"):].isdigit():
                    continue
                try:
                    rec = json.loads(p.read_text(encoding="utf-8"))
                except ValueError:
                    continue
                est = float(rec.get("estimate_dollars") or 0.0)
                if rec.get("status") in FINISHED and est > 0:
                    worst = max(worst, run_spend(rec) / est)
    return worst


def run_spend(rec: dict) -> float:
    """Everything a run's record says was spent: its own cost plus its aborted transport attempts."""
    return float(rec.get("cost_dollars") or 0.0) + sum(
        float(a.get("cost_dollars") or 0.0) for a in rec.get("aborted_attempts") or [])


def repeat1_status(cfg: Config, jobs: dict, arms=None) -> dict[tuple[str, str], str]:
    """``{(job, arm): status}`` of every repeat-1 cell ("missing" when there is no readable result file).
    Reads the status field only: never a quality field."""
    out = {}
    for name, job in jobs.items():
        for arm in arms_of(job, arms):
            p = cfg.day_dir / f"{name}_{arm}_1.json"
            try:
                out[(name, arm)] = str(json.loads(p.read_text(encoding="utf-8")).get("status") or "missing")
            except (OSError, ValueError):
                out[(name, arm)] = "missing"
    return out


def _fixed_cost(extra_fixed: dict, name: str, n_extra: int) -> float:
    """``extra_fixed[name]``: a float (the same whatever the count) or a callable of the number of extra repeats."""
    v = extra_fixed.get(name, 0.0)
    return float(v(n_extra) if callable(v) else v)


def plan_stage2(jobs: dict, measured: dict, conservative_cells: dict, spent: float, cap: float,
                extra_fixed: Optional[dict] = None, only=None, arms=None,
                rep1: Optional[dict] = None, not_run_ok: Optional[set] = None) -> dict:
    """Stage 2: extra repeats by WHOLE JOB, order A, D, B, C. Cost only: nothing here reads a quality field.

    Eligibility (round-2 review (3)): with ``rep1`` (``{(job, arm): status}`` of repeat 1), a job gets extra
    repeats only when every repeat-1 cell finished (completed or failed); a cell missing, refused, started or
    "not run" makes the job ineligible, except a local cell in ``not_run_ok`` (written "not run" by design).

    A job's extra repeats cost, per cell, the dearest observed run rounded up to the cent (the conservative
    estimate for a cell never measured), summed over every arm and every extra repeat, plus ``extra_fixed[job]``
    (the judge, for job C; a callable of the number of extra repeats). The job tries every extra repeat it has
    left (repeats 2 and 3), else one fewer, down to one (ruling 14 adjustment); it is added at the first count
    where ``spent`` + what is already added + its extra <= ``cap``; one that fits at no count is skipped and the
    later jobs are still tried. Every arm of an added job gets the same repeats.
    """
    extra_fixed = extra_fixed or {}
    not_run_ok = not_run_ok or set()
    names = [n for n in STAGE_JOB_ORDER if n in jobs] + [n for n in jobs if n not in STAGE_JOB_ORDER]
    chosen, skipped, runs, estimates = [], [], [], {}
    committed = 0.0
    for name in names:
        if only and name not in only:
            continue
        job = jobs[name]
        target = int((job.get("repeats", 3)))
        job_arms = arms_of(job, arms)
        per_cell = {}
        for arm in job_arms:
            seen = measured.get((name, arm))
            per_cell[arm] = ceil_cent(max(seen)) if seen else conservative_cells[(name, arm)]
        if rep1 is not None:
            unfinished = [f"{name}/{arm} ({rep1.get((name, arm), 'missing')})" for arm in job_arms
                          if rep1.get((name, arm), "missing") not in FINISHED
                          and not (rep1.get((name, arm)) == "not run" and (name, arm) in not_run_ok)]
            if unfinished:
                skipped.append({"job": name, "extra_estimate": 0.0, "repeats": [], "per_cell": per_cell,
                                "reason": "repeat 1 not finished: " + ", ".join(unfinished)})
                continue
        entry = None
        for k in range(target - 1, 0, -1):          # both extra repeats, else one
            extra_n = list(range(2, 2 + k))
            judge_part = _fixed_cost(extra_fixed, name, k)
            extra = sum(per_cell.values()) * k + judge_part
            entry = {"job": name, "extra_estimate": extra, "repeats": extra_n, "per_cell": per_cell,
                     "judge_estimate": judge_part}
            if spent + committed + extra <= cap + 1e-9:
                break
        else:
            if entry is not None:
                entry = dict(entry, reason="does not fit under the cap, even with one extra repeat")
                skipped.append(entry)
            continue
        committed += entry["extra_estimate"]
        chosen.append(entry)
        for n in entry["repeats"]:
            for arm in job_arms:
                r = RunSpec(name, arm, n)
                runs.append(r)
                estimates[r.key] = per_cell[arm]
    return {"jobs": chosen, "skipped": skipped, "runs": runs, "estimates": estimates,
            "committed": committed, "spent": spent, "cap": cap}


def stage_inputs(stage: int, jobs, cfg, matrix, baseline, only=None, arms=None, jms=None) -> dict:
    """What a stage will do: ``{"sequence": [(RunSpec, estimate)], "tail": judge estimate, "plan": stage-2 plan}``."""
    judged = [n for n, j in jobs.items() if (j.get("quality") or {}).get("kind") == "pairwise_judge"
              and (not only or n in only)]
    if stage == 1:
        seq = stage1_plan(jobs, cfg, matrix, baseline, only, arms)
        tail = sum(estimate_judge(jobs[n], 1, jms) for n in judged) if jms else 0.0
        return {"sequence": seq, "tail": tail, "plan": None, "judge_jobs": judged}
    rep1 = build_matrix(jobs, only, arms, repeats=1)
    cons = conservative_estimates(rep1, jobs, cfg, matrix, baseline)
    cells = {(r.job, r.arm): cons[r.key] for r in rep1}
    # the judge is paid only for the new pairs (repeat 1's verdicts are reused), plus a fresh sanity case
    extra_fixed = ({n: (lambda k, _j=jobs[n]: estimate_judge(_j, k, jms)) for n in judged} if jms else {})
    rep1_cells = repeat1_status(cfg, jobs, arms)
    not_run_ok = {(r.job, r.arm) for r in rep1 if local_skipped(jobs[r.job], r.arm, cfg, matrix)}
    plan = plan_stage2(jobs, load_measured(cfg, jobs), cells, cfg.ledger.spent(), cfg.phase_cap,
                       extra_fixed, only, arms, rep1=rep1_cells, not_run_ok=not_run_ok)
    seq = [(r, plan["estimates"][r.key]) for r in plan["runs"]]
    tail = sum(j.get("judge_estimate", 0.0) for j in plan["jobs"])
    return {"sequence": seq, "tail": tail, "plan": plan, "judge_jobs": [j["job"] for j in plan["jobs"] if j["job"] in judged]}


async def run_stage(stage: int, jobs, cfg, matrix, baseline, chat=llm.chat, docker_runner=None, log=print,
                    only=None, arms=None, jms=None) -> dict:
    """Run stage 1 or 2. Returns ``{"rc", "stopped", "done", "judge_jobs", "tail"}``.

    Before each run the guard checks ledger spend + that run's estimate against the cap; a refusal STOPS the
    stage (rc 3). After each run, a real cost above the run's own estimate is the tripwire: every remaining
    run is re-estimated from measured costs, written to ``reestimate_s<stage>_<k>.json`` and printed, and the
    stage goes on only if ledger spend + the re-estimated remainder fits under the cap (else rc 5).
    """
    for e in cfg.ledger.recover():
        log(f"ledger: recovered unfinished entry {e['key']} (US${e['dollars']:.4f} from its database)")
    inp = stage_inputs(stage, jobs, cfg, matrix, baseline, only, arms, jms)
    seq = list(inp["sequence"])
    measured = load_measured(cfg, jobs)
    if stage == 2:
        for j in inp["plan"]["jobs"]:
            log(f"stage 2: job {j['job']} gets repeats {j['repeats']} (extra estimate US${j['extra_estimate']:.4f})")
        for j in inp["plan"]["skipped"]:
            log(f"stage 2: job {j['job']} skipped ({j.get('reason') or 'does not fit under the cap'})")
    out = {"rc": 0, "stopped": None, "done": [], "judge_jobs": inp["judge_jobs"], "tail": inp["tail"]}
    trips = 0
    i = 0
    while i < len(seq):
        spec, est = seq[i]
        i += 1
        if _is_done(cfg, spec):
            log(f"skip {spec.key}: result file exists (use --force to redo)")
            continue
        rec = await execute_run(spec, jobs, cfg, matrix, baseline, chat=chat, docker_runner=docker_runner,
                                estimate=est)
        out["done"].append({"spec": spec, **rec})
        cost = float(rec.get("cost_dollars") or 0.0)
        spend = run_spend(rec)   # the run plus any aborted transport attempts: what the estimate had to cover
        log(f"{spec.key}: {rec['status']} cost=${cost:.4f} estimate=${est:.4f}"
            + (f" aborted attempts=${spend - cost:.4f}" if spend > cost else "")
            + (f" ({rec.get('reason')})" if rec["status"] in ("refused", "not run") else ""))
        if rec["status"] == "refused":
            log("budget guard refused a run; stopping the stage (a guard failure is a stop, not a warning)")
            out.update(rc=3, stopped="guard")
            return out
        measured.setdefault((spec.job, spec.arm), []).append(spend)
        if rec["status"] != "not run" and spend > est + 1e-12:
            trips += 1
            rest = [(r, e) for r, e in seq[i:] if not _is_done(cfg, r)]
            ratio = max(worst_ratio(cfg, jobs), spend / est if est > 0 else 1.0)
            new = reestimate([r for r, _ in rest], {r.key: e for r, e in rest}, measured, worst_ratio=ratio)
            spent = cfg.ledger.spent()
            total = spent + sum(new.values()) + inp["tail"]   # the judge's estimate already prices every cap
            fits = total <= cfg.phase_cap + 1e-9
            note = {"stage": stage, "tripped_by": spec.key, "cost": cost, "spend_with_aborted_attempts": spend,
                    "estimate": est, "worst_ratio": ratio, "remaining": new,
                    "ledger_spent": spent, "judge_tail": inp["tail"], "total_if_all_run": total,
                    "cap": cfg.phase_cap, "decision": "continue" if fits else "stop"}
            cfg.day_dir.mkdir(parents=True, exist_ok=True)
            (cfg.day_dir / f"reestimate_s{stage}_{trips}.json").write_text(json.dumps(note, indent=1) + "\n",
                                                                         encoding="utf-8")
            log(f"tripwire: {spec.key} cost US${cost:.4f} passed its estimate US${est:.4f}; re-estimated "
                f"{len(new)} remaining runs from measured costs: spend US${spent:.4f} + remainder "
                f"US${sum(new.values()):.4f} + judge US${inp['tail']:.4f} = US${total:.4f} "
                f"{'<=' if fits else '>'} cap US${cfg.phase_cap:.2f}: {'continue' if fits else 'STOP'}")
            for r, _ in rest:
                log(f"  {r.key}: US${new[r.key]:.4f}")
            if not fits:
                out.update(rc=TRIPWIRE_STOP_RC, stopped="tripwire")
                return out
            seq[i:] = [(r, new[r.key]) for r, _ in seq[i:] if r.key in new] or seq[i:]
    if any(d["status"] == "failed" for d in out["done"]):
        out["rc"] = 4
    return out


# --------------------------------------------------------------------------
# Judging job C
# --------------------------------------------------------------------------

def _stored_judge(cfg: Config, name: str) -> Optional[dict]:
    """The stored ``judge_<job>.json`` whose verdicts are reused (ruling 14 adjustment), or None."""
    p = cfg.day_dir / f"judge_{name}.json"
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except ValueError:
        return None


async def run_judge(jobs: dict, cfg: Config, matrix, baseline, chat=llm.chat, log=print) -> Optional[dict]:
    """Judge direct against borg_cloud for every judged job. Writes ``judge_<job>.json``. Counted in the ledger.

    Verdicts already stored in ``judge_<job>.json`` (stage 1's repeat 1) are kept and reused, never re-judged:
    only pairs without a stored verdict are judged (with a fresh sanity case), and only their calls are new spend.
    If the new judging fails, the stored verdicts stay in the file beside the failure.
    Returns the result of the first judged job that did not complete, else the last one (None if nothing was
    judged); ``main`` exits non-zero when it did not complete."""
    results = None
    worst = None
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
        stored = _stored_judge(cfg, name)
        kept = {p["id"]: p for p in (stored or {}).get("pairs") or [] if p.get("winner") is not None}
        new_pairs = [p for p in pairs if p["id"] not in kept]
        if not new_pairs:
            log(f"judge {name}: every finished pair already has a stored verdict; nothing new to judge")
            results = stored
            continue
        jms = pick_judges(matrix, baseline, cfg.judge_model_ids)
        to_judge = sum(1 for p in new_pairs if not p["direct_failed"] and not p["borg_failed"])
        estimate = estimate_judge(job, to_judge, jms) if to_judge else 0.0
        cfg.ledger.check(estimate, cfg.phase_cap)
        key = load_answers(job, cfg)
        spent = {"dollars": 0.0}
        calls_log = new_calls_log(cfg, f"judge_{name}")
        entry = cfg.ledger.start(f"judge_{name}", kind="judge", calls_log=calls_log, date=cfg.date)
        logged = logging_chat(chat, calls_log)

        def make_ask(jm):
            async def ask(system: str, user: str) -> dict:
                r = await logged(jm, system, user, JUDGE_MAX_TOKENS)
                spent["dollars"] += r["dollars"]
                return r
            return ask

        reused = sorted(kept, key=lambda i: int(i.rsplit("_", 1)[-1]))
        prior_sanity = list((stored or {}).get("sanity") or [])
        prior_cost = float((stored or {}).get("cost_dollars") or 0.0)
        try:
            fresh = await judge.judge_all([make_ask(jm) for jm in jms], key, new_pairs)
            merged = sorted(list(kept.values()) + fresh["pairs"], key=lambda p: int(p["id"].rsplit("_", 1)[-1]))
            results = judge.summarise(merged, prior_sanity + fresh["sanity"], fresh["dollars"])
            results["status"] = "completed"
        except judge.JudgeRefused as exc:
            results = {"status": "refused", "reason": str(exc)}
        except Exception as exc:  # spend is still counted
            results = {"status": "failed", "reason": f"{type(exc).__name__}: {str(exc)[:300]}"}
        if results["status"] != "completed":
            # the stored verdicts are never lost: they stay in the file beside the failure
            results["pairs"] = [kept[i] for i in reused]
        spent["dollars"] = max(spent["dollars"], budget.calls_log_dollars(calls_log))
        results.update({"job": name, "judge_models": [jm.model_id for jm in jms], "estimate_dollars": estimate,
                        "cost_dollars": prior_cost + spent["dollars"], "this_call_dollars": spent["dollars"],
                        "reused_pairs": reused, "incomplete_repeats": incomplete})
        results["calls_logs"] = list((stored or {}).get("calls_logs") or
                                     ([stored["calls_log"]] if stored and stored.get("calls_log") else [])) + [str(calls_log)]
        results["calls_log"] = str(calls_log)
        cfg.ledger.finish(entry, spent["dollars"])
        cfg.day_dir.mkdir(parents=True, exist_ok=True)
        (cfg.day_dir / f"judge_{name}.json").write_text(json.dumps(results, indent=1) + "\n", encoding="utf-8")
        log(f"judge {name}: {results['status']}"
            + (f" direct_wins={results['direct_wins']} borg_wins={results['borg_wins']} ties={results['ties']}"
               f" no_verdict={results['no_verdict']} (reused {len(reused)} stored verdict(s))"
               if results["status"] == "completed" else f" ({results['reason']})"))
        if results["status"] != "completed" and worst is None:
            worst = results
    return worst or results


# --------------------------------------------------------------------------
# Dry run
# --------------------------------------------------------------------------

def _per_mtok(model) -> str:
    if model is None or model.pricing is None:
        return "no price"
    return f"${model.pricing.input_per_token * 1e6:.2f} / ${model.pricing.output_per_token * 1e6:.2f} per MTok"


def local_skipped(job: dict, arm: str, cfg: Config, matrix) -> bool:
    """True when this arm is local and will be written as "not run" (flag, or no checked local model)."""
    local = bool(arm_conf(job, arm).get("local")) or arm in LOCAL_ARMS
    return local and (cfg.skip_local or not local_model_available(matrix))


def _header(out, cfg, matrix, baseline, jms, title):
    out(f"BORG_ROUTE bench: {title} (date {cfg.date}; nothing is sent, nothing is spent)")
    out(f"matrix               : {matrix._path}  (read only; checks are in memory)")
    out(f"baseline, direct arm : {baseline.model_id}  ({_per_mtok(baseline)})")
    for jm in jms:
        out(f"judge, job C         : {jm.model_id}  ({_per_mtok(jm)})  [in neither arm; vendor '{vendor_of(jm.model_id)}']")
    checked = [m.model_id for m in matrix.all() if m.checked]
    out(f"checked models ({len(checked)}): {', '.join(checked)}")
    local_models = [m.model_id for m in matrix.all()
                    if m.checked and m.source in ("ollama", "lmstudio", "custom_key") and not m.remote]
    if cfg.skip_local:
        out("local arms           : NOT RUN (--skip-local); no numbers")
    elif local_models:
        out(f"local arms           : run on {', '.join(local_models)}; priced like the cloud BORG arm (upper bound)")
    else:
        out("local arms           : NOT RUN (no checked local model; check one with --check-model)")
    out(f"run time limit       : BORG_MAX_RUN_SECONDS={cfg.max_run_seconds} for every BORG arm; "
        f"direct arm HTTP timeout {cfg.direct_timeout_s:.0f} s")
    out(f"per-call timeouts    : BORG_ANSWERER_CALL_TIMEOUT={PINNED_ENV['BORG_ANSWERER_CALL_TIMEOUT']}, "
        f"BORG_ORCHESTRATOR_CALL_TIMEOUT={PINNED_ENV['BORG_ORCHESTRATOR_CALL_TIMEOUT']} (bench only; "
        "product defaults 120 s / 180 s)")
    out(f"routing              : pool = the {len(checked)} checked rows; receipts baseline {baseline.model_id}; "
        "pinned for every run:")
    pinned = [f"{k}={'(unset)' if v is None else v}" for k, v in PINNED_ENV.items() if "CALL_TIMEOUT" not in k]
    for k in range(0, len(pinned), 4):
        out("                       " + "  ".join(pinned[k:k + 4]))
    out(f"tools                : {'on' if TOOLS_ENABLED else 'OFF'} for every run, both arms (web search off)")
    out("transport errors     : a run failed by HTTP 429/5xx, a connection error or a timeout is rerun once "
        "(both arms); nothing else is rerun")
    out(f"job B                : needs Docker and the '{checks.BENCH_IMAGE}' image for every run, else all of job B is void")
    out("")


def _setup(matrix, baseline, cfg, out):
    if baseline is None:
        out("pre-flight: no checked model in the matrix, so the direct arm has no baseline. Nothing was spent.")
        return None
    try:
        return pick_judges(matrix, baseline, cfg.judge_model_ids)
    except ValueError as exc:
        out(f"pre-flight: {exc}")
        return None


def dry_run(jobs, cfg, matrix, baseline, only_jobs, arms_filter, repeats, job_repeats, out=print) -> int:
    """The full matrix (all repeats) with its conservative estimate. No limit is applied: the staged plan
    (``--dry-run --stage 1`` / ``--stage 2``) is what governs spending."""
    jms = _setup(matrix, baseline, cfg, out)
    if jms is None:
        return 2
    runs = build_matrix(jobs, only_jobs, arms_filter, repeats, job_repeats)
    pool = borg_price_model(matrix, baseline)

    def estimate(rs):  # runs written as "not run" spend nothing and are not estimated
        return estimate_matrix([r for r in rs if not local_skipped(jobs[r.job], r.arm, cfg, matrix)],
                               jobs, cfg, baseline, jms, pool)

    est = estimate(runs)
    _header(out, cfg, matrix, baseline, jms, "dry run")
    out(f"{'job':<4}{'arm':<20}{'lane_mode':<14}{'repeats':<9}{'est $/run':>10}{'est $ total':>13}")
    n_run = n_skip = 0
    for name, job in jobs.items():
        if only_jobs and name not in only_jobs:
            continue
        for arm in arms_of(job, arms_filter):
            rs = [r for r in runs if r.job == name and r.arm == arm]
            if not rs:
                continue
            skipped = local_skipped(job, arm, cfg, matrix)
            e = 0.0 if skipped else est["per_run"][rs[0].key]
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
    out(f"estimate, total       : US${est['total']:.2f}   (all repeats; paid runs go by --stage 1 then --stage 2)")
    out(f"spent so far (ledger) : US${cfg.ledger.spent():.4f}   phase cap US${cfg.phase_cap:.2f}")
    return 0


def dry_run_stage(stage, jobs, cfg, matrix, baseline, only_jobs, arms_filter, out=print) -> int:
    jms = _setup(matrix, baseline, cfg, out)
    if jms is None:
        return 2
    _header(out, cfg, matrix, baseline, jms, f"dry run, stage {stage}")
    spent = cfg.ledger.spent()
    if stage == 1:
        inp = stage_inputs(1, jobs, cfg, matrix, baseline, only_jobs, arms_filter, jms)
        out("stage 1: one repeat of every cell (job x arm), cheapest conservative estimate first.")
        out("Before each run: refuse if ledger spend + that run's estimate > the cap; a refusal stops the stage.")
        out(f"{'#':<4}{'run':<26}{'est $':>10}{'cumulative':>12}")
        cum = 0.0
        for k, (r, e) in enumerate(inp["sequence"], 1):
            cum += e
            tag = "  not run" if e == 0 and local_skipped(jobs[r.job], r.arm, cfg, matrix) else ""
            out(f"{k:<4}{r.key:<26}{e:>10.4f}{cum:>12.4f}{tag}")
        total = cum + inp["tail"]
        out(f"estimate, runs        : US${cum:.2f}   judge US${inp['tail']:.2f}   total US${total:.2f} (upper bounds)")
        out(f"spent so far (ledger) : US${spent:.4f}   phase cap US${cfg.phase_cap:.2f}")
        out("If every run cost its full estimate the stage would "
            + ("fit under the cap." if spent + total <= cfg.phase_cap else "stop at the guard before the end."))
        return 0
    measured = load_measured(cfg, jobs)
    if not measured:
        out(f"stage 2: no stage 1 result files in {cfg.day_dir}. Run stage 1 first (and pass its --date).")
        return 2
    inp = stage_inputs(2, jobs, cfg, matrix, baseline, only_jobs, arms_filter, jms)
    plan = inp["plan"]
    out("stage 2: extra repeats estimated per cell from stage 1's measured cost (dearest observed run, "
        "rounded up to the cent); repeats are added by whole job, order A, D, B, C,")
    out("both extra repeats, else one, only while ledger spend + that job's extra estimate <= the cap, and only for "
        "a job whose every repeat-1 cell finished. The decision looks at cost only.")
    out(f"spent so far (ledger) : US${spent:.4f}   phase cap US${cfg.phase_cap:.2f}")
    out(f"{'job':<5}{'decision':<10}{'repeats':<10}{'extra est $':>12}   per cell")
    decided = {j["job"]: ("add", j) for j in plan["jobs"]}
    decided.update({j["job"]: ("skip", j) for j in plan["skipped"]})
    for name in [n for n in STAGE_JOB_ORDER if n in decided] + [n for n in decided if n not in STAGE_JOB_ORDER]:
        d, j = decided[name]
        cells = ", ".join(f"{a} US${v:.2f}" for a, v in j["per_cell"].items())
        out(f"{name:<5}{d:<10}{','.join(map(str, j['repeats'])) or '-':<10}{j['extra_estimate']:>12.4f}   {cells}"
            + (f"   [{j['reason']}]" if d == "skip" and j.get("reason") else ""))
    out(f"runs to add: {len(plan['runs'])}   committed estimate US${plan['committed']:.4f}   "
        f"spend + committed US${spent + plan['committed']:.4f} of US${cfg.phase_cap:.2f}")
    return 0


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="BORG_ROUTE benchmark runner (work order 3.2, planner ruling 14).")
    ap.add_argument("--dry-run", action="store_true", help="print the matrix (or, with --stage, the plan) and spend nothing")
    ap.add_argument("--stage", type=int, choices=(1, 2),
                    help="1: one repeat of every cell, cheapest estimate first; 2: repeats 2 and 3 by whole job, "
                         "order A, D, B, C, from stage 1's measured cost")
    ap.add_argument("--jobs", help="comma list, e.g. A,C")
    ap.add_argument("--arms", help="comma list, e.g. direct,borg_cloud (also enables optional arms such as D's direct)")
    ap.add_argument("--repeats", type=int, help="cap repeats for every job (full-matrix dry run only)")
    ap.add_argument("--skip-local", action=argparse.BooleanOptionalAction, default=False,
                    help="write 'not run' for local arms (default off since ruling 13)")
    ap.add_argument("--judge-model", action="append", default=[],
                    help="model id for the job C judge (in neither arm, vendor outside both sides); "
                         "give it twice for two-judge mode (one per side's vendor), only when no outside vendor exists")
    ap.add_argument("--check-model", action="append", default=[], help="check this matrix row in memory for the run (never saved)")
    ap.add_argument("--prior-spend", type=float,
                    help="set the dollars already spent earlier in the phase (ledger): the 3.1 cloud rescoring "
                         "plus the ruling-13 scoring of the local model, summed")
    ap.add_argument("--no-docker", action="store_true", help="skip job B's container test step")
    ap.add_argument("--no-judge", action="store_true")
    ap.add_argument("--judge-only", action="store_true")
    ap.add_argument("--force", action="store_true", help="redo runs whose result file exists")
    ap.add_argument("--date", help="results folder date YYYY_MM_DD (default today)")
    ap.add_argument("--results-root")
    ap.add_argument("--matrix", default=str(DEFAULT_MATRIX),
                    help="the bench matrix file (default bench/pool_matrix.yaml); read only, never written")
    ap.add_argument("--baseline", help="pin the direct arm's model (a row of --matrix; it need not be checked)")
    ap.add_argument("--env-file", default=str(REPO_ROOT / ".env"),
                    help=".env read before the baseline is chosen (default the repo's .env)")
    args = ap.parse_args(argv)

    os.chdir(REPO_ROOT)  # the matrix, skills and prompts are read by relative path
    cfg = Config(skip_local=args.skip_local, use_docker=not args.no_docker,
                 judge_model_ids=list(args.judge_model), force=args.force)
    if args.date:
        cfg.date = args.date
    if args.results_root:
        cfg.results_root = Path(args.results_root)
    if args.prior_spend is not None and not args.dry_run:
        cfg.ledger.set_prior(args.prior_spend)

    only = args.jobs.split(",") if args.jobs else None
    arms = args.arms.split(",") if args.arms else None

    from dotenv import load_dotenv

    load_dotenv(args.env_file)  # before the baseline is chosen (BASELINE_MODEL_ID); the key is never printed
    jobs = load_jobs(cfg.jobs_dir)
    matrix = load_matrix(args.check_model, Path(args.matrix))
    try:
        baseline = baseline_model(matrix, args.baseline)
    except ValueError as exc:
        print(f"preflight: {exc}. Nothing was spent.")
        return 2

    if args.dry_run:
        if args.stage:
            return dry_run_stage(args.stage, jobs, cfg, matrix, baseline, only, arms)
        return dry_run(jobs, cfg, matrix, baseline, only, arms, args.repeats, {})

    if not args.stage and not args.judge_only:
        print("preflight: a paid run goes by stage: --stage 1, then --stage 2 (planner ruling 14). Nothing was spent.")
        return 2
    if not os.getenv("OPENROUTER_API_KEY"):
        print("preflight: OPENROUTER_API_KEY is not set (put it in .env). Nothing was spent.")
        return 2
    if baseline is None:
        print("preflight: no checked model in the matrix. Nothing was spent.")
        return 2
    try:
        jms = pick_judges(matrix, baseline, cfg.judge_model_ids)
    except ValueError as exc:
        print(f"preflight: {exc}. Nothing was spent.")
        return 2

    for e in cfg.ledger.recover():  # a crash in an earlier invocation: count what its database logged
        print(f"ledger: recovered unfinished entry {e['key']} (US${e['dollars']:.4f} from its database)")
    print(f"results folder: {cfg.day_dir}  (stage 2 and any resume must pass the same --date {cfg.date})")
    rc = 0
    judge_jobs = {n: j for n, j in jobs.items() if (j.get("quality") or {}).get("kind") == "pairwise_judge"}
    if args.stage and not args.judge_only:
        res = asyncio.run(run_stage(args.stage, jobs, cfg, matrix, baseline, only=only, arms=arms, jms=jms))
        rc = res["rc"]
        judge_jobs = {n: jobs[n] for n in res["judge_jobs"]}
    if not args.no_judge and judge_jobs and rc not in (3, TRIPWIRE_STOP_RC):
        try:
            jres = asyncio.run(run_judge(judge_jobs, cfg, matrix, baseline))
        except budget.BudgetRefused as exc:
            print(f"judge: {exc}; stopping (a guard failure is a stop).")
            rc = 3
        else:
            if jres is not None and jres.get("status") != "completed":
                print(f"judge: {jres.get('status')} ({jres.get('reason')}); exit code {JUDGE_FAILED_RC}")
                rc = JUDGE_FAILED_RC
    print(f"ledger: US${cfg.ledger.spent():.4f} spent of US${cfg.phase_cap:.2f}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
