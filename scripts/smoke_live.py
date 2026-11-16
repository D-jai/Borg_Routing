"""Live Check 1 smoke test (work order 1.10).

Runs one real multi-step prompt through ``run_pipeline`` against the real
OpenRouter models in ``data/capability_matrix.yaml`` and checks that routing
really spreads work across models, costs money in a sane range, and records
why each step went where it did.

THIS SPENDS REAL MONEY (expected well under US$0.50). It starts no server and
uses a fresh temporary SQLite database, so ``data/borg_route.db`` is untouched.

Run from the repo root:

    python scripts/smoke_live.py

Exit codes: 0 = PASS, 1 = FAIL, 2 = preflight refused (nothing was spent).
The last line printed is always the one-line summary (except on exit 2).
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

REPO_ROOT = Path(__file__).resolve().parent.parent

WALL_CLOCK_LIMIT_S = 600
MAX_SPEND_DOLLARS = 0.50
MIN_PRICED_MODELS = 3

PROMPT = (
    "From the 12 short product reviews below, extract product and sentiment for "
    "each, write a Python function that tallies them, and recommend which "
    "product to stock.\n\n"
    "1. Brewmaster kettle: boils fast and looks great, love it.\n"
    "2. Brewmaster kettle: the lid cracked after two weeks, very disappointed.\n"
    "3. Brewmaster kettle: does the job, nothing special.\n"
    "4. Brewmaster kettle: quiet, quick, and easy to clean.\n"
    "5. Lumo desk lamp: bright, adjustable, perfect for my desk.\n"
    "6. Lumo desk lamp: the switch broke in a month, would not buy again.\n"
    "7. Lumo desk lamp: fine for the price, average build.\n"
    "8. Lumo desk lamp: flickers at night, returned it.\n"
    "9. Trekpak backpack: comfortable on long hikes, great pockets.\n"
    "10. Trekpak backpack: zipper held up through a rough trip, very happy.\n"
    "11. Trekpak backpack: a bit heavy but sturdy, decent value.\n"
    "12. Trekpak backpack: straps frayed early, not impressed.\n"
)

# --------------------------------------------------------------------------
# Preflight
# --------------------------------------------------------------------------


def priced_openrouter_models(models) -> list:
    """Checked OpenRouter models with a positive input price."""
    return [
        m for m in models
        if m.checked
        and m.source == "openrouter"
        and m.pricing is not None
        and m.pricing.input_per_token > 0
    ]


def preflight(models) -> tuple[bool, str]:
    """Need at least three checked priced OpenRouter models whose input prices
    differ; otherwise the 'two or more models' check cannot mean anything."""
    priced = priced_openrouter_models(models)
    prices = {m.pricing.input_per_token for m in priced}
    if len(priced) < MIN_PRICED_MODELS or len(prices) < MIN_PRICED_MODELS:
        return False, (
            f"preflight: need at least {MIN_PRICED_MODELS} checked, priced OpenRouter "
            f"models with distinct input prices; found {len(priced)} priced "
            f"model(s) with {len(prices)} distinct input price(s). Nothing was spent."
        )
    return True, (
        f"preflight ok: {len(priced)} priced OpenRouter models, "
        f"{len(prices)} distinct input prices"
    )


# --------------------------------------------------------------------------
# Reading the run tree and judging it
# --------------------------------------------------------------------------

_TREE_CTE = """WITH RECURSIVE tree(run_id) AS (
    SELECT ?
    UNION
    SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id
)"""


def _tree_ids(conn: sqlite3.Connection, run_id: str) -> list[str]:
    rows = conn.execute(
        _TREE_CTE + " SELECT run_id FROM tree", (run_id,)
    ).fetchall()
    return [r[0] for r in rows]


def read_run_tree(db_path: str, run_id: Optional[str]) -> dict:
    """Everything the checks need, read from the database for the root run and
    every descendant run."""
    out = {"run_id": run_id, "models": [], "steps": [], "dollars": 0.0, "baseline": 0.0}
    if not run_id:
        return out
    with sqlite3.connect(db_path) as conn:
        ids = _tree_ids(conn, run_id)
        marks = ",".join("?" for _ in ids)

        # Same sum the run digest uses: model_call + orchestrator_decision rows.
        row = conn.execute(
            _TREE_CTE
            + """ SELECT COALESCE(SUM(dollars), 0), COALESCE(SUM(baseline_dollars), 0)
                  FROM logs WHERE run_id IN (SELECT run_id FROM tree)
                  AND event_type IN ('model_call','orchestrator_decision')""",
            (run_id,),
        ).fetchone()
        out["dollars"], out["baseline"] = float(row[0]), float(row[1])

        # Models that served steps: model_call rows (the planner and compose
        # calls are orchestrator_decision rows), minus tool invocations.
        out["models"] = [
            r[0] for r in conn.execute(
                f"SELECT DISTINCT model_id FROM logs WHERE run_id IN ({marks}) "
                "AND event_type='model_call' AND model_id IS NOT NULL "
                "AND model_id NOT LIKE 'tool:%' ORDER BY model_id",
                ids,
            )
        ]

        step_dollars: dict[str, float] = {}
        for env_id, d in conn.execute(
            f"SELECT envelope_id, COALESCE(SUM(dollars), 0) FROM logs "
            f"WHERE run_id IN ({marks}) AND event_type='model_call' "
            "AND envelope_id IS NOT NULL GROUP BY envelope_id",
            ids,
        ):
            step_dollars[env_id] = float(d)

        for env_id, dj in conn.execute(
            f"SELECT envelope_id, decision_json FROM logs WHERE run_id IN ({marks}) "
            "AND event_type='orchestrator_decision' AND envelope_id IS NOT NULL "
            "ORDER BY log_id",
            ids,
        ):
            try:
                decision = json.loads(dj or "null")
            except (TypeError, ValueError):
                continue
            if isinstance(decision, dict) and decision.get("mode") == "route":
                out["steps"].append({
                    "step": decision.get("node_id") or env_id,
                    "envelope_id": env_id,
                    "decision": decision,
                    "dollars": step_dollars.get(env_id, 0.0),
                })
    return out


def judge(tree: dict, run_status: str = "completed") -> tuple[list[str], list[str]]:
    """Return (failures, report_lines)."""
    failures: list[str] = []
    lines: list[str] = []

    lines.append("per step:")
    for s in tree["steps"]:
        d = s["decision"]
        lines.append(
            f"  step={s['step']} task_type={d.get('task_type')} tier={d.get('tier')} "
            f"chosen={d.get('chosen')} reason={d.get('reason')} "
            f"dollars=${s['dollars']:.6f}"
        )
    if not tree["steps"]:
        lines.append("  (no routed steps recorded)")

    if run_status != "completed":
        failures.append(f"run status is '{run_status}', not 'completed'")

    models = tree["models"]
    if len(models) < 2:
        failures.append(
            f"(1) need 2+ distinct models serving steps; got {len(models)}: {models}"
        )

    if not tree["dollars"] > 0:
        failures.append(f"(2) total dollars must be > 0; got {tree['dollars']:.6f}")
    elif not tree["dollars"] < MAX_SPEND_DOLLARS:
        failures.append(
            f"(2) total dollars must be < {MAX_SPEND_DOLLARS:.2f}; got {tree['dollars']:.6f}"
        )

    if not tree["steps"]:
        failures.append("(3) no routed steps (orchestrator_decision mode 'route') found")
    for s in tree["steps"]:
        d = s["decision"]
        if not d.get("reason"):
            failures.append(f"(3) step {s['step']}: routing decision has no reason")
        if not d.get("chosen"):
            failures.append(f"(3) step {s['step']}: routing decision has no chosen model")
        if d.get("reason") == "routed" and not (d.get("candidates") or []):
            failures.append(f"(3) step {s['step']}: reason 'routed' but no candidates")
    return failures, lines


def summary_line(passed: bool, tree: dict, elapsed: float, db_path: str) -> str:
    return (
        f"CHECK1 {'PASS' if passed else 'FAIL'} models={len(tree['models'])} "
        f"steps={len(tree['steps'])} spend=${tree['dollars']:.6f} "
        f"baseline_est=${tree['baseline']:.6f} elapsed={elapsed:.1f}s db={db_path}"
    )


def evaluate(
    db_path: str,
    run_id: Optional[str],
    elapsed: float,
    run_status: str = "completed",
    extra_failures: Optional[list[str]] = None,
) -> tuple[bool, list[str], str]:
    """Judge a finished run. Returns (passed, report_lines, summary_line)."""
    tree = read_run_tree(db_path, run_id)
    failures, lines = judge(tree, run_status)
    failures = list(extra_failures or []) + failures
    passed = not failures
    lines.append(f"models: {', '.join(tree['models']) or '(none)'}")
    for f in failures:
        lines.append(f"FAILED: {f}")
    return passed, lines, summary_line(passed, tree, elapsed, db_path)


# --------------------------------------------------------------------------
# Live run
# --------------------------------------------------------------------------


def _latest_root_run(db_path: str) -> Optional[str]:
    try:
        with sqlite3.connect(db_path) as conn:
            row = conn.execute(
                "SELECT run_id FROM runs WHERE parent_run_id IS NULL "
                "ORDER BY created_at DESC LIMIT 1"
            ).fetchone()
        return row[0] if row else None
    except sqlite3.Error:
        return None


async def _live(db_path: str) -> tuple[Optional[str], str, list[str]]:
    from src.db.schema import create_all
    from src.pipeline.runner import run_pipeline

    await create_all(db_path)
    extra: list[str] = []
    try:
        result = await asyncio.wait_for(
            run_pipeline(PROMPT, db_path=db_path), timeout=WALL_CLOCK_LIMIT_S
        )
    except asyncio.TimeoutError:
        extra.append(f"timeout: run exceeded the {WALL_CLOCK_LIMIT_S}s wall-clock limit")
        return _latest_root_run(db_path), "failed", extra
    if result.get("error"):
        extra.append(f"pipeline error: {result['error']}")
    return result.get("run_id"), result.get("status", "failed"), extra


def main() -> int:
    os.chdir(REPO_ROOT)  # the matrix and skills are read by relative path
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))

    from dotenv import load_dotenv

    load_dotenv(REPO_ROOT / ".env")  # the key itself is never printed
    if not os.getenv("OPENROUTER_API_KEY"):
        print("preflight: OPENROUTER_API_KEY is not set (put it in .env). Nothing was spent.")
        return 2

    from src.registry.capability_matrix import CapabilityMatrix

    matrix = CapabilityMatrix()
    matrix.load()
    ok, msg = preflight(matrix.all())
    print(msg)
    if not ok:
        return 2

    db_dir = tempfile.mkdtemp(prefix="borg_smoke_")
    db_path = str(Path(db_dir) / "borg_smoke.db")
    print(f"running live (limit {WALL_CLOCK_LIMIT_S}s); db={db_path}")

    t0 = time.monotonic()
    run_id, status, extra = asyncio.run(_live(db_path))
    elapsed = time.monotonic() - t0

    passed, lines, summary = evaluate(db_path, run_id, elapsed, status, extra)
    for line in lines:
        print(line)
    print(summary)
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(main())
