"""Batch / Task API — Surface C (partial).

POST /v1/task           — submit async task, returns run_id immediately (202 Accepted)
GET  /runs/{id}         — fetch full run state (envelopes, events, digest)
GET  /runs/{id}/stream  — SSE stream of envelope status changes
POST /runs/{id}/resume  — re-dispatch failed/in-flight envelopes
GET  /runs              — list recent runs (dashboard support)

Error codes:
- BRT-API-020: malformed task request body
- BRT-API-021: run not found
- BRT-API-022: task submission failed
- BRT-API-023: run fetch failed
- BRT-API-024: resume failed
- BRT-API-025: run list failed
- BRT-API-028: stream run not found
- BRT-API-029: stream DB error
"""
import asyncio
import json
from typing import Optional

import aiosqlite
from fastapi import APIRouter, BackgroundTasks, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from src.db.schema import DB_PATH
from src.db.state import (
    compute_and_save_digest,
    get_envelopes,
    get_run,
    get_run_digest,
    get_runs_list,
    resume_run,
    set_run_rating,
)
from src.pipeline.runner import run_pipeline

router = APIRouter()


class TaskRequest(BaseModel):
    instruction: str
    inputs_ref: Optional[str] = None


class RatingRequest(BaseModel):
    rating: int  # 1 = thumbs up, -1 = thumbs down


async def _run_in_background(
    instruction: str,
    run_id_str: str,
    db_path: str,
    root_instruction: Optional[str] = None,
) -> None:
    """Background task: drive the pipeline for a submitted run.

    ``root_instruction`` is the operator's verbatim instruction when it
    differs from ``instruction`` (a resumed child run); Law 1 governs every
    call with it.

    Persists ``failure_reason`` on failure so ``GET /runs/<id>`` shows why
    the run failed (BRT-PIP-010). Also emits a trace-visible error event
    carrying the traceback.

    BRT-PIP-008 — wall-clock budget applies to background runs too. Cap
    is resolved through the same DB → env → default path the Admin UI
    displays.
    """
    import asyncio as _asyncio
    import traceback as _tb
    import uuid as _uuid
    from src.orchestrator.engine import orchestrate_run
    from src.db.state import update_run_status
    from src.logging.writer import write_event
    from src.pipeline.runner import resolve_max_run_seconds

    run_id = _uuid.UUID(run_id_str)
    max_run_seconds = await resolve_max_run_seconds(db_path=db_path)
    try:
        await _asyncio.wait_for(
            orchestrate_run(
                run_id=run_id,
                user_instruction=instruction,
                db_path=db_path,
                root_instruction=root_instruction,
            ),
            timeout=max_run_seconds,
        )
        await update_run_status(run_id, "completed", db_path=db_path)
    except _asyncio.TimeoutError:
        failure_reason = (
            f"BRT-PIP-008: run exceeded wall-clock budget ({max_run_seconds}s)."
        )
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-PIP-008",
                    "msg": failure_reason,
                    "max_run_seconds": max_run_seconds,
                },
                db_path=db_path,
            )
        except Exception:
            pass
        try:
            await update_run_status(
                run_id, "failed", failure_reason=failure_reason, db_path=db_path
            )
        except Exception:
            pass
    except Exception as exc:
        tb = _tb.format_exc()
        failure_reason = f"BRT-PIP-002: orchestration failed: {exc}"
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-PIP-010",
                    "msg": failure_reason,
                    "traceback": tb[-2000:],
                },
                db_path=db_path,
            )
        except Exception:
            pass
        try:
            await update_run_status(
                run_id, "failed", failure_reason=failure_reason, db_path=db_path
            )
        except Exception:
            pass
    finally:
        # Digest for every background run, same as the pipeline does: wait for
        # the fire-and-forget log writes, then total the logged rows.
        try:
            from src.logging.writer import flush_pending  # noqa: PLC0415

            await flush_pending()
            await compute_and_save_digest(run_id, db_path=db_path)
        except Exception:
            pass


@router.post("/v1/task", status_code=202)
async def submit_task(
    task_req: TaskRequest,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Submit an async task. Returns run_id immediately; processing happens in background.

    Poll GET /runs/{run_id} for status.
    """
    if not task_req.instruction or not task_req.instruction.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-020", "message": "instruction is required"},
        )

    # Create run record synchronously so the caller gets a valid run_id
    from src.db.state import create_run
    try:
        run_id = await create_run(task_req.instruction, db_path=DB_PATH)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-022", "message": f"Task submission failed: {exc}"},
        )

    # Kick off background processing
    background_tasks.add_task(
        _run_in_background,
        instruction=task_req.instruction,
        run_id_str=str(run_id),
        db_path=DB_PATH,
    )

    return JSONResponse(
        status_code=202,
        content={"run_id": str(run_id), "status": "pending"},
    )


@router.get("/runs/{run_id}")
async def get_run_state(run_id: str) -> JSONResponse:
    """Return the full run state including envelopes and log events.

    Response shape:
    {
        "run": { ...run record... },
        "envelopes": [ ...envelope records... ],
        "events": [ ...log events... ],
        "digest": {cost_dollars, baseline_dollars, tokens_input, tokens_output,
                   token_count, summary} or null if no digest was saved yet,
    }
    """
    import uuid as _uuid
    try:
        run_uuid = _uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-021", "message": f"Invalid run_id format: {run_id}"},
        )

    try:
        run = await get_run(run_uuid, db_path=DB_PATH)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-API-021", "message": f"Run '{run_id}' not found"},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-023", "message": f"Run fetch failed: {exc}"},
        )

    try:
        envelopes = await get_envelopes(run_uuid, db_path=DB_PATH)
    except Exception:
        envelopes = []

    # Fetch log events for this run
    events = []
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM logs WHERE run_id=? ORDER BY created_at",
                (run_id,),
            ) as cur:
                rows = await cur.fetchall()
            events = [dict(r) for r in rows]
    except Exception:
        pass

    # Work order 1.5 - each envelope carries why its model was chosen.
    # Only a routed step has a route event; any other envelope gets null.
    routes: dict[str, dict] = {}
    for ev in events:
        if ev.get("event_type") != "orchestrator_decision" or not ev.get("envelope_id"):
            continue
        try:
            decision = json.loads(ev.get("decision_json") or "null")
        except (TypeError, ValueError):
            continue
        if isinstance(decision, dict) and decision.get("mode") == "route":
            routes[ev["envelope_id"]] = decision
    for env in envelopes:
        env["routing"] = routes.get(env.get("envelope_id"))

    try:
        digest = await get_run_digest(run_uuid, db_path=DB_PATH)
    except Exception:
        digest = None

    return JSONResponse(content={
        "run": run,
        "envelopes": envelopes,
        "events": events,
        "digest": digest,
    })


@router.get("/runs/{run_id}/stream")
async def stream_run_state(run_id: str) -> StreamingResponse:
    """Stream envelope status changes as Server-Sent Events.

    Emits ``data: {json}\\n\\n`` for each new or changed envelope.
    Closes automatically when the run reaches a terminal state or after 90 s.

    Event shapes:
    - ``{"type": "envelope_update", "envelope": {...}}``
    - ``{"type": "run_terminal",    "status": "completed"|"failed", "run_id": "..."}``
    - ``{"type": "error",           "error": "BRT-API-029", "message": "..."}``

    Error codes: BRT-API-028 (run not found), BRT-API-029 (stream DB error).
    """
    import uuid as _uuid

    try:
        run_uuid = _uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-028", "message": f"Invalid run_id format: {run_id}"},
        )

    try:
        await get_run(run_uuid, db_path=DB_PATH)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-API-028", "message": f"Run '{run_id}' not found"},
        )

    async def _event_generator():
        seen: dict[str, str] = {}  # envelope_id -> last emitted status
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 90.0

        try:
            while loop.time() < deadline:
                try:
                    run = await get_run(run_uuid, db_path=DB_PATH)
                    envelopes = await get_envelopes(run_uuid, db_path=DB_PATH)
                except Exception as exc:
                    yield (
                        f"data: {json.dumps({'type': 'error', 'error': 'BRT-API-029', 'message': str(exc)})}\n\n"
                    )
                    return

                for env in envelopes:
                    eid = env["envelope_id"]
                    new_status = env.get("status", "")
                    if seen.get(eid) != new_status:
                        seen[eid] = new_status
                        yield f"data: {json.dumps({'type': 'envelope_update', 'envelope': env})}\n\n"

                run_status = run.get("status", "")
                if run_status in ("completed", "failed"):
                    yield (
                        f"data: {json.dumps({'type': 'run_terminal', 'status': run_status, 'run_id': run_id})}\n\n"
                    )
                    return

                await asyncio.sleep(0.5)
        except asyncio.CancelledError:
            return

    return StreamingResponse(
        _event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


_MAX_PARENT_HOPS = 32


async def _run_instructions(run_uuid, db_path: str) -> tuple[str, str]:
    """Return (this run's instruction, its root run's instruction).

    Walks ``parent_run_id`` up to the top-level run, whose instruction is
    the operator's. The walk stops at a missing parent or after
    ``_MAX_PARENT_HOPS`` rows, keeping the highest instruction found.
    """
    run = await get_run(run_uuid, db_path=db_path)
    own = run.get("user_instruction") or ""
    root = own
    parent = run.get("parent_run_id")
    hops = 0
    while parent and hops < _MAX_PARENT_HOPS:
        try:
            row = await get_run(parent, db_path=db_path)
        except ValueError:
            break
        root = row.get("user_instruction") or root
        parent = row.get("parent_run_id")
        hops += 1
    return own, root


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run_endpoint(
    run_id: str,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Re-dispatch all failed or in-flight envelopes for a run.

    Returns 202 immediately. Polling GET /runs/{id} shows updated status.
    """
    import uuid as _uuid
    try:
        run_uuid = _uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-021", "message": f"Invalid run_id format: {run_id}"},
        )

    try:
        tasks_to_retry = await resume_run(run_uuid, db_path=DB_PATH)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-024", "message": str(exc)},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-024", "message": f"Resume failed: {exc}"},
        )

    if not tasks_to_retry:
        return JSONResponse(
            status_code=202,
            content={"run_id": run_id, "status": "no_pending_work"},
        )

    # Law 1 — the re-run is governed by the run row's own operator
    # instruction, never by a step's text (envelopes saved before
    # governance_instruction existed carry only the step text). A child run
    # row also gets its root run's instruction as the governing one.
    try:
        run_instruction, root_instruction = await _run_instructions(
            run_uuid, db_path=DB_PATH
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-024", "message": f"Resume failed: {exc}"},
        )

    background_tasks.add_task(
        _run_in_background,
        instruction=run_instruction,
        run_id_str=run_id,
        db_path=DB_PATH,
        root_instruction=(
            root_instruction if root_instruction != run_instruction else None
        ),
    )

    return JSONResponse(
        status_code=202,
        content={"run_id": run_id, "status": "resuming", "retrying": len(tasks_to_retry)},
    )


@router.post("/runs/{run_id}/rating", status_code=200)
async def rate_run(run_id: str, req: RatingRequest) -> JSONResponse:
    """Set thumbs-up (1) or thumbs-down (-1) rating for a completed run.

    Error codes: BRT-API-026 (invalid rating), BRT-API-027 (run not found).
    """
    import uuid as _uuid
    if req.rating not in (1, -1):
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-026", "message": "rating must be 1 (up) or -1 (down)"},
        )
    try:
        run_uuid = _uuid.UUID(run_id)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-027", "message": f"Invalid run_id format: {run_id}"},
        )
    try:
        await set_run_rating(run_uuid, req.rating, db_path=DB_PATH)
    except ValueError as exc:
        msg = str(exc)
        if "BRT-DB-010" in msg:
            raise HTTPException(
                status_code=404,
                detail={"error": "BRT-API-027", "message": f"Run '{run_id}' not found"},
            )
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-026", "message": msg},
        )
    return JSONResponse(content={"run_id": run_id, "rating": req.rating})


@router.get("/runs")
async def list_runs(limit: int = 50) -> JSONResponse:
    """Return recent runs ordered by created_at descending."""
    try:
        runs = await get_runs_list(limit=limit, db_path=DB_PATH)
        return JSONResponse(content={"runs": runs})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-025", "message": f"Run list failed: {exc}"},
        )
