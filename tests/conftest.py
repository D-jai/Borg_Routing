"""Session-wide test fixtures for BORG_ROUTE.

Several endpoints call `matrix.save()`, which writes the capability matrix
YAML to disk. Combined with e2e fixtures that flip every row to
``checked=True`` in memory before tests, this causes the on-disk seed to
drift between tests. The autouse fixture below snapshots the file before
every test and restores it afterwards so tests stay isolated regardless of
order.

BUG-006 residue cleanup: a handful of tests (test_slice6.py,
test_tasks.py) write directly to the repo-root ``data/borg_route.db``
rather than tmp_path. That leaks placeholder runs ("stream test
envelopes", "Test instruction.") into the operator's local DB. The
``_cleanup_db_residue`` session-scoped fixture below deletes rows with
well-known residue instructions at the end of the test run. Safer than
dropping the whole DB; preserves any real runs the operator created
outside the test suite.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

_MATRIX_PATH = Path("data/capability_matrix.yaml")
_DB_PATH = Path("data/borg_route.db")

# Instructions used only by the test suite — safe to wipe on teardown.
_RESIDUE_INSTRUCTIONS = (
    "stream test completed",
    "stream test failed",
    "stream test envelopes",
    "Test instruction.",
    "Classify these issues.",
    "Classify these 10 GitHub issues into buckets.",
)


@pytest.fixture(autouse=True)
def _preserve_matrix_yaml():
    """Snapshot data/capability_matrix.yaml around every test."""
    original = _MATRIX_PATH.read_text(encoding="utf-8") if _MATRIX_PATH.exists() else None
    try:
        yield
    finally:
        if original is not None:
            _MATRIX_PATH.write_text(original, encoding="utf-8")


@pytest.fixture(autouse=True)
def _restore_shared_matrix():
    """The engine routes on ``main.matrix`` (R2), so a test that ticks rows or
    inserts a test row there must not leak into later tests: snapshot the
    rows before each test and put them back afterwards."""
    import sys
    import copy

    main_mod = sys.modules.get("main")
    shared = getattr(main_mod, "matrix", None)
    snapshot = copy.deepcopy(shared._models) if shared is not None else None
    try:
        yield
    finally:
        # ``main`` may have been imported during the test itself.
        shared = getattr(sys.modules.get("main"), "matrix", None)
        if snapshot is not None and shared is not None:
            shared._models = snapshot


@pytest.fixture(scope="session", autouse=True)
def _cleanup_db_residue():
    """BUG-006 — wipe well-known test-only runs from the repo DB on teardown.

    Runs once at the end of the whole pytest session. Silent no-op if the
    DB file is missing or the runs table hasn't been created yet (new
    clones that haven't booted the app).
    """
    yield
    if not _DB_PATH.exists():
        return
    try:
        with sqlite3.connect(str(_DB_PATH)) as conn:
            # Collect run_ids to cascade-delete related rows.
            placeholders = ",".join(["?"] * len(_RESIDUE_INSTRUCTIONS))
            cur = conn.execute(
                f"SELECT run_id FROM runs WHERE user_instruction IN ({placeholders})",
                _RESIDUE_INSTRUCTIONS,
            )
            run_ids = [row[0] for row in cur.fetchall()]
            if not run_ids:
                return
            rid_ph = ",".join(["?"] * len(run_ids))
            for table in ("log_payloads",):
                conn.execute(
                    f"DELETE FROM {table} WHERE log_id IN "
                    f"(SELECT log_id FROM logs WHERE run_id IN ({rid_ph}))",
                    run_ids,
                )
            for table in (
                "log_entities", "log_relations", "logs", "scratch",
                "envelopes", "subtasks", "run_digests", "messages", "runs",
            ):
                try:
                    conn.execute(
                        f"DELETE FROM {table} WHERE run_id IN ({rid_ph})",
                        run_ids,
                    )
                except sqlite3.OperationalError:
                    pass  # Table may not exist in older schemas.
            conn.commit()
    except Exception:
        # Teardown cleanup must never fail the session.
        pass
