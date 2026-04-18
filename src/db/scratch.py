"""Async helpers for the scratch table — ephemeral child working storage.

The scratch table is the foundation of Law 2 (No Forgotten Scratch).
Every write is tagged with (run_id, level_path, envelope_id) so the parent
can verify and clean up child residue at fold-in time.

Error codes:
- BRT-DB-015: scratch write failed
- BRT-DB-016: scratch read failed
- BRT-DB-017: scratch delete failed
"""
import json
from typing import Optional
from uuid import UUID

import aiosqlite

from src.db.schema import DB_PATH
from src.envelopes.schema import SideEffectAction


async def write_scratch(
    run_id: UUID,
    level_path: str,
    envelope_id: UUID,
    table_name: str,
    data: dict,
    db_path: str = DB_PATH,
) -> int:
    """Insert one scratch row. Returns the new row id.

    Raises:
        RuntimeError: BRT-DB-015 if the insert fails.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                """INSERT INTO scratch(run_id, level_path, envelope_id, table_name, data)
                   VALUES (?,?,?,?,?)""",
                (str(run_id), level_path, str(envelope_id), table_name, json.dumps(data)),
            )
            await db.commit()
            return cur.lastrowid
    except Exception as exc:
        raise RuntimeError(f"BRT-DB-015: scratch write failed: {exc}") from exc


async def read_scratch_for_level(
    run_id: UUID,
    level_path: str,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return all scratch rows for a given (run_id, level_path).

    Raises:
        RuntimeError: BRT-DB-016 if the read fails.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute(
                "SELECT * FROM scratch WHERE run_id=? AND level_path=? ORDER BY id",
                (str(run_id), level_path),
            ) as cur:
                rows = await cur.fetchall()
        result = []
        for r in rows:
            row_dict = dict(r)
            try:
                row_dict["data"] = json.loads(row_dict["data"])
            except (json.JSONDecodeError, TypeError):
                pass
            result.append(row_dict)
        return result
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"BRT-DB-016: scratch read failed: {exc}") from exc


async def delete_scratch_for_level(
    run_id: UUID,
    level_path: str,
    db_path: str = DB_PATH,
) -> int:
    """Delete all scratch rows for a given (run_id, level_path). Returns row count deleted.

    Raises:
        RuntimeError: BRT-DB-017 if the delete fails.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            cur = await db.execute(
                "DELETE FROM scratch WHERE run_id=? AND level_path=?",
                (str(run_id), level_path),
            )
            await db.commit()
            return cur.rowcount
    except Exception as exc:
        raise RuntimeError(f"BRT-DB-017: scratch delete failed: {exc}") from exc


async def count_scratch_for_level(
    run_id: UUID,
    level_path: str,
    db_path: str = DB_PATH,
) -> int:
    """Return the count of scratch rows for a given (run_id, level_path)."""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(
            "SELECT COUNT(*) FROM scratch WHERE run_id=? AND level_path=?",
            (str(run_id), level_path),
        ) as cur:
            row = await cur.fetchone()
    return row[0]


async def apply_cleanup_directives(
    run_id: UUID,
    directives: list[SideEffectAction],
    db_path: str = DB_PATH,
) -> None:
    """Execute a list of SideEffectAction cleanup directives against the scratch table.

    Supports:
    - action='delete' with ids=[...]: deletes scratch rows by id
    - action='delete' with row_id=...: deletes a single scratch row by id
    - action='write': no-op at cleanup time (writes are handled at execution time)
    """
    if not directives:
        return
    async with aiosqlite.connect(db_path) as db:
        for directive in directives:
            if directive.action != "delete":
                continue
            if directive.ids:
                placeholders = ",".join("?" * len(directive.ids))
                await db.execute(
                    f"DELETE FROM scratch WHERE run_id=? AND id IN ({placeholders})",
                    [str(run_id)] + directive.ids,
                )
            elif directive.row_id:
                await db.execute(
                    "DELETE FROM scratch WHERE run_id=? AND id=?",
                    (str(run_id), directive.row_id),
                )
        await db.commit()
