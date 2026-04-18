"""Fire-and-forget async event logger.

All log writes are non-blocking: they are scheduled via asyncio.create_task
so the caller never waits for the DB write to complete.

Event types: model_call | orchestrator_decision | child_residue |
             escalation | deferral | reorchestration | error

Error codes:
- BRT-LOG-001: invalid event_type value
- BRT-LOG-002: log write failed (logged to stderr, never re-raised)
"""
import asyncio
import json
import sys
from typing import Optional
from uuid import UUID

import aiosqlite

from src.db.schema import DB_PATH

VALID_EVENT_TYPES = {
    "model_call",
    "orchestrator_decision",
    "child_residue",
    "escalation",
    "deferral",
    "reorchestration",
    "error",
}


async def _write_event_task(
    event_type: str,
    run_id: Optional[str],
    envelope_id: Optional[str],
    model_id: Optional[str],
    tokens_input: Optional[int],
    tokens_output: Optional[int],
    dollars: Optional[float],
    latency_ms: Optional[int],
    decision_json: Optional[str],
    payload_in: Optional[str],
    payload_out: Optional[str],
    db_path: str,
) -> None:
    """Internal coroutine that does the actual DB writes."""
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                """INSERT INTO logs(
                    run_id, envelope_id, event_type, model_id,
                    tokens_input, tokens_output, dollars, latency_ms, decision_json
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    run_id, envelope_id, event_type, model_id,
                    tokens_input, tokens_output, dollars, latency_ms, decision_json,
                ),
            )
            log_id = cur.lastrowid

            if payload_in is not None:
                await db.execute(
                    "INSERT INTO log_payloads(log_id, direction, text) VALUES (?,?,?)",
                    (log_id, "in", payload_in),
                )
            if payload_out is not None:
                await db.execute(
                    "INSERT INTO log_payloads(log_id, direction, text) VALUES (?,?,?)",
                    (log_id, "out", payload_out),
                )
            await db.commit()
    except Exception as exc:
        print(f"BRT-LOG-002: log write failed for event_type={event_type}: {exc}", file=sys.stderr)


async def write_event(
    event_type: str,
    run_id: Optional[UUID] = None,
    envelope_id: Optional[UUID] = None,
    model_id: Optional[str] = None,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
    dollars: Optional[float] = None,
    latency_ms: Optional[int] = None,
    decision: Optional[dict] = None,
    payload_in: Optional[str] = None,
    payload_out: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Schedule a non-blocking log write via asyncio.create_task.

    Raises:
        ValueError: BRT-LOG-001 if event_type is not in the allowed set.
    """
    if event_type not in VALID_EVENT_TYPES:
        raise ValueError(
            f"BRT-LOG-001: invalid event_type '{event_type}'. "
            f"Must be one of {sorted(VALID_EVENT_TYPES)}"
        )

    decision_json = json.dumps(decision) if decision is not None else None

    asyncio.create_task(
        _write_event_task(
            event_type=event_type,
            run_id=str(run_id) if run_id else None,
            envelope_id=str(envelope_id) if envelope_id else None,
            model_id=model_id,
            tokens_input=tokens_input,
            tokens_output=tokens_output,
            dollars=dollars,
            latency_ms=latency_ms,
            decision_json=decision_json,
            payload_in=payload_in,
            payload_out=payload_out,
            db_path=db_path,
        )
    )
