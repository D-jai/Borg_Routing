"""Async helpers for runs, subtasks, envelopes, and config tables.

Error codes:
- BRT-DB-010: run not found
- BRT-DB-011: envelope not found
- BRT-DB-012: invalid run status transition
- BRT-DB-013: duplicate run_id
- BRT-DB-014: config key not found
"""
import json
from typing import Optional
from uuid import UUID, uuid4

import aiosqlite

from src.db.schema import DB_PATH
from src.envelopes.schema import InboundEnvelope, WrappedTask


async def create_run(
    user_instruction: str,
    parent_run_id: Optional[UUID] = None,
    thread_id: Optional[str] = None,
    db_path: str = DB_PATH,
) -> UUID:
    """Insert a new run record with status=pending. Returns the new run_id."""
    run_id = uuid4()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO runs(run_id, parent_run_id, thread_id, status, user_instruction)
               VALUES (?, ?, ?, 'pending', ?)""",
            (
                str(run_id),
                str(parent_run_id) if parent_run_id else None,
                thread_id,
                user_instruction,
            ),
        )
        await db.commit()
    return run_id


async def update_run_status(
    run_id: UUID,
    status: str,
    failure_reason: Optional[str] = None,
    db_path: str = DB_PATH,
) -> None:
    """Update run status. Valid values: pending|running|completed|failed.

    When ``failure_reason`` is provided (typically with status='failed'), it
    is persisted to the runs.failure_reason column so ``GET /runs/{id}``
    and the UI can surface why the run failed. Passing ``failure_reason``
    with a non-failed status is allowed but unusual — no filtering applied.

    Raises:
        ValueError: BRT-DB-010 if run_id not found.
        ValueError: BRT-DB-012 if status is invalid.
    """
    valid = {"pending", "running", "completed", "failed"}
    if status not in valid:
        raise ValueError(f"BRT-DB-012: invalid run status '{status}'. Must be one of {sorted(valid)}")

    async with aiosqlite.connect(db_path) as db:
        sql = "UPDATE runs SET status=?"
        params: list = [status]
        if status in ("completed", "failed"):
            sql += ", completed_at=CURRENT_TIMESTAMP"
        if failure_reason is not None:
            sql += ", failure_reason=?"
            params.append(failure_reason)
        sql += " WHERE run_id=?"
        params.append(str(run_id))
        cur = await db.execute(sql, params)
        if cur.rowcount == 0:
            raise ValueError(f"BRT-DB-010: run_id '{run_id}' not found")
        await db.commit()


async def save_envelope(task: WrappedTask, db_path: str = DB_PATH) -> None:
    """Insert an outbound envelope record with status=in_flight."""
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO envelopes(
                envelope_id, run_id, subtask_id, type, level_path,
                model_id, trigger_reason, skill_id, skill_version,
                status, outbound_payload
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(task.envelope_id),
                str(task.run_id),
                str(task.subtask_id),
                task.type,
                task.level_path,
                task.model_name,
                task.trigger_reason,
                task.skill_id,
                task.skill_version,
                "in_flight",
                task.model_dump_json(),
            ),
        )
        await db.commit()


async def update_envelope_inbound(
    envelope_id: UUID,
    env: InboundEnvelope,
    tokens_input: Optional[int] = None,
    tokens_output: Optional[int] = None,
    dollars: Optional[float] = None,
    latency_ms: Optional[int] = None,
    db_path: str = DB_PATH,
) -> None:
    """Store the inbound envelope payload and mark the envelope completed."""
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            """UPDATE envelopes SET
                status=?,
                inbound_payload=?,
                tokens_input=?,
                tokens_output=?,
                dollars=?,
                latency_ms=?,
                completed_at=CURRENT_TIMESTAMP
               WHERE envelope_id=?""",
            (
                env.status,
                env.model_dump_json(),
                tokens_input,
                tokens_output,
                dollars,
                latency_ms,
                str(envelope_id),
            ),
        )
        if cur.rowcount == 0:
            raise ValueError(f"BRT-DB-011: envelope_id '{envelope_id}' not found")
        await db.commit()


async def get_run(run_id: UUID, db_path: str = DB_PATH) -> dict:
    """Return the run record as a dict.

    Raises:
        ValueError: BRT-DB-010 if not found.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM runs WHERE run_id=?", (str(run_id),)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        raise ValueError(f"BRT-DB-010: run_id '{run_id}' not found")
    return dict(row)


async def get_runs_list(limit: int = 50, db_path: str = DB_PATH) -> list[dict]:
    """Return the most recent runs ordered by created_at descending."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_envelopes(run_id: UUID, db_path: str = DB_PATH) -> list[dict]:
    """Return all envelope records for a run, ordered by created_at."""
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM envelopes WHERE run_id=? ORDER BY created_at",
            (str(run_id),),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def resume_run(run_id: UUID, db_path: str = DB_PATH) -> list:
    """Return WrappedTask objects for envelopes in failed or in_flight status.

    Used by POST /runs/{id}/resume to re-dispatch incomplete work.

    Raises:
        ValueError: BRT-DB-020 if run not found or already completed.
    """
    run = await get_run(run_id, db_path=db_path)
    if run["status"] == "completed":
        raise ValueError(
            f"BRT-DB-020: run '{run_id}' is already completed and cannot be resumed"
        )

    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT outbound_payload FROM envelopes
               WHERE run_id=? AND status IN ('failed','in_flight')
               ORDER BY created_at""",
            (str(run_id),),
        ) as cur:
            rows = await cur.fetchall()

    tasks = []
    for row in rows:
        try:
            payload = json.loads(row["outbound_payload"])
            from src.envelopes.schema import WrappedTask
            tasks.append(WrappedTask(**payload))
        except Exception:
            pass
    return tasks


async def compute_and_save_digest(run_id: UUID, db_path: str = DB_PATH) -> dict:
    """Aggregate envelope cost/tokens for a run and persist to run_digests.

    Returns the digest dict: {run_id, summary, cost_dollars, token_count}.
    Safe to call multiple times — upserts on conflict.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT COUNT(*) as cnt,
                      COALESCE(SUM(tokens_input), 0) + COALESCE(SUM(tokens_output), 0) AS total_tokens,
                      COALESCE(SUM(dollars), 0) AS total_cost
               FROM envelopes WHERE run_id=?""",
            (str(run_id),),
        ) as cur:
            row = await cur.fetchone()

    envelope_count = row["cnt"] if row else 0
    token_count = int(row["total_tokens"]) if row else 0
    cost_dollars = float(row["total_cost"]) if row else 0.0

    summary = (
        f"{envelope_count} envelope(s) dispatched; "
        f"{token_count:,} tokens; "
        f"${cost_dollars:.4f} cost."
    )

    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO run_digests(run_id, summary, cost_dollars, token_count)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 summary=excluded.summary,
                 cost_dollars=excluded.cost_dollars,
                 token_count=excluded.token_count""",
            (str(run_id), summary, cost_dollars, token_count),
        )
        await db.commit()

    return {
        "run_id": str(run_id),
        "summary": summary,
        "cost_dollars": cost_dollars,
        "token_count": token_count,
    }


async def set_run_rating(run_id: UUID, rating: int, db_path: str = DB_PATH) -> None:
    """Set user_rating (1 = thumbs up, -1 = thumbs down) on a run.

    Raises:
        ValueError: if rating is not 1 or -1.
        ValueError: BRT-DB-010 if run_id not found.
    """
    if rating not in (1, -1):
        raise ValueError(f"Invalid rating '{rating}': must be 1 (up) or -1 (down)")
    async with aiosqlite.connect(db_path) as db:
        cur = await db.execute(
            "UPDATE runs SET user_rating=? WHERE run_id=?",
            (rating, str(run_id)),
        )
        if cur.rowcount == 0:
            raise ValueError(f"BRT-DB-010: run_id '{run_id}' not found")
        await db.commit()


async def get_config(key: str, db_path: str = DB_PATH) -> Optional[str]:
    """Return config value for key, or None if not set."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT value FROM config WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        return None
    value = row[0]
    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return value


async def set_config(key: str, value, db_path: str = DB_PATH) -> None:
    """Upsert a config value. Value is JSON-serialised if not a string."""
    stored = json.dumps(value) if not isinstance(value, str) else value
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO config(key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (key, stored),
        )
        await db.commit()
