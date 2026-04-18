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
    db_path: str = DB_PATH,
) -> UUID:
    """Insert a new run record with status=pending. Returns the new run_id."""
    run_id = uuid4()
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            """INSERT INTO runs(run_id, parent_run_id, status, user_instruction)
               VALUES (?, ?, 'pending', ?)""",
            (
                str(run_id),
                str(parent_run_id) if parent_run_id else None,
                user_instruction,
            ),
        )
        await db.commit()
    return run_id


async def update_run_status(
    run_id: UUID,
    status: str,
    db_path: str = DB_PATH,
) -> None:
    """Update run status. Valid values: pending|running|completed|failed.

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
