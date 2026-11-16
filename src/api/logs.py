"""Logs API — observability endpoints.

GET  /logs                — list recent log events
GET  /logs/entities       — list extracted NER entities (optionally filtered by run_id/label)
GET  /logs/cost-summary   — aggregate cost and token counts per model
GET  /logs/outlier-runs   — the most expensive or slowest runs (sort=cost|duration)
GET  /logs/export         — export log events as JSONL

Error codes:
- BRT-API-040: logs fetch failed
- BRT-API-041: entities fetch failed
- BRT-API-042: cost summary fetch failed
- BRT-API-043: export failed
- BRT-API-044: outlier runs fetch failed
"""
import json

import aiosqlite
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.auth import require_admin
from src.db.schema import DB_PATH

router = APIRouter(prefix="/logs", tags=["logs"])


@router.get("", dependencies=[Depends(require_admin)])
async def list_logs(
    limit: int = Query(100, ge=1, le=1000),
    run_id: str = Query(None),
    event_type: str = Query(None),
) -> JSONResponse:
    """Return recent log events, optionally filtered by run_id or event_type."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM logs"
            params = []
            conditions = []
            if run_id:
                conditions.append("run_id=?")
                params.append(run_id)
            if event_type:
                conditions.append("event_type=?")
                params.append(event_type)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return JSONResponse(content={"logs": [dict(r) for r in rows]})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-040", "message": f"Logs fetch failed: {exc}"},
        )


@router.get("/entities", dependencies=[Depends(require_admin)])
async def list_entities(
    limit: int = Query(200, ge=1, le=2000),
    run_id: str = Query(None),
    label: str = Query(None),
) -> JSONResponse:
    """Return NER entities extracted from log payloads."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM log_entities"
            params = []
            conditions = []
            if run_id:
                conditions.append("run_id=?")
                params.append(run_id)
            if label:
                conditions.append("label=?")
                params.append(label)
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY created_at DESC LIMIT ?"
            params.append(limit)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()
        return JSONResponse(content={"entities": [dict(r) for r in rows]})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-041", "message": f"Entities fetch failed: {exc}"},
        )


# Rows that carry a paid call: every model_call, plus each orchestrator_decision
# that recorded tokens or dollars (route decisions carry neither, so they are
# left out and nothing is counted twice).
_COST_ROWS = (
    "(event_type='model_call' OR (event_type='orchestrator_decision' "
    "AND (dollars IS NOT NULL OR tokens_input IS NOT NULL)))"
)


@router.get("/cost-summary")
async def cost_summary() -> JSONResponse:
    """Return aggregate cost and token counts grouped by model_id.

    Sums ``model_call`` rows plus ``orchestrator_decision`` rows that carry
    tokens or dollars (plan, re_plan and compose calls).
    """
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""SELECT
                     model_id,
                     COUNT(*) AS call_count,
                     COALESCE(SUM(tokens_input), 0)  AS total_tokens_in,
                     COALESCE(SUM(tokens_output), 0) AS total_tokens_out,
                     COALESCE(SUM(dollars), 0)        AS total_cost
                   FROM logs
                   WHERE {_COST_ROWS}
                   GROUP BY model_id
                   ORDER BY total_cost DESC"""
            ) as cur:
                rows = await cur.fetchall()

            # Overall totals
            async with db.execute(
                f"""SELECT
                     COUNT(*) AS total_calls,
                     COALESCE(SUM(tokens_input), 0)  AS total_tokens_in,
                     COALESCE(SUM(tokens_output), 0) AS total_tokens_out,
                     COALESCE(SUM(dollars), 0)        AS total_cost
                   FROM logs WHERE {_COST_ROWS}"""
            ) as cur:
                totals = await cur.fetchone()

        return JSONResponse(content={
            "by_model": [dict(r) for r in rows],
            "totals": dict(totals) if totals else {},
        })
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-042", "message": f"Cost summary failed: {exc}"},
        )


@router.get("/outlier-runs")
async def outlier_runs(
    sort: str = Query("cost", pattern="^(cost|duration)$"),
    limit: int = Query(5, ge=1, le=50),
) -> JSONResponse:
    """Root runs ranked by what they cost or how long they took (never by prompt length).

    Only root runs are listed (no ``parent_run_id``): a child run is part of
    its root. Cost is the logged dollars of the whole run tree (the run and
    every descendant run, the same rows the cost summary counts). Duration is
    the span of the tree's logged activity, newest log minus oldest, so a run
    with no logged activity has no duration and is left out of the duration
    sort. Direct-to-model (bypass) runs are left out.
    """
    order = "cost DESC" if sort == "cost" else "duration_s DESC"
    where = "WHERE duration_s IS NOT NULL" if sort == "duration" else ""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                f"""WITH RECURSIVE tree(root, run_id) AS (
                      SELECT run_id, run_id FROM runs
                      WHERE parent_run_id IS NULL AND COALESCE(kind, 'orchestrated') != 'bypass'
                      UNION ALL
                      SELECT t.root, r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id
                    ), totals AS (
                      SELECT t.root AS run_id,
                             COALESCE(SUM(CASE WHEN {_COST_ROWS} THEN l.dollars END), 0) AS cost,
                             COALESCE(SUM(CASE WHEN {_COST_ROWS} THEN l.tokens_input END), 0)
                               + COALESCE(SUM(CASE WHEN {_COST_ROWS} THEN l.tokens_output END), 0) AS tokens,
                             (julianday(MAX(l.created_at)) - julianday(MIN(l.created_at))) * 86400.0
                                 AS duration_s
                      FROM tree t LEFT JOIN logs l ON l.run_id = t.run_id
                      GROUP BY t.root
                    ), ranked AS (
                      SELECT r.run_id, r.status, r.user_instruction, r.created_at,
                             p.duration_s, p.cost, p.tokens
                      FROM runs r JOIN totals p ON p.run_id = r.run_id
                    )
                    SELECT * FROM ranked {where}
                    ORDER BY {order}
                    LIMIT ?""",
                (limit,),
            ) as cur:
                rows = await cur.fetchall()
        return JSONResponse(content={"sort": sort, "runs": [dict(r) for r in rows]})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-044", "message": f"Outlier runs failed: {exc}"},
        )


@router.get("/export", dependencies=[Depends(require_admin)])
async def export_logs(
    limit: int = Query(1000, ge=1, le=10000),
    run_id: str = Query(None),
) -> StreamingResponse:
    """Export log events as JSONL (one JSON object per line)."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            sql = "SELECT * FROM logs"
            params = []
            if run_id:
                sql += " WHERE run_id=?"
                params.append(run_id)
            sql += " ORDER BY created_at ASC LIMIT ?"
            params.append(limit)
            async with db.execute(sql, params) as cur:
                rows = await cur.fetchall()

        lines = [json.dumps(dict(r)) for r in rows]
        content = "\n".join(lines) + ("\n" if lines else "")

        return StreamingResponse(
            iter([content]),
            media_type="application/x-ndjson",
            headers={"Content-Disposition": "attachment; filename=borg_route_logs.jsonl"},
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-043", "message": f"Export failed: {exc}"},
        )
