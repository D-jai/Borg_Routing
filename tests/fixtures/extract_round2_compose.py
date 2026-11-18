"""Write ``round2_compose_{a,b,c,d}.json`` from the bench round-2 run DBs
(planner ruling 32e; docs/design/compose_placement.md section 2.7).

Source: ``bench/results/2026_11_18/dbs/<job>_borg_cloud_1.db`` (git-ignored),
opened read-only. Each fixture holds what a compose replay needs:

- ``source``, ``instruction``;
- ``lane``: mode, manifest ``names``, and the documents (id, name, part ids)
  read from the manifest block of the first compose request;
- ``steps``: node type, data scope, status and output of each step. A, C and
  D take them from the stage results of the first compose request; B (lane
  off, no request ledger) from ``envelopes.inbound_payload``;
- ``recorded_replies``: the parsed compose replies in order. C's first reply
  was not logged and is reconstructed as ``{}``;
- ``recorded_quality``: the grader's figures from the round's result file;
- ``asked_but_not_produced``: constructed. For A, the list ruling 32c would
  hold after a repeated fault (the recorded runs predate 32c); empty for the
  others.

The script asserts that no D canary appears in any fixture and that the A and
D fixtures hold no document text (only ids, names and model-written fields).

Run once from the repository root:
    python tests/fixtures/extract_round2_compose.py
Test input only: never used to choose wording or defaults.
"""
import json
import re
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DAY = ROOT / "bench/results/2026_11_18"
OUT = Path(__file__).resolve().parent
STAGE_HEAD = "Stage results so far (if any; relevant for re_plan / compose):\n"
DOC_LINE = re.compile(r"^- (doc_\d+): (\S+) \((\w+), (\d+) bytes(?:, (\d+) parts)?\)$")

ASKED_A = [{"step": "classify_emails", "skill": "classify_into_buckets", "field": f}
           for f in ("file", "category", "urgency")]


def _open(job: str) -> sqlite3.Connection:
    db = DAY / "dbs" / f"{job}_borg_cloud_1.db"
    if not db.exists():
        raise SystemExit(f"missing {db}")
    return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True)


def _decisions(con) -> list:
    out = []
    for (dj,) in con.execute(
        "select decision_json from logs where event_type='orchestrator_decision' order by log_id"
    ):
        try:
            out.append(json.loads(dj or "null") or {})
        except ValueError:
            continue
    return out


def _plan_nodes(decisions: list) -> dict:
    nodes: dict = {}
    for d in decisions:
        parsed = d.get("parsed") or {}
        if d.get("mode") == "plan":
            batch = (parsed.get("plan") or {}).get("nodes") or []
        elif d.get("mode") == "re_plan":
            batch = parsed.get("next_nodes") or []
        else:
            continue
        for n in batch:
            if isinstance(n, dict) and n.get("id"):
                nodes.setdefault(n["id"], n)
    return nodes


def _scope(node: dict) -> object:
    reads = node.get("inputs_ref") == "documents" or bool(node.get("documents")) or (
        (node.get("skill_args") or {}).get("items_ref") == "documents")
    return "raw" if reads else None


def _first_compose(con) -> tuple:
    row = con.execute(
        "select body from outbound_requests where purpose='orchestrator:compose' "
        "order by started_at, rowid limit 1").fetchone()
    body = json.loads(row[0])
    return body["messages"][0]["content"], body["messages"][1]["content"]


def _documents(system: str) -> list:
    docs = []
    for line in system.splitlines():
        m = DOC_LINE.match(line.strip())
        if not m:
            continue
        parts = int(m.group(5) or 1)
        ids = [m.group(1)] if parts == 1 else [f"{m.group(1)}#{k}" for k in range(1, parts + 1)]
        docs.append({"id": m.group(1), "name": m.group(2), "parts": ids})
    return docs


def _stage_results(system: str) -> dict:
    a = system.index(STAGE_HEAD) + len(STAGE_HEAD)
    b = system.index("\n\nRaw tool_results", a)
    return json.loads(system[a:b])


def _replies(decisions: list) -> list:
    return [d.get("parsed") for d in decisions if d.get("mode") == "compose"]


def _quality(job: str) -> dict:
    rec = json.loads((DAY / f"{job}_borg_cloud_1.json").read_text(encoding="utf-8"))
    return rec.get("quality") or {}


def extract(job: str) -> dict:
    con = _open(job)
    decisions = _decisions(con)
    nodes = _plan_nodes(decisions)
    run = con.execute("select user_instruction, lane_mode from runs").fetchone()
    steps: dict = {}
    if job == "B":
        docs = []
        for ob, ib in con.execute(
            "select outbound_payload, inbound_payload from envelopes order by created_at"
        ):
            node_id = json.loads(ob).get("node_id")
            env = json.loads(ib or "{}")
            node = nodes.get(node_id) or {}
            steps[node_id] = {"node_type": node.get("type", "answerer"), "data_scope": None,
                              "status": env.get("status"), "output": env.get("output")}
    else:
        system, _ = _first_compose(con)
        docs = _documents(system)
        for sid, out in _stage_results(system).items():
            node = nodes.get(sid) or {}
            steps[sid] = {"node_type": node.get("type", "answerer"), "data_scope": _scope(node),
                          "status": "resolved", "output": out}
    replies = _replies(decisions)
    if job == "C":
        replies = [{}] + replies            # the first reply was not logged
    return {
        "source": f"bench/results/2026_11_18/dbs/{job}_borg_cloud_1.db",
        "instruction": run[0],
        "lane": {"mode": run[1], "manifest": "names", "documents": docs},
        "steps": steps,
        "recorded_replies": replies,
        "recorded_quality": _quality(job),
        "asked_but_not_produced": ASKED_A if job == "A" else [],
    }


def _check(job: str, text: str, canaries: list) -> None:
    assert not any(c in text for c in canaries), f"a D canary in fixture {job}"
    if job in ("A", "D"):
        folder = ROOT / "bench/data" / f"job_{job.lower()}"
        for path in folder.iterdir():
            body = path.read_text(encoding="utf-8").strip()
            probe = body[:60]
            assert len(probe) < 24 or probe not in text, f"document text of {path.name} in {job}"


def main() -> int:
    canaries = json.loads((ROOT / "bench/data/answers/job_d_answers.json")
                          .read_text(encoding="utf-8"))["canaries"]
    for job in "ABCD":
        data = extract(job)
        text = json.dumps(data, indent=1, ensure_ascii=False) + "\n"
        _check(job, text, canaries)
        path = OUT / f"round2_compose_{job.lower()}.json"
        path.write_text(text, encoding="utf-8")
        print(f"wrote {path} ({len(text):,} chars)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
