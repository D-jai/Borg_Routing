"""Planner ruling 23: prove that with packing off every request body is
byte-identical to the tree before packing (design note section 8).

One fixed stand-in job runs in a source tree, in a fresh process per run:

- an apply_to_each of 45 items, one of them oversized (over 400 tokens),
  with the lane off;
- a by_reference run over two documents (apply_to_each over the documents,
  then a summary step).

Controls: every uuid4 binding is patched to one seeded sequence (``uuid.uuid4``
and each module's ``from uuid import uuid4`` name), the environment is pinned
to one dict, and both trees import ONE stand-in source (this repo's
``tests/e2e/stub_model_server.py``, loaded by path). Every request body is
written as ``json.dumps(body)`` (httpx's own serialisation) and the two runs
are compared as a sorted multiset, because concurrency reorders posts.

Usage:
    python scripts/compare_request_bodies.py --base <tree> --build <tree>
        runs base vs base first (must be empty), then base vs build with
        BORG_PACK_MAX_ITEMS=0 and =1, and prints the result.
    python scripts/compare_request_bodies.py --run <tree> --pack <value> --out <file>
        (internal) one run in one tree.

Test-only tooling: nothing here spends money or leaves the machine.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
STUB_SOURCE = HERE / "tests" / "e2e" / "stub_model_server.py"

PINNED_ENV = {
    "OPENROUTER_API_KEY": "sk-or-test-compare",
    "ORCHESTRATOR_QUALITY_REQUIREMENT": "9.0",
    "TIME_COST_PER_SEC": "0.0001",
    "BORG_RULE_ZERO_SHORTCUT": "false",
    "PYTHONHASHSEED": "0",
}
CLEARED_ENV = (
    "BORG_LANE_MODE", "BORG_LANE_MANIFEST", "BORG_LOCAL_HOSTS", "BORG_LANE_LOCAL_ALLOWLIST",
    "BORG_LANE_MAX_OUTPUT_CHARS", "BORG_LANE_MAX_VERBATIM_CHARS", "BORG_LANE_MAX_NGRAM_SHARE",
    "BORG_LANE_PART_CHARS", "LEAF_QUALITY_LIGHT", "LEAF_QUALITY_MID", "LEAF_QUALITY_HEAVY",
    "BASELINE_MODEL_ID", "ANSWERER_MAX_TOKENS", "ORCHESTRATOR_MAX_TOKENS", "BORG_DOCS_ROOT",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "BORG_ADMIN_TOKEN", "BORG_PACK_MAX_ITEMS",
)

OPERATOR_A = "Sort each of these 45 short notes into kind a, b or c, and count each kind."
TASK_A = "Give the kind of this note: a, b or c."
OPERATOR_B = "Read the attached client notes and tell me which clients are high risk."
EXTRACT_B = "From this client note, give the client name and the risk level."
DOC_ACME = ("Client: Acme Holdings. Account ACCT-CMP-1. The relationship desk flagged late payments "
            "three quarters running and a disputed guarantee.")
DOC_BIRCH = ("Client: Birch Logistics. Warehouse contract renewed for two years; payments on time; "
             "no open disputes.")


def _patch_uuid4() -> None:
    import random
    import uuid

    rng = random.Random(23)
    real = uuid.uuid4

    def seeded():
        return uuid.UUID(int=rng.getrandbits(128), version=4)

    uuid.uuid4 = seeded
    for name, mod in list(sys.modules.items()):
        if mod is not None and getattr(mod, "uuid4", None) is real:
            setattr(mod, "uuid4", seeded)


def _load_stub():
    spec = importlib.util.spec_from_file_location("compare_stub", STUB_SOURCE)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["compare_stub"] = mod
    spec.loader.exec_module(mod)
    return mod


class _MP:
    """The two monkeypatch calls the stand-in uses."""
    def setattr(self, target, name, value):
        setattr(target, name, value)


async def _job(stub_mod, db_dir: Path, docs: Path) -> list[str]:
    from src.db.schema import create_all
    from src.orchestrator import engine
    from src.pipeline.runner import run_pipeline

    bodies: list[str] = []

    # Job A: 45 items, one oversized, lane off.
    os.environ.pop("BORG_LANE_MODE", None)
    db_a = str(db_dir / "a.db")
    await create_all(db_a)
    engine._shared_matrix = stub_mod.three_tier_matrix()
    items = [f"note {i:02d}" for i in range(44)] + ["long " + "z" * 1700]
    plan_a = [{"type": "apply_to_each", "id": "n1", "instruction": TASK_A, "items": items,
               "task_type": "extraction", "tier": "light", "output_schema": {"kind": "a|b|c"}}]
    stub = stub_mod.StubModels(plan_nodes=plan_a, answer_for=lambda r: {"kind": "a"}).install(_MP())
    result = await run_pipeline(OPERATOR_A, db_path=db_a)
    await stub_mod.drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    bodies += [json.dumps(r.body) for r in stub.requests]

    # Job B: by_reference over two documents.
    os.environ["BORG_LANE_MODE"] = "by_reference"
    os.environ["BORG_DOCS_ROOT"] = str(docs)
    db_b = str(db_dir / "b.db")
    await create_all(db_b)
    engine._shared_matrix = stub_mod.three_tier_matrix()
    plan_b = [
        {"type": "apply_to_each", "id": "n1", "inputs_ref": "documents", "instruction": EXTRACT_B,
         "task_type": "extraction", "tier": "light", "output_schema": {"client": "string", "risk": "string"}},
        {"type": "answerer", "id": "n2", "depends_on": ["n1"], "instruction": "List the high-risk clients.",
         "task_type": "reasoning", "tier": "mid", "output_schema": {"result": "string"}},
    ]
    stub = stub_mod.StubModels(plan_nodes=plan_b).install(_MP())
    result = await run_pipeline(OPERATOR_B, db_path=db_b, documents={"folder": "."})
    await stub_mod.drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    bodies += [json.dumps(r.body) for r in stub.requests]
    return sorted(bodies)


def _run_one(tree: Path, pack: str, out: Path) -> None:
    sys.path.insert(0, str(tree))
    os.chdir(tree)
    for name in CLEARED_ENV:
        os.environ.pop(name, None)
    os.environ.update(PINNED_ENV)
    if pack != "unset":
        os.environ["BORG_PACK_MAX_ITEMS"] = pack
    import src.orchestrator.engine  # noqa: F401  (import the tree's modules before patching)
    import src.pipeline.runner  # noqa: F401
    import src.lane.outbound  # noqa: F401
    import src.db.state  # noqa: F401
    import src.skills.library  # noqa: F401
    import src.memory.window  # noqa: F401
    stub_mod = _load_stub()
    _patch_uuid4()
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        docs = tmp_path / "docs"
        docs.mkdir()
        (docs / "acme_note.txt").write_text(DOC_ACME, encoding="utf-8")
        (docs / "birch_note.md").write_text(DOC_BIRCH, encoding="utf-8")
        bodies = asyncio.run(_job(stub_mod, tmp_path, docs))
    out.write_text(json.dumps(bodies, ensure_ascii=False, indent=0), encoding="utf-8")


def _spawn(tree: Path, pack: str, out: Path) -> list[str]:
    env = {k: v for k, v in os.environ.items() if k not in CLEARED_ENV}
    env.update(PINNED_ENV)
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--run", str(tree), "--pack", pack, "--out", str(out)],
        env=env, capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        raise SystemExit(f"run in {tree} (pack={pack}) failed:\n{proc.stdout[-3000:]}\n{proc.stderr[-3000:]}")
    return json.loads(out.read_text(encoding="utf-8"))


def _diff(a: list[str], b: list[str]) -> tuple[int, int]:
    """(bodies only in a, bodies only in b), as multisets."""
    from collections import Counter

    ca, cb = Counter(a), Counter(b)
    return sum((ca - cb).values()), sum((cb - ca).values())


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--base")
    ap.add_argument("--build")
    ap.add_argument("--run")
    ap.add_argument("--pack", default="unset")
    ap.add_argument("--out")
    args = ap.parse_args()
    if args.run:
        _run_one(Path(args.run).resolve(), args.pack, Path(args.out))
        return 0
    base, build = Path(args.base).resolve(), Path(args.build).resolve()
    with tempfile.TemporaryDirectory() as tmp:
        t = Path(tmp)
        b1 = _spawn(base, "unset", t / "base1.json")
        b2 = _spawn(base, "unset", t / "base2.json")
        rows = [("base vs base", b1, b2)]
        for value in ("0", "1"):
            rows.append((f"base vs build, BORG_PACK_MAX_ITEMS={value}", b1, _spawn(build, value, t / f"build{value}.json")))
        # Sensitivity: with packing on the bodies must differ, or the comparison proves nothing.
        on = _spawn(build, "20", t / "build20.json")
        ok = True
        for label, a, b in rows:
            only_a, only_b = _diff(a, b)
            same = only_a == 0 and only_b == 0 and len(a) == len(b)
            ok &= same
            print(f"{label}: {len(a)} vs {len(b)} bodies; only in first {only_a}, only in second {only_b}"
                  f" -> {'IDENTICAL' if same else 'DIFFERENT'}")
        only_a, only_b = _diff(b1, on)
        print(f"sensitivity, base vs build, BORG_PACK_MAX_ITEMS=20: {len(b1)} vs {len(on)} bodies; "
              f"only in first {only_a}, only in second {only_b} -> "
              f"{'DIFFERENT (as it must be)' if only_a or only_b else 'IDENTICAL (comparison is blind!)'}")
        ok &= bool(only_a or only_b)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
