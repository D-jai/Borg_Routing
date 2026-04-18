"""Top-level pipeline entry point.

run_pipeline() creates the run record, drives orchestration, marks completion,
and returns a summary dict suitable for callers (API endpoints, tests).

Error codes:
- BRT-PIP-001: run creation failed
- BRT-PIP-002: orchestration failed (engine raised)
- BRT-PIP-003: run completion update failed
- BRT-PIP-004: invalid user_instruction (empty)
- BRT-PIP-005: run already in terminal state
"""
import time
from typing import Optional
from uuid import UUID

from src.db.schema import DB_PATH
from src.db.state import create_run, get_envelopes, get_run, update_run_status
from src.orchestrator.engine import orchestrate_run


async def run_pipeline(
    user_instruction: str,
    thread_window: list[dict] = None,
    rolling_summary: str = "",
    db_path: str = DB_PATH,
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
        run_id = await create_run(user_instruction, db_path=db_path)
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
        )
    except Exception as exc:
        status = "failed"
        error = f"BRT-PIP-002: orchestration failed: {exc}"
        response = f"I was unable to complete this request. (Run: {run_id})"

    latency_ms = int((time.monotonic() - t0) * 1000)

    try:
        await update_run_status(run_id, status, db_path=db_path)
    except Exception as exc:
        error = error or f"BRT-PIP-003: run completion update failed: {exc}"

    try:
        envelopes = await get_envelopes(run_id, db_path=db_path)
        envelope_count = len(envelopes)
    except Exception:
        envelope_count = 0

    return {
        "run_id": str(run_id),
        "response": response,
        "status": status,
        "latency_ms": latency_ms,
        "envelope_count": envelope_count,
        "error": error,
    }
