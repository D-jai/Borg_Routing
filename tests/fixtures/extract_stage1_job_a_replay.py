"""Write ``stage1_job_a_replay.json`` from the recorded stage-1 job A run.

Source: ``bench/results/2026_11_17/dbs/A_borg_cloud_1.db`` (git-ignored; the
bench keeps each BORG run's database). The extract holds only what a replay
needs: the orchestrator's parsed replies in order, and, per apply_to_each
step, each document id's stored output fields or its failure code. No email
text: the documents themselves are read from the committed
``bench/data/job_a`` when the replay runs.

Run once from the repository root:
    python tests/fixtures/extract_stage1_job_a_replay.py
Test input only (planner ruling 21a/21b): never used to choose wording or defaults.
"""
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "bench/results/2026_11_17/dbs/A_borg_cloud_1.db"
OUT = Path(__file__).resolve().parent / "stage1_job_a_replay.json"


def main() -> int:
    if not DB.exists():
        print(f"missing {DB}", file=sys.stderr)
        return 1
    con = sqlite3.connect(DB)
    instruction = con.execute("select user_instruction from runs").fetchone()[0]
    replies = {"plan": [], "re_plan": [], "compose": []}
    for (dj,) in con.execute(
        "select decision_json from logs where event_type='orchestrator_decision' "
        "order by created_at, log_id"
    ):
        decision = json.loads(dj)
        if decision.get("mode") in replies:
            replies[decision["mode"]].append(decision["parsed"])

    # The two apply_to_each aggregates, in the order they ran; each plan
    # node that is an apply_to_each names them in the same order.
    step_ids = [
        n["id"] for reply in [replies["plan"][0]["plan"]["nodes"]] + [
            r.get("next_nodes") or [] for r in replies["re_plan"]
        ] for n in reply if n.get("type") == "apply_to_each"
    ]
    items: dict = {}
    failures: dict = {}
    aggregates = con.execute(
        "select inbound_payload from envelopes where type='apply_to_each' order by created_at"
    ).fetchall()
    for step_id, (payload,) in zip(step_ids, aggregates):
        out = json.loads(payload)["output"]
        items[step_id] = {e["item"]: e["output"] for e in out["results"]}
        failures[step_id] = {
            e["item"]: (e["reason"]["code"] if isinstance(e["reason"], dict) else str(e["reason"])[:11])
            for e in out["failures"]
        }
    OUT.write_text(json.dumps({
        "source": "bench/results/2026_11_17/dbs/A_borg_cloud_1.db (run 0ef867a2)",
        "lane_mode": "by_reference",
        "folder": "job_a",
        "instruction": instruction,
        "orchestrator_replies": replies,
        "items": items,
        "failures": failures,
    }, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
