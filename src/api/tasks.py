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
- BRT-LANE-001: a document path was refused (400, no run is created)
- BRT-LANE-005: documents sent with the lane off, or a bad lane setting (400)
- BRT-LANE-008: resume refused for a run that read documents, or when that
  could not be checked (409, fails closed)
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
    get_descendant_run_ids,
    get_envelopes,
    get_outbound_requests,
    get_run,
    get_run_counts,
    get_run_documents,
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
    # On-premise lane: {"folder": "<relative path>"} or {"files": [...]}
    # under BORG_DOCS_ROOT. Refused while lane_mode is off.
    documents: Optional[dict] = None


def lane_admin_allowed(request=None) -> bool:
    """THE single hook for the admin token (work order 4.4, not built yet).

    With the lane on, the outbound bodies in run detail belong behind the
    admin token; today GET /runs/{id} is the only caller. The design note
    also puts /logs/entities and /logs/export behind the token: those routes
    do not call this hook yet (work order 4.4 wires them). Until 4.4 adds the
    token this returns True; 4.4 replaces its body with the token check.
    """
    return True


class RatingRequest(BaseModel):
    rating: int  # 1 = thumbs up, -1 = thumbs down


async def _run_in_background(
    instruction: str,
    run_id_str: str,
    db_path: str,
    root_instruction: Optional[str] = None,
    route_options: Optional[dict] = None,
    lane=None,
) -> None:
    """Background task: drive the pipeline for a submitted run.

    ``lane`` is the on-premise lane state prepared when the task was
    submitted (documents already checked and loaded), or None.

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
    # Visible as running from the moment the work starts, so a second resume
    # of the same run is refused instead of racing this one.
    await update_run_status(run_id, "running", db_path=db_path)
    try:
        await _asyncio.wait_for(
            orchestrate_run(
                run_id=run_id,
                user_instruction=instruction,
                db_path=db_path,
                root_instruction=root_instruction,
                **({"route_options": route_options} if route_options else {}),
                **({"lane": lane} if lane is not None else {}),
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

    # On-premise lane: check and load the documents before a run exists, so
    # a refused path creates nothing and sends nothing.
    from src.lane.context import prepare_lane
    from src.lane.errors import LaneError
    try:
        lane = await prepare_lane(task_req.documents, DB_PATH)
    except LaneError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": exc.code, "message": str(exc)},
        )

    # Create run record synchronously so the caller gets a valid run_id
    from src.db.state import create_run
    try:
        run_id = await create_run(
            task_req.instruction,
            db_path=DB_PATH,
            **({"documents": lane.manifest()} if lane is not None and lane.has_documents else {}),
        )
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
        **({"lane": lane} if lane is not None else {}),
    )

    return JSONResponse(
        status_code=202,
        content={"run_id": str(run_id), "status": "pending"},
    )


def _attach_routing(envelopes: list[dict], events: list[dict]) -> None:
    """Set ``routing`` on each envelope from its route event (else null).

    An orchestrate step also gets ``child_run_id`` in its routing, read from
    the envelope's recorded output once the step has finished.
    """
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
        routing = routes.get(env.get("envelope_id"))
        if routing is not None and routing.get("served_by") == "child_run":
            try:
                inbound = json.loads(env.get("inbound_payload") or "null") or {}
                child_id = (inbound.get("output") or {}).get("child_run_id")
            except (TypeError, ValueError, AttributeError):
                child_id = None
            routing = dict(routing, child_run_id=child_id)
        env["routing"] = routing


@router.get("/runs/{run_id}")
async def get_run_state(run_id: str) -> JSONResponse:
    """Return the full run state including envelopes and log events.

    Response shape:
    {
        "run": { ...run record... },
        "envelopes": [ ...envelope records, each with "routing" (why its model
                        was chosen, or null)... ],
        "child_envelopes": [ ...envelopes of descendant (child) runs, same
                        shape, each carrying its own run_id... ],
        "events": [ ...log events... ],
        "digest": {cost_dollars, baseline_dollars, tokens_input, tokens_output,
                   token_count, summary} or null if no digest was saved yet,
        "outbound": [ ...on-premise lane: every request this run and its child
                     runs sent off this machine, oldest first, each with its
                     run_id, url, destination, attempt, status and the exact
                     body sent; empty when the lane was off... ],
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
    _attach_routing(envelopes, events)

    # Child runs (orchestrate nodes) choose their own models; show their steps
    # too, each envelope tagged with its own run_id.
    child_envelopes: list[dict] = []
    try:
        child_ids = await get_descendant_run_ids(run_uuid, db_path=DB_PATH)
        for child_id in child_ids:
            child_envs = await get_envelopes(_uuid.UUID(child_id), db_path=DB_PATH)
            child_events: list[dict] = []
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                async with db.execute(
                    "SELECT * FROM logs WHERE run_id=? ORDER BY created_at",
                    (child_id,),
                ) as cur:
                    child_events = [dict(r) for r in await cur.fetchall()]
            _attach_routing(child_envs, child_events)
            child_envelopes.extend(child_envs)
    except Exception:
        pass

    try:
        digest = await get_run_digest(run_uuid, db_path=DB_PATH)
    except Exception:
        digest = None

    # On-premise lane (P3): what left this machine, for the run tree.
    try:
        outbound = await get_outbound_requests(run_uuid, db_path=DB_PATH)
    except Exception:
        outbound = []
    if not lane_admin_allowed():
        outbound = [dict(r, body=None) for r in outbound]

    return JSONResponse(content={
        "run": run,
        "envelopes": envelopes,
        "child_envelopes": child_envelopes,
        "events": events,
        "digest": digest,
        "outbound": outbound,
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


async def _stored_route_options(run_uuid, db_path: str) -> Optional[dict]:
    """The chat alias the run (or, for a child run, its nearest ancestor that
    has one) started with, or None."""
    current = run_uuid
    for _ in range(_MAX_PARENT_HOPS + 1):
        try:
            row = await get_run(current, db_path=db_path)
        except ValueError:
            return None
        raw = row.get("route_options")
        if raw:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                return None
            return value if isinstance(value, dict) else None
        current = row.get("parent_run_id")
        if not current:
            return None
    return None


@router.post("/runs/{run_id}/resume", status_code=202)
async def resume_run_endpoint(
    run_id: str,
    background_tasks: BackgroundTasks,
) -> JSONResponse:
    """Run a failed run again: it is planned from the start, from the run's own
    instruction (it does not continue where it stopped).

    Refused: a running run (409), a completed run (400), a run that read
    documents (409). A pending run with no saved steps has no pending work.
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

    # A run that is still running is never resumed: a second copy would race
    # the first. (A missing run falls through to the checks below.)
    try:
        current = await get_run(run_uuid, db_path=DB_PATH)
    except ValueError:
        current = None
    if current is not None and current["status"] == "running":
        raise HTTPException(
            status_code=409,
            detail={
                "error": "BRT-API-024",
                "message": "this run is still running, so it cannot be run again; "
                           "wait for it to finish or fail",
            },
        )
    # A pending run with work in flight was submitted a moment ago and has
    # not been marked running yet: it is just as busy.
    if current is not None and current["status"] == "pending":
        in_flight = [
            e for e in await get_envelopes(run_uuid, db_path=DB_PATH)
            if e.get("status") == "in_flight"
        ]
        if in_flight:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "BRT-API-024",
                    "message": "this run has work in flight, so it cannot be run "
                               "again; wait for it to finish or fail",
                },
            )

    # On-premise lane: a run that read documents is never resumed (design
    # note 5.7); a resume would start a fresh run without its documents.
    # Fails closed: if the lookup itself fails, the resume is refused.
    try:
        read_documents = await get_run_documents(run_uuid, db_path=DB_PATH)
    except Exception:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "BRT-LANE-008",
                "message": "could not check whether this run read documents, so it "
                           "is not resumed; try again, or start a new run",
            },
        )
    if read_documents:
        raise HTTPException(
            status_code=409,
            detail={
                "error": "BRT-LANE-008",
                "message": "this run read documents, so it cannot be resumed; "
                           "start a new run with the same documents",
            },
        )

    # The re-run takes the lane exactly as POST /v1/task does (no documents:
    # a run that read some was refused above), so all_local stays local.
    from src.lane.context import prepare_lane
    from src.lane.errors import LaneError
    try:
        lane = await prepare_lane(None, DB_PATH)
    except LaneError as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": exc.code, "message": str(exc)},
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

    # A failed run with no saved failed or unfinished step failed before it
    # had steps (at planning, e.g. BRT-ORC-007): it is planned again from its
    # own instruction. Anything else with nothing to do has no pending work.
    replan = (
        not tasks_to_retry
        and current is not None
        and current["status"] == "failed"
    )
    if not tasks_to_retry and not replan:
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
        route_options = await _stored_route_options(run_uuid, db_path=DB_PATH)
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
        route_options=route_options,
        **({"lane": lane} if lane is not None else {}),
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
async def list_runs(limit: int = 50, include_bypass: bool = False) -> JSONResponse:
    """Return recent runs ordered by created_at descending.

    Direct-to-model (bypass) runs are left out unless ``include_bypass=true``.
    """
    try:
        runs = await get_runs_list(
            limit=limit, db_path=DB_PATH, include_bypass=include_bypass
        )
        counts = await get_run_counts(db_path=DB_PATH, include_bypass=include_bypass)
        return JSONResponse(content={"runs": runs, "counts": counts})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-025", "message": f"Run list failed: {exc}"},
        )
