"""Planner ruling 23 live check (23d, 23g): 40 labelled lines, unpacked and packed,
on one cloud worker. SPENDS MONEY (about US$0.02, capped at US$0.10).

Run it only when the lead says so, after the review:

    python scripts/live_check_packing.py

What it does:

- Loads ``tests/fixtures/packing_live_items.json`` (40 lines written fresh for
  this check, labelled and committed before any run; never from ``bench/``).
- Builds one apply_to_each task directly (no planner, no write-up, so only
  worker calls are paid), pinned to one cloud worker, and runs it twice on one
  kept database: ``BORG_PACK_MAX_ITEMS=0`` (unpacked), then ``20`` (packed).
- Budget: ``Ledger.check(0.10)`` against the phase cap before anything is
  spent; ``start(..., db_path=<kept db>)`` so ``recover()`` can close a crashed
  run; ``finish`` with the dollars logged in that database. Any post after the
  logged dollars pass US$0.10 is refused before it is sent.
- Reports (23d, 23g): items accepted from a pack on their first post and the
  accuracy on those; the accuracy on all 40 final rows, packed and unpacked
  (a failed or declined row counts as wrong); the items whose answers differ;
  calls, retries, tokens and dollars per arm.
- Rule: fewer than 32 of 40 accepted on first post is INCONCLUSIVE (report to
  the planner, naming the failure behind the retries; change nothing). At 32 or
  more, the 2-item rule decides on the final rows: packed more than 2 items
  worse than unpacked is REPORT TO PLANNER (change nothing); otherwise PASS.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import uuid
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

WORKER = "openrouter/google/gemini-3.1-flash-lite-preview"
ITEMS_FILE = ROOT / "tests" / "fixtures" / "packing_live_items.json"
SPEND_FILE = ROOT / "bench" / "results" / "spend.json"
LEDGER_KEY = "ruling23_live_check"
CAP_DOLLARS = 0.10
_LIVE_MIN_ACCEPTED = 32          # ruling 23g: fewer first-post acceptances is inconclusive
_WORSE_BY = 2                    # ruling 23: packed more than 2 items worse -> report

OPERATOR = "For each short line, say which kind of quantity it mentions."
TASK = (
    "Name the kind of quantity this line mentions: length, mass, time, temperature or volume. "
    "If it mentions none of these, answer none."
)
SCHEMA = {"kind": "length|mass|time|temperature|volume|none"}


def _post_count_sql():
    from src.orchestrator.packing import COUNTS_AS_CALL_SQL

    return COUNTS_AS_CALL_SQL


def _arm_numbers(db: str, run_id: str) -> dict:
    with sqlite3.connect(db) as conn:
        calls, dollars, t_in, t_out = conn.execute(
            f"SELECT COALESCE(SUM({_post_count_sql()}),0), COALESCE(SUM(dollars),0), "
            "COALESCE(SUM(tokens_input),0), COALESCE(SUM(tokens_output),0) "
            "FROM logs WHERE run_id=? AND event_type='model_call'", (run_id,)).fetchone()
        outcomes = [json.loads(r[0]) for r in conn.execute(
            "SELECT decision_json FROM logs WHERE run_id=? AND event_type='orchestrator_decision' "
            "ORDER BY log_id", (run_id,)) if r[0]]
    outcomes = [o for o in outcomes if o.get("mode") == "pack_outcome"]
    failures = {k: sum(int(o.get(k) or 0) for o in outcomes)
                for k in ("missing", "duplicate", "unknown_id", "invalid", "declined", "alone")}
    for flag in ("cut", "timed_out", "raised"):
        failures[flag] = sum(1 for o in outcomes if o.get(flag))
    return {"calls": int(calls), "dollars": float(dollars), "tokens_in": int(t_in),
            "tokens_out": int(t_out), "packed_posts": len(outcomes), "pack_failures": failures}


def _first_post_accepted(db: str, run_id: str, child_of: dict) -> set:
    """Indexes whose first packed post filed them from the pack value."""
    first: dict = {}
    with sqlite3.connect(db) as conn:
        for env_id, dj in conn.execute(
                "SELECT envelope_id, decision_json FROM logs WHERE run_id=? AND event_type='model_call' "
                "ORDER BY log_id", (run_id,)):
            try:
                pack = (json.loads(dj or "null") or {}).get("pack")
            except (TypeError, ValueError, AttributeError):
                pack = None
            if isinstance(pack, dict) and env_id not in first:
                first[env_id] = bool(pack.get("accepted"))
    return {child_of[e] for e, ok in first.items() if ok and e in child_of}


async def _run_arm(pack_value: str, items: list, db: str) -> tuple[str, dict, dict]:
    from src.envelopes.schema import WrappedTask
    from src.orchestrator import apply_to_each

    os.environ["BORG_PACK_MAX_ITEMS"] = pack_value
    child_of: dict = {}
    real_make = apply_to_each._make_child_task

    def recording(parent, item, index, suffix):
        child = real_make(parent, item, index, suffix)
        child_of[str(child.envelope_id)] = index
        return child

    apply_to_each._make_child_task = recording
    try:
        task = WrappedTask(
            envelope_id=uuid.uuid4(), run_id=uuid.uuid4(), subtask_id=uuid.uuid4(),
            type="apply_to_each", current_depth=1, max_depth=5, level_path="1",
            instruction=TASK, governance_instruction=OPERATOR, output_schema=SCHEMA,
            dependency_input={"items": items, "concurrency_limit": 5},
            model_endpoint="https://openrouter.ai/api", model_name=WORKER,
        )
        env = await apply_to_each.expand_and_run(task, db_path=db)
    finally:
        apply_to_each._make_child_task = real_make
    from src.logging.writer import flush_pending

    await flush_pending()
    rows: dict = {}
    for r in env.output["results"]:
        out = r.get("output") or {}
        rows[r["index"]] = str(out.get("kind", "")).strip().lower()
    for f in env.output["failures"]:
        rows[f["index"]] = None
    return str(task.run_id), rows, child_of


async def main() -> int:
    from bench.budget import Ledger, logged_dollars
    from src.agents import executor
    from src.db.schema import create_all
    from src.orchestrator import engine
    from src.registry.capability_matrix import CapabilityMatrix

    data = json.loads(ITEMS_FILE.read_text(encoding="utf-8"))
    entries = data["items"]
    assert len(entries) == 40 and all(e["label"] in data["labels"] for e in entries)
    items = [e["text"] for e in entries]
    labels = [e["label"] for e in entries]

    if not os.environ.get("OPENROUTER_API_KEY"):
        from dotenv import load_dotenv  # noqa: PLC0415

        load_dotenv(ROOT / ".env")
    ledger = Ledger(SPEND_FILE)
    ledger.check(CAP_DOLLARS)

    day = datetime.now().strftime("%Y_%m_%d")
    db_dir = ROOT / "bench" / "results" / day / "dbs"
    db_dir.mkdir(parents=True, exist_ok=True)
    db = str(db_dir / f"{LEDGER_KEY}.db")
    await create_all(db)
    engine._shared_matrix = CapabilityMatrix()
    if engine._shared_matrix.get(WORKER) is None:
        raise SystemExit(f"{WORKER} is not in the matrix")

    real_send = executor.send_outbound

    async def capped_send(*args, **kwargs):
        if logged_dollars(db) > CAP_DOLLARS:
            raise RuntimeError(f"live check stopped: logged dollars passed US${CAP_DOLLARS:.2f}")
        return await real_send(*args, **kwargs)

    executor.send_outbound = capped_send
    entry = ledger.start(LEDGER_KEY, kind="check", db_path=db)
    try:
        run_u, rows_u, _ = await _run_arm("0", items, db)
        run_p, rows_p, child_of = await _run_arm("20", items, db)
    finally:
        executor.send_outbound = real_send
        ledger.finish(entry, logged_dollars(db))

    def correct(rows, which=None):
        idx = range(40) if which is None else which
        return sum(1 for i in idx if rows.get(i) == labels[i])

    first_ok = _first_post_accepted(db, run_p, child_of)
    numbers_u, numbers_p = _arm_numbers(db, run_u), _arm_numbers(db, run_p)
    differ = [{"id": entries[i]["id"], "text": items[i], "label": labels[i],
               "unpacked": rows_u.get(i), "packed": rows_p.get(i)}
              for i in range(40) if rows_u.get(i) != rows_p.get(i)]
    acc_u, acc_p = correct(rows_u), correct(rows_p)
    if len(first_ok) < _LIVE_MIN_ACCEPTED:
        verdict = "INCONCLUSIVE: report to the planner, change nothing"
    elif acc_p < acc_u - _WORSE_BY:
        verdict = "REPORT TO PLANNER: packed is more than 2 items worse; change nothing"
    else:
        verdict = "PASS"
    report = {
        "worker": WORKER,
        "first_post_accepted": len(first_ok),
        "accuracy_on_first_post_accepted": f"{correct(rows_p, sorted(first_ok))}/{len(first_ok)}",
        "accuracy_unpacked": f"{acc_u}/40",
        "accuracy_packed": f"{acc_p}/40",
        "differ_count": len(differ),
        "differ": differ,
        "unpacked": numbers_u,
        "packed": numbers_p,
        "spend_logged_dollars": logged_dollars(db),
        "verdict": verdict,
    }
    out = ROOT / "bench" / "results" / day / f"{LEDGER_KEY}.json"
    out.write_text(json.dumps(report, indent=1, ensure_ascii=False), encoding="utf-8")
    print(json.dumps(report, indent=1, ensure_ascii=False))
    print(f"\nreport: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
