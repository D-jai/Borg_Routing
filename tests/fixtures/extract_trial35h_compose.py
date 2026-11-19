"""Write ``trial35h_compose_a.json`` from the trial-35h round-1-data run DB of
job A (planner ruling 36a; the motivating reply of check C5, BRT-ORC-039).

Source: ``bench/results/trial35h_r1data/dbs/A_borg_cloud_1.db`` (git-ignored),
opened read-only, under ``BORG_DATA_ROOT`` when set (a worktree has none),
else the repository root. The fixture has the shape of
``round2_compose_{a,b,c,d}.json`` (``extract_round2_compose.py``):

- ``source``, ``instruction``;
- ``lane``: mode, manifest ``names``, and the documents (id, name, part ids)
  read from the manifest block of the first compose request;
- ``steps``: node type, data scope, status and output of each step, from the
  stage results of the first compose request;
- ``recorded_replies``: the parsed compose replies in order (one: ten typed
  rows, then ``// ... (full list of all 200 entries follows ...)``);
- ``recorded_quality``: the grader's figures from the trial's result file;
- ``asked_but_not_produced``: as recorded, read from the block of the first
  compose request (this run has ruling 32c).

The script asserts that no D canary appears in the fixture and that it holds
no document text (only ids, names and model-written fields).

Run once from the repository root:
    python tests/fixtures/extract_trial35h_compose.py
Test input only: never used to choose wording or defaults.
"""
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_round2_compose import (  # noqa: E402
    ROOT, _check, _decisions, _documents, _plan_nodes, _replies, _scope, _stage_results,
)

DATA = Path(os.environ.get("BORG_DATA_ROOT") or ROOT)
TRIAL = DATA / "bench/results/trial35h_r1data"
OUT = Path(__file__).resolve().parent
ASKED_HEAD = "Asked for but not produced"
ASKED_LINE = re.compile(r"^- (\S+) \(skill (\S+)\): ([^.]+)")


def asked_from_block(user: str) -> list:
    """The asked-but-not-produced list a compose request's block names, one
    ``{"step", "skill", "field"}`` entry per field (empty when there is none)."""
    if ASKED_HEAD not in user:
        return []
    out = []
    for line in user[user.index(ASKED_HEAD):].splitlines()[1:]:
        m = ASKED_LINE.match(line)
        if not m:
            break
        out.extend({"step": m.group(1), "skill": m.group(2), "field": f.strip()}
                   for f in m.group(3).split(",") if f.strip())
    return out


def extract() -> dict:
    db = TRIAL / "dbs" / "A_borg_cloud_1.db"
    if not db.exists():
        raise SystemExit(f"missing {db}")
    con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)
    decisions = _decisions(con)
    nodes = _plan_nodes(decisions)
    run = con.execute("select user_instruction, lane_mode from runs").fetchone()
    body = json.loads(con.execute(
        "select body from outbound_requests where purpose='orchestrator:compose' "
        "order by started_at, rowid limit 1").fetchone()[0])
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    steps = {}
    for sid, out in _stage_results(system).items():
        node = nodes.get(sid) or {}
        steps[sid] = {"node_type": node.get("type", "answerer"), "data_scope": _scope(node),
                      "status": "resolved", "output": out}
    rec = json.loads((TRIAL / "A_borg_cloud_1.json").read_text(encoding="utf-8"))
    return {
        "source": "bench/results/trial35h_r1data/dbs/A_borg_cloud_1.db",
        "instruction": run[0],
        "lane": {"mode": run[1], "manifest": "names", "documents": _documents(system)},
        "steps": steps,
        "recorded_replies": _replies(decisions),
        "recorded_quality": rec.get("quality") or {},
        "asked_but_not_produced": asked_from_block(user),
    }


def main() -> int:
    canaries = json.loads((ROOT / "bench/data/answers/job_d_answers.json")
                          .read_text(encoding="utf-8"))["canaries"]
    data = extract()
    text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
    _check("A", text, canaries)
    path = OUT / "trial35h_compose_a.json"
    path.write_text(text, encoding="utf-8")
    print(f"wrote {path} ({len(text):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
