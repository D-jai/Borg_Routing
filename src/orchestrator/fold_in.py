"""Fold-in verifier — Law 2 (No Forgotten Scratch) enforcement.

Called by the engine after every child orchestrate/answerer completes.
Checks for residual scratch rows left by the child; logs them as diagnostic
signals; cleans them up; returns a brief digest for the parent's context.

DESIGN_PRINCIPLES.md §2 (Law 2):
  "Every write to SQLite has an owner. The child is responsible for cleaning
   up after itself. The parent verifies at fold-in and backstops if the child
   failed to comply. Residue is a diagnostic signal that something went wrong."

Error codes:
- BRT-ORC-010: fold-in check failed (DB error)
- BRT-ORC-011: residue found (informational — logged, not raised)
- BRT-ORC-012: cleanup failed after residue detection
"""
from typing import Optional
from uuid import UUID

from src.db.scratch import (
    count_scratch_for_level,
    delete_scratch_for_level,
    read_scratch_for_level,
)
from src.logging.writer import write_event


async def fold_in(
    run_id: UUID,
    child_level_path: str,
    child_model_id: Optional[str] = None,
    db_path: str = "data/borg_route.db",
) -> dict:
    """Verify that the child cleaned up its scratch; backstop if it didn't.

    1. Count scratch rows for child_level_path.
    2. If count > 0 (residue):
       a. Read the residue rows.
       b. Log a 'child_residue' event with run_id, model_id, row count, sample.
       c. Delete all residue rows.
    3. Return a digest dict (empty if no residue, summary if residue found).

    Raises:
        RuntimeError: BRT-ORC-010 if the DB check itself fails (unexpected).
    """
    try:
        count = await count_scratch_for_level(run_id, child_level_path, db_path=db_path)
    except Exception as exc:
        raise RuntimeError(
            f"BRT-ORC-010: fold-in check failed for run {run_id} "
            f"level {child_level_path}: {exc}"
        ) from exc

    if count == 0:
        return {}

    # Residue detected — read, log, clean
    try:
        residue_rows = await read_scratch_for_level(run_id, child_level_path, db_path=db_path)
    except Exception:
        residue_rows = []

    sample = residue_rows[:3]

    # Log diagnostic event (fire-and-forget)
    await write_event(
        "child_residue",
        run_id=run_id,
        model_id=child_model_id,
        decision={
            "level_path": child_level_path,
            "residue_count": count,
            "rows_sample": sample,
        },
        db_path=db_path,
    )

    # Clean up (backstop)
    try:
        await delete_scratch_for_level(run_id, child_level_path, db_path=db_path)
    except Exception as exc:
        raise RuntimeError(
            f"BRT-ORC-012: cleanup failed after residue detection for "
            f"run {run_id} level {child_level_path}: {exc}"
        ) from exc

    # Return digest for parent's context
    return {
        "residue_found": True,
        "residue_count": count,
        "level_path": child_level_path,
        "child_model": child_model_id,
        "sample_tables": list({r.get("table_name") for r in residue_rows}),
        "sample_data": [r.get("data") for r in sample],
    }
