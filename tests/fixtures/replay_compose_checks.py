"""Replay the 32e checks over every stored compose reply (planner ruling 33a/(4)).

Reads, read-only, every git-ignored run DB that holds a compose reply:
``bench/results/2026_11_17/dbs/*.db`` (round 1), ``bench/results/2026_11_18/dbs/*.db``
(round 2) and ``demo/runs/db/demo.db`` (the demo runs). For each compose reply
it rebuilds the compose context the way ``extract_round2_compose.py`` does:

- with a compose request in ``outbound_requests`` (lane on): the stage results
  and the document manifest of the run's first compose request; the lane mode
  from ``runs.lane_mode``, or from the manifest wording when the column is
  missing (round 1);
- otherwise (lane off, no request ledger): each plan step's stored envelope.

Node types and data scopes come from the plan and re_plan replies. The
asked-but-not-produced list is empty (every stored run predates ruling 32c),
except a second pass over round-2 job A with the list 32c would hold.

It then runs the marker checks (BRT-ORC-032, 037), C1 (038), C3 (040) and C4
(041) as ``_handle_compose_result`` would (``_RowContext.problems``) and
prints one row per reply. No DB content is written anywhere.

Run from the repository root:
    python tests/fixtures/replay_compose_checks.py
"""
import glob
import json
import os
import re
import sqlite3
import sys
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from extract_round2_compose import (  # noqa: E402
    ASKED_A, STAGE_HEAD, _documents, _plan_nodes, _scope,
)
from src.envelopes.schema import InboundEnvelope  # noqa: E402
from src.lane.context import LaneContext, lane_scope  # noqa: E402
from src.lane.documents import Document, DocPart, LoadedDocuments  # noqa: E402
from src.lane.settings import LaneSettings  # noqa: E402
from src.orchestrator import engine  # noqa: E402

DBS = (sorted(glob.glob(str(ROOT / "bench/results/2026_11_17/dbs/*.db")))
       + sorted(glob.glob(str(ROOT / "bench/results/2026_11_18/dbs/*.db")))
       + [str(ROOT / "demo/runs/db/demo.db")])


def _decisions(con, run_id):
    out = []
    for (dj,) in con.execute(
            "select decision_json from logs where run_id=? and event_type='orchestrator_decision' "
            "order by log_id", (run_id,)):
        try:
            out.append(json.loads(dj or "null") or {})
        except ValueError:
            pass
    return out


def _type_of(out):
    if isinstance(out, dict) and isinstance(out.get("results"), list) and (
            "success_count" in out or "failures" in out):
        return "apply_to_each"
    if isinstance(out, dict) and isinstance(out.get("result"), list):
        return "state_op"
    return "answerer"


def _mode_from_wording(system):
    if "# Documents (by reference)" not in system:
        return "off"
    return "by_reference" if "may be cloud models" in system else "raw_local"


def _lane(mode, docs):
    if mode == "off" or not docs:
        return None
    built = [Document(d["id"], d["name"], "txt", 1, "placeholder",
                      [DocPart(p, d["id"], f"placeholder {p}") for p in d["parts"]]) for d in docs]
    return LaneContext(settings=LaneSettings(mode=mode, manifest="names"),
                       docs=LoadedDocuments(built, []), db_path=":memory:")


def _env(status, output, node_type, scope):
    env = InboundEnvelope(envelope_id=uuid4(), subtask_id=uuid4(), status=status,
                          output=output if isinstance(output, dict) else {"value": output},
                          **({"failure_reason": "stored failure"} if status == "failed" else {}))
    env._node_type = node_type
    env._data_scope = scope
    if status == "resolved" and scope == "raw":
        env.lane_guarded = True        # shown to compose, so it passed the output guard
    return env


def context(con, run_id, lane_col):
    """(results, lane mode, documents, how) for a run's compose calls."""
    nodes = _plan_nodes(_decisions(con, run_id))
    row = con.execute("select body from outbound_requests where run_id=? and "
                      "purpose='orchestrator:compose' order by started_at, rowid limit 1",
                      (run_id,)).fetchone() if _has(con, "outbound_requests") else None
    results = {}
    if row:
        body = json.loads(row[0])
        system = body["messages"][0]["content"]
        a = system.index(STAGE_HEAD) + len(STAGE_HEAD)
        b = system.index("\n\nRaw tool_results", a)
        stage = json.loads(system[a:b]) if system[a:b].strip() != "(none)" else {}
        docs = _documents(system)
        mode = lane_col or _mode_from_wording(system)
        for sid, out in stage.items():
            node = nodes.get(sid) or {}
            failed = isinstance(out, dict) and set(out) == {"failure"}
            results[sid] = _env("failed" if failed else "resolved", out,
                                node.get("type") or _type_of(out), _scope(node))
        return results, mode, docs, "first compose request"
    # Round 1 stored no node id on the envelope: top-level envelopes are then
    # matched to the plan's node ids in the order the steps were planned.
    unnamed = iter(list(nodes))
    for ob, ib in con.execute("select outbound_payload, inbound_payload from envelopes "
                              "where run_id=? order by created_at", (run_id,)):
        sid = json.loads(ob).get("node_id")
        if sid is None:
            sid = next(unnamed, None)
        inbound = json.loads(ib or "{}")
        if sid is None or sid not in nodes or not inbound:
            continue
        node = nodes[sid]
        results[sid] = _env(inbound.get("status") or "resolved", inbound.get("output") or {},
                            node.get("type") or _type_of(inbound.get("output")), _scope(node))
    return results, lane_col or "off", [], "stored envelopes"


def _has(con, table):
    return bool(con.execute("select 1 from sqlite_master where name=?", (table,)).fetchone())


def replay(results, mode, docs, replies, asked=()):
    lane = _lane(mode, docs)
    rows_out = []
    with lane_scope(lane):
        engine_asked = engine.asked_but_not_produced
        engine.asked_but_not_produced = lambda run_id: list(asked)
        try:
            ctx = engine._RowContext.for_run(results, lane, "", None, uuid4())
        finally:
            engine.asked_but_not_produced = engine_asked
        for reply in replies:
            text, field, _ = engine._compose_text(reply)
            action = str((reply or {}).get("action") or "").lower().strip() \
                if isinstance(reply, dict) else ""
            if not ctx.active:
                rows_out.append(("no block (gate closed): checks do not run", []))
            elif not text and action != "fail":
                rows_out.append(("no final text (029 path): checks do not run", []))
            else:
                value = reply[field] if text else str(reply.get("rationale") or "")
                rows_out.append(("checked" + (" (declined)" if not text else ""),
                                 ctx.problems(value, declined=not text)))
    return ctx, rows_out


def main():
    for db in DBS:
        if not os.path.exists(db):
            print(f"missing {db}")
            continue
        con = sqlite3.connect(f"file:{Path(db).as_posix()}?mode=ro", uri=True)
        cols = [r[1] for r in con.execute("pragma table_info(runs)")]
        for (run_id,) in con.execute("select run_id from runs order by created_at").fetchall():
            decisions = _decisions(con, run_id)
            replies = [d.get("parsed") for d in decisions if d.get("mode") == "compose"]
            if not replies:
                continue
            lane_col = con.execute("select lane_mode from runs where run_id=?", (run_id,)).fetchone()[0] \
                if "lane_mode" in cols else None
            results, mode, docs, how = context(con, run_id, lane_col)
            passes = [((), "")]
            if "2026_11_18" in db and os.path.basename(db).startswith("A_"):
                passes.append((ASKED_A, " +32c list"))
            for asked, tag in passes:
                ctx, rows = replay(results, mode, docs, replies, asked)
                for n, (status, problems) in enumerate(rows, 1):
                    counts = {}
                    for code, sentence in problems:
                        m = re.search(r"to (\d+) items", sentence)
                        counts[code] = counts.get(code, 0) + (int(m.group(1)) if m else 1)
                    missing = [n for n, node in _plan_nodes(decisions).items()
                               if n not in results and how == "stored envelopes"]
                    print(json.dumps({
                        "db": os.path.relpath(db, ROOT).replace(os.sep, "/"), "run": run_id[:8],
                        "missing_steps": missing,
                        "reply": n, "pass": tag.strip() or "as stored", "context": how,
                        "lane": mode, "list_steps": list(ctx.steps), "long_outputs": list(ctx.outputs),
                        "status": status, "fires": counts,
                        "sentences": [s[:160] for _, s in problems],
                    }))


def stage1_fixture():
    """The ruling-21 replay (tests/fixtures/stage1_job_a_replay.json): its
    recorded compose reply and the scripted marker reply the replay test uses."""
    rec = json.loads((ROOT / "tests/fixtures/stage1_job_a_replay.json").read_text(encoding="utf-8"))
    docs = sorted(set(rec["items"]["extract_urgency"]) | set(rec["failures"]["classify_emails"]))
    results = {}
    for step in ("classify_emails", "extract_urgency"):
        ok, bad = rec["items"][step], rec["failures"].get(step, {})
        out = {"results": [{"index": i, "item": d, "output": ok[d]} for i, d in enumerate(docs) if d in ok],
               "failures": [{"index": i, "item": d, "reason": {"code": bad[d], "message": "m"}}
                            for i, d in enumerate(docs) if d in bad]}
        out.update(total=len(docs), success_count=len(out["results"]),
                   failure_count=len(out["failures"]))
        results[step] = _env("resolved", out, "apply_to_each", "raw")
    lane_docs = [{"id": d, "name": f"job_a/email_{d[4:]}.txt", "parts": [d]} for d in docs]
    replies = [rec["orchestrator_replies"]["compose"][0], {"response": "@@ROWS:extract_urgency@@"}]
    ctx, rows = replay(results, "by_reference", lane_docs, replies)
    for n, (status, problems) in enumerate(rows, 1):
        print(json.dumps({"db": "tests/fixtures/stage1_job_a_replay.json", "run": "0ef867a2",
                          "reply": n, "pass": "recorded" if n == 1 else "scripted (ruling-21 test)",
                          "list_steps": list(ctx.steps), "status": status,
                          "fires": {c: 1 for c, _ in problems}}))


if __name__ == "__main__":
    main()
    stage1_fixture()
