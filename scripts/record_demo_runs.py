"""Planner ruling 24: record the demo runs that replay mode (work order 4.5) plays back.

SPENDS MONEY (cloud calls, capped at US$1.00 in total). Run it only when the lead says so,
after reading this file:

    python scripts/record_demo_runs.py --dry-run          # no model call; checks and estimate
    python scripts/record_demo_runs.py --job 1            # one job
    python scripts/record_demo_runs.py --job all          # all three

Every run is a "demo run": labelled so in the budget ledger, in demo/runs/manifest.json and in
everything printed. Demo runs back no claim (see demo/README.md).

Entry point: ``run_pipeline`` (src/pipeline/runner.py), the function behind the Ask page's
``POST /v1/chat/completions`` with the ``borg_route`` model. It creates the run row, runs the
orchestrator (envelopes, events, routing decisions, costs, outbound ledger), writes the run
digest with the one-pass floor and returns the answer. It takes ``db_path``, so everything goes
to one dedicated, gitignored database (demo/runs/db/demo.db), never the app's main database.

The three jobs (data in demo/data/, all fictional):

1. Bulk small items: 60 feedback lines, one classification and one field per line. Lane off.
2. Mixed job: compare two short policies and draft a recommendation memo with a table. Lane off.
3. Lane run: 10 internal notes, one holding a canary string, read as documents in ``raw_local``.
   The notes are read only by models on this machine (llama3.2:3b on Ollama, the bench's scored local row); only the planner
   and the write-up may be cloud models and they see the manifest and short checked outputs, never
   the note text. ``all_local`` would also keep the notes home but would need the 3B local model to
   plan and write up, which it does not do reliably, and would show no cloud routing at all.
"""
from __future__ import annotations

import argparse
import asyncio
import http.client
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA_DIR = ROOT / "demo" / "data"
RUNS_DIR = ROOT / "demo" / "runs"
DB_FILE = RUNS_DIR / "db" / "demo.db"
MANIFEST = RUNS_DIR / "manifest.json"
SPEND_FILE = ROOT / "bench" / "results" / "spend.json"
MATRIX_FILE = ROOT / "data" / "capability_matrix.yaml"
CAP_DOLLARS = 1.00
LABEL = "demo run"
OLLAMA_HOST, OLLAMA_PORT = "localhost", 11434
LOCAL_MODELS = ("llama3.2:3b",)

# Rough token use per job (planner, workers, write-up) for the dry-run estimate only. The
# estimate prices them at the dearest ticked cloud model, so it is a ceiling, not a forecast.
EST_TOKENS = {"1": (14_000, 6_000), "2": (22_000, 7_000), "3": (4_000, 2_000)}
JOB_TITLES = {"1": "bulk small items", "2": "mixed job", "3": "lane run"}


def _read_data(path: Path) -> str:
    """A demo data file without its header line (the first line, which starts with '#')."""
    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines and lines[0].startswith("#"), f"{path.name} has no header line"
    return "\n".join(lines[1:]).strip()


def build_requests() -> list[dict]:
    """The three requests, built from files only (no network)."""
    feedback = _read_data(DATA_DIR / "job1_feedback.txt").splitlines()
    numbered = "\n".join(f"{i}. {t}" for i, t in enumerate(feedback, 1))
    job1 = (
        f"Below are {len(feedback)} customer feedback lines about a smart water bottle app. "
        "For each line, classify the sentiment as positive, negative, neutral or mixed, and "
        "extract the one product feature it mentions (or 'none'). Treat every line on its own. "
        "Then write a short summary: how many lines per sentiment and the three most mentioned "
        "features, with the full per-line table placed in the answer.\n\n" + numbered
    )
    pol_a = _read_data(DATA_DIR / "job2_policy_a.md")
    pol_b = _read_data(DATA_DIR / "job2_policy_b.md")
    job2 = (
        "A company is weighing two remote work policies. Compare them point by point, pick out "
        "the risks and costs of the proposed one (work out the yearly cost difference per person "
        "for the allowances and equipment), and draft a short recommendation memo for the "
        "leadership team with a comparison table.\n\nPOLICY A (current):\n" + pol_a
        + "\n\nPOLICY B (proposed):\n" + pol_b
    )
    job3 = (
        "Read each of the supplied internal notes. For every note give its topic, its owner and "
        "whether it needs follow-up (yes or no) in a few words. Then list the notes that need "
        "follow-up. Keep each field short; do not repeat note text."
    )
    return [
        {"job": "1", "instruction": job1, "documents": None, "lane_mode": "off", "docs_root": None},
        {"job": "2", "instruction": job2, "documents": None, "lane_mode": "off", "docs_root": None},
        {"job": "3", "instruction": job3, "documents": {"folder": "job3_notes"},
         "lane_mode": "raw_local", "docs_root": str(DATA_DIR)},
    ]


# --- checks (no model call) ----------------------------------------------------------------


def probe_ollama(timeout: float = 2.0) -> dict:
    """Is Ollama reachable, and which of the two models does it list? A plain GET of the tag list
    (not a model call, no httpx)."""
    try:
        conn = http.client.HTTPConnection(OLLAMA_HOST, OLLAMA_PORT, timeout=timeout)
        conn.request("GET", "/api/tags")
        data = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        names = {m.get("name", "") for m in data.get("models", [])}
        return {"reachable": True, "models": {m: m in names for m in LOCAL_MODELS}}
    except Exception as exc:  # noqa: BLE001
        return {"reachable": False, "error": type(exc).__name__, "models": {}}


def _round1_routing_config() -> dict:
    """The routing configuration the round-1 BORG cloud arm stored in its result file: the checked pool
    and the receipts baseline. Read from there, not copied by hand."""
    files = sorted((ROOT / "bench" / "results").glob("*/*_borg_cloud_*.json"))
    for f in files:
        rc = (json.loads(f.read_text(encoding="utf-8")) or {}).get("routing_config")
        if rc and rc.get("pool"):
            return {"pool": rc["pool"], "baseline_model_id": rc.get("baseline_model_id"),
                    "source": f.relative_to(ROOT).as_posix()}
    raise SystemExit("no round-1 BORG result file with a routing_config in bench/results/")


def load_matrix():
    """The bench's proof pool, checked in memory: ``bench/pool_matrix.yaml`` through run_bench's own
    ``load_matrix`` (its ``save`` raises, so no matrix file is ever written), with the rows the
    round-1 BORG arms checked."""
    from bench import run_bench

    rc = _round1_routing_config()
    matrix = run_bench.load_matrix(rc["pool"], run_bench.DEFAULT_MATRIX)
    matrix.demo_baseline_id = rc["baseline_model_id"]
    matrix.demo_pool_source = rc["source"]
    return matrix


def _is_cloud(m) -> bool:
    return m.source in ("openrouter", "custom_key") and not m.remote


def matrix_report(matrix) -> dict:
    ticked = [m for m in matrix.all() if m.checked]
    cloud = [m for m in ticked if _is_cloud(m)]
    local = [m for m in ticked if not _is_cloud(m)]
    scored_local = [m for m in local if m.task_scores]
    top = max(((m.pricing.input_per_token, m.pricing.output_per_token) for m in cloud if m.pricing),
              default=(0.0, 0.0))
    return {"models": len(matrix.all()), "baseline": getattr(matrix, "demo_baseline_id", None),
            "pool_source": getattr(matrix, "demo_pool_source", None), "ticked_cloud": [m.model_id for m in cloud],
            "ticked_local": [m.model_id for m in local],
            "scored_local": [m.model_id for m in scored_local], "dearest_price_per_token": top}


def estimate(job: str, report: dict) -> float:
    tin, tout = EST_TOKENS[job]
    pin, pout = report["dearest_price_per_token"]
    return round(tin * pin + tout * pout, 4)


def keys_report() -> dict:
    """Which keys are set (names only, never values)."""
    from dotenv import dotenv_values

    set_ = bool(os.environ.get("OPENROUTER_API_KEY") or dotenv_values(ROOT / ".env").get("OPENROUTER_API_KEY"))
    return {"OPENROUTER_API_KEY": set_}


def problems(job: str, report: dict, ollama: dict | None, keys: dict) -> list[str]:
    out = []
    if not report["ticked_cloud"]:
        out.append("no checked cloud model in the bench pool")
    if not keys["OPENROUTER_API_KEY"]:
        out.append("OPENROUTER_API_KEY is not set (.env)")
    if job == "3":
        if not report["scored_local"]:
            out.append("no checked, scored local model in the bench pool")
        if ollama is not None:
            if not ollama["reachable"]:
                out.append(f"Ollama is not reachable at {OLLAMA_HOST}:{OLLAMA_PORT}")
            else:
                missing = [m for m, ok in ollama["models"].items() if not ok]
                if missing:
                    out.append("Ollama does not list: " + ", ".join(missing))
    return out


def dry_run(jobs: list[str], probe: bool) -> int:
    requests = [r for r in build_requests() if r["job"] in jobs]
    report = matrix_report(load_matrix())
    keys = keys_report()
    ollama = probe_ollama() if probe else None
    print(f"DRY RUN ({LABEL}s): no model call, nothing written, no money spent.")
    print(f"matrix: bench/pool_matrix.yaml checked in memory (never written), {report['models']} rows; "
          f"pool as stored in {report['pool_source']}")
    print(f"checked pool, cloud ({len(report['ticked_cloud'])}): {report['ticked_cloud']}")
    print(f"checked pool, local: {report['ticked_local']} (scored: {report['scored_local']})")
    print(f"receipts baseline (BASELINE_MODEL_ID): {report['baseline']}")
    print(f"dearest checked worker prices per million tokens: input US${report['dearest_price_per_token'][0]*1e6:.2f}, "
          f"output US${report['dearest_price_per_token'][1]*1e6:.2f}")
    print(f"keys set: {keys}")
    print("ollama: " + ("not probed (--no-probe)" if ollama is None else json.dumps(ollama)))
    total, blocked = 0.0, False
    for r in requests:
        est = estimate(r["job"], report)
        total += est
        issues = problems(r["job"], report, ollama, keys)
        blocked = blocked or bool(issues)
        print(f"\n{LABEL} job {r['job']} ({JOB_TITLES[r['job']]}): lane_mode={r['lane_mode']}, "
              f"documents={r['documents']}, request {len(r['instruction'])} characters, "
              f"estimated spend up to US${est:.4f}")
        print("  request starts: " + r["instruction"][:110].replace("\n", " ") + " ...")
        for i in issues:
            print(f"  WOULD BLOCK: {i}")
    print(f"\n{len(requests)} {LABEL}s would run; estimated total spend up to US${total:.4f} "
          f"(cap US${CAP_DOLLARS:.2f}; local steps cost nothing).")
    print("a live run would be refused for the reasons above" if blocked else "no blocker found")
    return 0


# --- live run ----------------------------------------------------------------------------


def _add_manifest(entry: dict) -> None:
    MANIFEST.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else []
    data.append(entry)
    MANIFEST.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def save_answer(run_id: str, text: object, folder: Path = DB_FILE.parent / "answers") -> Optional[Path]:
    """Keep the run's final answer beside the demo database (``answers/<run_id>.md``, git-ignored
    with it): the run database does not store it, and the replay export (scripts/export_run.py,
    work order 4.5) reads it from here. Written byte for byte (no newline translation)."""
    if not run_id or not isinstance(text, str):
        return None
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / f"{run_id}.md"
    path.write_text(text, encoding="utf-8", newline="")
    return path


async def record(jobs: list[str]) -> int:
    from bench.budget import Ledger, logged_dollars
    from src.agents import executor
    from src.db.schema import create_all
    from src.db.state import get_run
    from src.orchestrator import engine
    from src.pipeline.runner import run_pipeline

    if not os.environ.get("OPENROUTER_API_KEY"):
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    requests = [r for r in build_requests() if r["job"] in jobs]
    matrix = load_matrix()
    report = matrix_report(matrix)
    keys = keys_report()
    ollama = probe_ollama() if "3" in jobs else None
    refusals = [f"job {r['job']}: {p}" for r in requests
                for p in problems(r["job"], report, ollama, keys)]
    if refusals:
        print("REFUSED before anything was spent:\n  " + "\n  ".join(refusals))
        return 2

    ledger = Ledger(SPEND_FILE)
    ledger.check(CAP_DOLLARS)
    DB_FILE.parent.mkdir(parents=True, exist_ok=True)
    db = str(DB_FILE)
    await create_all(db)
    engine._shared_matrix = matrix
    os.environ.setdefault("BORG_PACK_MAX_ITEMS", "20")

    real_send = executor.send_outbound

    async def capped_send(*args, **kwargs):
        if logged_dollars(db) > CAP_DOLLARS:
            raise RuntimeError(f"{LABEL} stopped: logged dollars passed US${CAP_DOLLARS:.2f}")
        return await real_send(*args, **kwargs)

    executor.send_outbound = capped_send
    code = 0
    try:
        for r in requests:
            key = f"demo_run_{r['job']}"
            print(f"\n[{LABEL}] job {r['job']} ({JOB_TITLES[r['job']]}), lane_mode={r['lane_mode']}")
            entry = ledger.start(key, kind="demo", db_path=db)
            spend, run_id, status, note = 0.0, None, "failed", None
            try:
                # the same pinned environment, baseline, run time limit and web-search-off the bench uses
                from bench import run_bench

                env = dict(run_bench.PINNED_ENV)
                env.update({"BORG_LANE_MODE": r["lane_mode"] if r["lane_mode"] != "off" else None,
                            "BORG_MAX_RUN_SECONDS": str(run_bench.MAX_RUN_SECONDS),
                            "BASELINE_MODEL_ID": report["baseline"],
                            "BORG_DOCS_ROOT": r["docs_root"]})
                with run_bench._Env(**env), run_bench.tools_off():
                    result = await run_pipeline(r["instruction"], db_path=db, documents=r["documents"])
                run_id, status = result.get("run_id"), result.get("status")
                save_answer(run_id, result.get("response"))
                spend = float(result.get("cost_dollars") or 0.0)
                note = result.get("error")
                print(f"[{LABEL}] job {r['job']}: status {status}, run {run_id}, "
                      f"US${spend:.4f}, {result.get('envelope_count')} envelopes"
                      + (f", error: {note}" if note else ""))
            finally:
                ledger.finish(entry, spend, note=f"{LABEL}")
            lane_mode = None
            if run_id:
                import uuid

                lane_mode = (await get_run(uuid.UUID(run_id), db_path=db)).get("lane_mode")
                _add_manifest({"label": LABEL, "run_id": run_id, "job": r["job"],
                               "title": JOB_TITLES[r["job"]], "date": datetime.now().strftime("%Y-%m-%d"),
                               "lane_mode": lane_mode, "spend_dollars": round(spend, 8),
                               "status": status, "db": "demo/runs/db/demo.db"})
            if status != "completed":
                code = 1
            if logged_dollars(db) > CAP_DOLLARS:
                print(f"[{LABEL}] cap of US${CAP_DOLLARS:.2f} reached: stopping")
                break
    finally:
        executor.send_outbound = real_send
    print(f"\n[{LABEL}] total logged in the demo database: US${logged_dollars(db):.4f}; "
          f"manifest: {MANIFEST}")
    return code


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Record the ruling-24 demo runs (spends money unless --dry-run).")
    ap.add_argument("--job", choices=["1", "2", "3", "all"], default="all")
    ap.add_argument("--dry-run", action="store_true", help="check and estimate only; no model call")
    ap.add_argument("--no-probe", action="store_true", help="dry run: do not even probe Ollama")
    args = ap.parse_args(argv)
    jobs = ["1", "2", "3"] if args.job == "all" else [args.job]
    if args.dry_run:
        return dry_run(jobs, probe=not args.no_probe)
    return asyncio.run(record(jobs))


if __name__ == "__main__":
    raise SystemExit(main())
