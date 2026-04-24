"""Top-level pipeline entry point.

run_pipeline() creates the run record, drives orchestration, marks completion,
and returns a summary dict suitable for callers (API endpoints, tests).

Error codes:
- BRT-PIP-001: run creation failed
- BRT-PIP-002: orchestration failed (engine raised)
- BRT-PIP-003: run completion update failed
- BRT-PIP-004: invalid user_instruction (empty)
- BRT-PIP-005: run already in terminal state
- BRT-PIP-010: failure_reason persisted (informational; written whenever a
               run ends in status='failed'). Not an error itself — exists
               so operators can grep for 'BRT-PIP-010' in the error event
               stream and see the user-facing failure message.
"""
import logging
import time
import traceback
from typing import Optional
from uuid import UUID

from src.db.schema import DB_PATH
from src.db.state import (
    compute_and_save_digest,
    create_run,
    get_config,
    get_envelopes,
    get_run,
    update_run_status,
)
from src.logging.writer import write_event
from src.orchestrator.engine import orchestrate_run

logger = logging.getLogger(__name__)


async def run_pipeline(
    user_instruction: str,
    thread_window: list[dict] = None,
    rolling_summary: str = "",
    db_path: str = DB_PATH,
    parent_run_id: Optional[UUID] = None,
    current_depth: int = 0,
    thread_id: Optional[str] = None,
) -> dict:
    """Create a run, orchestrate it, return a summary.

    Returns:
        {
            "run_id": str,
            "response": str,
            "status": "completed" | "failed",
            "latency_ms": int,
            "envelope_count": int,
            "error": str | None,
        }

    Never raises — errors are captured in the returned dict.
    """
    if thread_window is None:
        thread_window = []

    # Auto-load rolling_summary from config when thread_id is provided and
    # no explicit summary was passed by the caller.
    if thread_id and not rolling_summary:
        stored = await get_config(f"rolling_summary:{thread_id}", db_path=db_path)
        if stored:
            rolling_summary = stored

    if not user_instruction or not user_instruction.strip():
        return {
            "run_id": None,
            "response": "",
            "status": "failed",
            "latency_ms": 0,
            "envelope_count": 0,
            "error": "BRT-PIP-004: user_instruction is empty",
        }

    # Create run record
    try:
        run_id = await create_run(
            user_instruction,
            parent_run_id=parent_run_id,
            thread_id=thread_id,
            db_path=db_path,
        )
    except Exception as exc:
        return {
            "run_id": None,
            "response": "",
            "status": "failed",
            "latency_ms": 0,
            "envelope_count": 0,
            "error": f"BRT-PIP-001: run creation failed: {exc}",
        }

    await update_run_status(run_id, "running", db_path=db_path)

    t0 = time.monotonic()
    response = ""
    status = "completed"
    error: Optional[str] = None

    try:
        response = await orchestrate_run(
            run_id=run_id,
            user_instruction=user_instruction,
            thread_window=thread_window,
            rolling_summary=rolling_summary,
            db_path=db_path,
            current_depth=current_depth,
        )
    except Exception as exc:
        status = "failed"
        tb = traceback.format_exc()
        error = f"BRT-PIP-002: orchestration failed: {exc}"
        logger.exception("run_pipeline orchestration failed for run_id=%s", run_id)
        response = f"I was unable to complete this request. (Run: {run_id})"
        # BRT-PIP-010 — emit a trace-visible error event carrying the
        # traceback so the UI timeline + `GET /runs/<id>` both show why
        # the run failed. Fire-and-forget; a logging hiccup never shadows
        # the real failure.
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-PIP-010",
                    "msg": error,
                    "traceback": tb[-2000:],
                },
                db_path=db_path,
            )
        except Exception:
            pass

    latency_ms = int((time.monotonic() - t0) * 1000)

    try:
        await update_run_status(
            run_id,
            status,
            failure_reason=error if status == "failed" else None,
            db_path=db_path,
        )
    except Exception as exc:
        error = error or f"BRT-PIP-003: run completion update failed: {exc}"

    try:
        envelopes = await get_envelopes(run_id, db_path=db_path)
        envelope_count = len(envelopes)
    except Exception:
        envelope_count = 0

    # Fire-and-forget digest at completion
    try:
        await compute_and_save_digest(run_id, db_path=db_path)
    except Exception:
        pass

    return {
        "run_id": str(run_id),
        "response": response,
        "status": status,
        "latency_ms": latency_ms,
        "envelope_count": envelope_count,
        "error": error,
    }
