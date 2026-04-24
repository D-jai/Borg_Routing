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
from src.db.state import get_envelopes, get_run, get_runs_list, resume_run, set_run_rating
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
) -> None:
    """Background task: drive the pipeline for a submitted run."""
    import uuid
    # run_pipeline creates its own run_id — but we need to use the one we
    # pre-created (already created by the POST handler). For background runs,
    # we just drive orchestration directly.
    from src.orchestrator.engine import orchestrate_run
    from src.db.state import update_run_status
    import uuid as _uuid
    run_id = _uuid.UUID(run_id_str)
    try:
        await orchestrate_run(run_id=run_id, user_instruction=instruction, db_path=db_path)
        await update_run_status(run_id, "completed", db_path=db_path)
    except Exception:
        try:
            await update_run_status(run_id, "failed", db_path=db_path)
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

    return JSONResponse(content={
        "run": run,
        "envelopes": envelopes,
        "events": events,
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

    background_tasks.add_task(
        _run_in_background,
        instruction=tasks_to_retry[0].instruction if tasks_to_retry else "",
        run_id_str=run_id,
        db_path=DB_PATH,
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
