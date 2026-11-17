"""The bench spend ledger and the budget guard (work order 3.2).

``bench/results/spend.json`` holds the dollars spent so far in this phase, from
the logged rows of each run (never from an estimate). Before a run the guard
adds that run's estimate to the sum and refuses the run if the total would pass
the cap (US$20 for the whole of Phase 3).
"""
from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PHASE_CAP_DOLLARS = 20.0
REPO_ROOT = Path(__file__).resolve().parent.parent


def repo_relative(path):
    """A path as the bench writes it into result files and the ledger: relative to the repo root (forward
    slashes) when it lies inside the repo, else unchanged. ``None`` stays ``None``."""
    if not path:
        return None
    p = Path(path)
    try:
        return p.resolve().relative_to(REPO_ROOT.resolve()).as_posix() if p.is_absolute() else p.as_posix()
    except ValueError:
        return str(path)


def resolve_path(path) -> Path:
    """The reverse: a repo-relative path resolved against the repo root (an absolute path, such as those in
    the round-1 files, is returned as it is)."""
    p = Path(path)
    return p if p.is_absolute() else REPO_ROOT / p


LOGGED_EVENTS = ("model_call", "orchestrator_decision")  # the rows that carry dollars (as run_bench.read_run_tree)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def logged_dollars(db_path) -> float:
    """Every dollar logged in a run's database (0.0 if it does not exist or has no log table)."""
    if not db_path or not resolve_path(db_path).exists():
        return 0.0
    try:
        with sqlite3.connect(str(resolve_path(db_path))) as conn:
            marks = ",".join("?" for _ in LOGGED_EVENTS)
            row = conn.execute(f"SELECT COALESCE(SUM(dollars),0) FROM logs WHERE event_type IN ({marks})",
                               LOGGED_EVENTS).fetchone()
    except sqlite3.Error:
        return 0.0
    return float(row[0] or 0.0)


def append_call(calls_log, dollars: float, **info) -> None:
    """One line per paid call outside BORG_ROUTE's database (direct arm, judge)."""
    path = resolve_path(calls_log)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps({"dollars": float(dollars), **info}) + "\n")


def calls_log_dollars(calls_log) -> float:
    if not calls_log or not resolve_path(calls_log).exists():
        return 0.0
    total = 0.0
    for line in resolve_path(calls_log).read_text(encoding="utf-8").splitlines():
        try:
            total += float(json.loads(line).get("dollars") or 0.0)
        except (ValueError, AttributeError):
            continue
    return total


class BudgetRefused(Exception):
    """The run would take the phase past its cap; nothing was started."""


class Ledger:
    """Append-only: every run adds an entry (a rerun of the same key adds another, never replaces).

    An entry is written when a run starts (``status: "started"``, with the run's
    database or calls log) and completed when it ends (``"ended"``, with its
    logged dollars). An entry still "started" when the runner starts again (a
    crash) is recovered by summing the dollars logged in its database and calls
    log (``"recovered"``). ``prior_dollars`` holds spend logged elsewhere in the
    phase (3.1's scoring).
    """

    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"prior_dollars": 0.0, "entries": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("prior_dollars", 0.0)
        entries = data.setdefault("entries", [])
        for r in data.pop("runs", []) or []:  # the first format: one row per key, already ended
            entries.append({"id": uuid.uuid4().hex[:12], "key": r.get("key"), "kind": r.get("kind", "run"),
                            "status": "ended", "dollars": float(r.get("dollars", 0.0))})
        return data

    def spent(self) -> float:
        d = self._read()
        return float(d["prior_dollars"]) + sum(float(e.get("dollars", 0.0)) for e in d["entries"])

    def set_prior(self, dollars: float) -> None:
        """Dollars already spent earlier in the phase (for example 3.1's scoring), logged elsewhere."""
        d = self._read()
        d["prior_dollars"] = float(dollars)
        self._write(d)

    def start(self, key: str, kind: str = "run", db_path: Optional[str] = None,
              calls_log: Optional[str] = None, date: Optional[str] = None) -> str:
        """Append a "started" entry before the run spends anything; returns its id."""
        d = self._read()
        eid = uuid.uuid4().hex[:12]
        d["entries"].append({"id": eid, "key": key, "kind": kind, "date": date,
                             "db_path": repo_relative(db_path),
                             "calls_log": repo_relative(calls_log),
                             "status": "started", "started_at": _now(), "dollars": 0.0})
        self._write(d)
        return eid

    def finish(self, entry_id: str, dollars: float, status: str = "ended", note: Optional[str] = None) -> float:
        """Complete a started entry with the run's logged dollars; returns the new total."""
        d = self._read()
        for e in d["entries"]:
            if e.get("id") == entry_id:
                e.update({"status": status, "dollars": round(float(dollars), 8), "ended_at": _now()})
                if note:
                    e["note"] = note
                break
        else:
            raise KeyError(f"no ledger entry {entry_id}")
        self._write(d)
        return d["total_dollars"]

    def record(self, key: str, dollars: float, kind: str = "run") -> float:
        """Add one finished entry (start and finish at once); returns the new total."""
        return self.finish(self.start(key, kind), dollars)

    def recover(self) -> list[dict]:
        """Close every entry still "started" with the dollars its database and calls log hold."""
        d = self._read()
        out = []
        for e in d["entries"]:
            if e.get("status") != "started":
                continue
            dollars = logged_dollars(e.get("db_path")) + calls_log_dollars(e.get("calls_log"))
            e.update({"status": "recovered", "dollars": round(dollars, 8), "ended_at": _now(),
                      "note": "run did not finish; dollars summed from its database and calls log"})
            out.append(dict(e))
        if out:
            self._write(d)
        return out

    def _write(self, d: dict) -> None:
        d["total_dollars"] = round(float(d["prior_dollars"]) + sum(float(e.get("dollars", 0.0)) for e in d["entries"]), 8)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                json.dump(d, fh, indent=1)
                fh.write("\n")
            os.replace(tmp, self.path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise

    def check(self, estimate: float, cap: float = PHASE_CAP_DOLLARS) -> None:
        """Raise BudgetRefused if the logged sum plus ``estimate`` would pass ``cap``."""
        spent = self.spent()
        if spent + estimate > cap:
            raise BudgetRefused(
                f"budget guard: spent US${spent:.4f} + this run's estimate US${estimate:.4f} "
                f"would pass the US${cap:.2f} cap; run refused, nothing was spent"
            )
