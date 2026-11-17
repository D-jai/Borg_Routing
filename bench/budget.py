"""The bench spend ledger and the budget guard (work order 3.2).

``bench/results/spend.json`` holds the dollars spent so far in this phase, from
the logged rows of each run (never from an estimate). Before a run the guard
adds that run's estimate to the sum and refuses the run if the total would pass
the cap (US$20 for the whole of Phase 3).
"""
from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Optional

PHASE_CAP_DOLLARS = 20.0
ESTIMATE_LIMIT_DOLLARS = 14.0


class BudgetRefused(Exception):
    """The run would take the phase past its cap; nothing was started."""


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def _read(self) -> dict:
        if not self.path.exists():
            return {"prior_dollars": 0.0, "runs": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        data.setdefault("prior_dollars", 0.0)
        data.setdefault("runs", [])
        return data

    def spent(self) -> float:
        d = self._read()
        return float(d["prior_dollars"]) + sum(float(r.get("dollars", 0.0)) for r in d["runs"])

    def set_prior(self, dollars: float) -> None:
        """Dollars already spent earlier in the phase (for example 3.1's scoring), logged elsewhere."""
        d = self._read()
        d["prior_dollars"] = float(dollars)
        self._write(d)

    def record(self, key: str, dollars: float, kind: str = "run") -> float:
        """Add one run's logged dollars; returns the new total. Replaces an earlier entry with the same key."""
        d = self._read()
        d["runs"] = [r for r in d["runs"] if r.get("key") != key]
        d["runs"].append({"key": key, "kind": kind, "dollars": round(float(dollars), 8)})
        d["total_dollars"] = round(float(d["prior_dollars"]) + sum(r["dollars"] for r in d["runs"]), 8)
        self._write(d)
        return d["total_dollars"]

    def _write(self, d: dict) -> None:
        d["total_dollars"] = round(float(d["prior_dollars"]) + sum(float(r.get("dollars", 0.0)) for r in d["runs"]), 8)
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


def load_shrink_rules(path: Path) -> dict:
    import yaml

    return yaml.safe_load(Path(path).read_text(encoding="utf-8"))


def apply_shrink(rules: dict, steps: int) -> dict:
    """Overrides after the first ``steps`` shrink steps: ``{"repeats": int|None, "job_repeats": {job: int}}``."""
    out: dict = {"repeats": None, "job_repeats": {}}
    for step in (rules.get("steps") or [])[:steps]:
        if "repeats" in step:
            out["repeats"] = int(step["repeats"])
        for job, n in (step.get("job_repeats") or {}).items():
            out["job_repeats"][job] = int(n)
    return out
