"""Offline test of the live Check 1 script's judging and preflight (work order 1.10).

No network: a fake database is built in a temp file and the script's
``evaluate`` function reads it.
"""
import asyncio
import importlib.util
import json
import sqlite3
import uuid
from pathlib import Path

from src.db.schema import create_all
from src.registry.models import ModelInfo, Pricing

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "smoke_live.py"
_spec = importlib.util.spec_from_file_location("smoke_live", _SCRIPT)
smoke = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(smoke)


def _db(tmp_path, *, models=("openrouter/a", "openrouter/b"), dollars=0.01,
        reason="routed", candidates=1, child=False):
    """A root run with two routed steps (plus planner/compose rows); optionally
    one step is served by a child run holding the rest of the spend."""
    db = str(tmp_path / "fake.db")
    asyncio.run(create_all(db))
    root = str(uuid.uuid4())
    kid = str(uuid.uuid4())
    with sqlite3.connect(db) as c:
        c.execute("INSERT INTO runs(run_id, status, user_instruction) VALUES (?, 'completed', 'x')", (root,))
        if child:
            c.execute(
                "INSERT INTO runs(run_id, parent_run_id, status, user_instruction) "
                "VALUES (?, ?, 'completed', 'x')", (kid, root))

        def log(run, env, event, model, dollars_, decision=None, base=0.0):
            c.execute(
                "INSERT INTO logs(run_id, envelope_id, event_type, model_id, tokens_input, "
                "tokens_output, dollars, baseline_dollars, decision_json) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run, env, event, model, 10, 10, dollars_, base,
                 json.dumps(decision) if decision is not None else None))

        log(root, None, "orchestrator_decision", "openrouter/top", dollars / 4, {"mode": "plan"}, 0.02)
        for i, model in enumerate(models):
            env = str(uuid.uuid4())
            run = kid if (child and i == 1) else root
            log(root if run == root else run, env, "orchestrator_decision", model, 0.0, {
                "mode": "route", "node_id": f"s{i}", "task_type": "coding", "tier": "mid",
                "chosen": model, "reason": reason,
                "candidates": [{"model_id": model}] * candidates,
            })
            log(run, env, "model_call", model, dollars / 4, base=0.02)
        log(root, None, "model_call", "tool:calc", 0.0)  # a tool call is not a model
        c.commit()
    return db, root


def test_pass_line_and_report(tmp_path):
    db, root = _db(tmp_path)
    passed, lines, summary = smoke.evaluate(db, root, 12.34)
    assert passed, lines
    assert summary.startswith("CHECK1 PASS models=2 steps=2 spend=$0.007500 ")
    assert "baseline_est=$0.060000 elapsed=12.3s" in summary
    assert summary.endswith(f"db={db}")
    assert any("step=s0 task_type=coding tier=mid chosen=openrouter/a reason=routed" in l for l in lines)


def test_child_run_rows_count_toward_the_tree(tmp_path):
    db, root = _db(tmp_path, child=True)
    passed, _lines, summary = smoke.evaluate(db, root, 1.0)
    assert passed
    assert "models=2 steps=2 spend=$0.007500" in summary


def test_fail_when_only_one_model_served(tmp_path):
    db, root = _db(tmp_path, models=("openrouter/a", "openrouter/a"))
    passed, lines, summary = smoke.evaluate(db, root, 1.0)
    assert not passed
    assert summary.startswith("CHECK1 FAIL models=1 ")
    assert any("(1)" in l for l in lines)


def test_fail_when_spend_is_zero_or_too_high(tmp_path):
    db, root = _db(tmp_path, dollars=0.0)
    passed, lines, _ = smoke.evaluate(db, root, 1.0)
    assert not passed and any("(2)" in l for l in lines)
    (tmp_path / "x").mkdir()
    db, root = _db(tmp_path / "x", dollars=2.0)
    passed, lines, _ = smoke.evaluate(db, root, 1.0)
    assert not passed and any("(2)" in l and "< 0.50" in l for l in lines)


def test_fail_when_routed_step_has_no_candidates(tmp_path):
    db, root = _db(tmp_path, candidates=0)
    passed, lines, _ = smoke.evaluate(db, root, 1.0)
    assert not passed and any("(3)" in l and "no candidates" in l for l in lines)


def test_fallback_reason_needs_no_candidates(tmp_path):
    db, root = _db(tmp_path, reason="fallback_default", candidates=0)
    passed, _lines, _ = smoke.evaluate(db, root, 1.0)
    assert passed


def test_timeout_style_failure_is_reported_in_the_line(tmp_path):
    db, root = _db(tmp_path)
    passed, lines, summary = smoke.evaluate(
        db, root, 600.0, "failed", ["timeout: run exceeded the 600s wall-clock limit"])
    assert not passed
    assert summary.startswith("CHECK1 FAIL ")
    assert any("timeout" in l for l in lines)


def _model(mid, price, checked=True, source="openrouter"):
    return ModelInfo(
        model_id=mid, source=source, context_length=8000, checked=checked,
        pricing=Pricing(input_per_token=price, output_per_token=price * 5) if price is not None else None,
    )


def test_preflight_accepts_three_distinct_priced_models():
    ok, _ = smoke.preflight([_model("a", 1e-6), _model("b", 2e-6), _model("c", 3e-6)])
    assert ok


def test_preflight_refuses_fewer_than_three_priced_or_distinct():
    ok, msg = smoke.preflight([_model("a", 1e-6), _model("b", 2e-6)])
    assert not ok and "at least 3" in msg
    # Four models but only two distinct prices, an unchecked one and an unpriced one.
    ok, _ = smoke.preflight([
        _model("a", 1e-6), _model("b", 1e-6), _model("c", 2e-6),
        _model("d", 3e-6, checked=False), _model("e", None),
        _model("f", 4e-6, source="ollama"),
    ])
    assert not ok
