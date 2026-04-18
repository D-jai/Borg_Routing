"""Thread-scoped context window extractor.

Fetches the last N message turns from a thread, suitable for passing as
`thread_window` to orchestrate_run() / run_pipeline().

Error codes:
- BRT-MEM-001: thread not found
- BRT-MEM-002: window extraction failed (DB error)
"""
from typing import Optional
from uuid import uuid4

import aiosqlite

from src.db.schema import DB_PATH


async def create_thread(db_path: str = DB_PATH) -> str:
    """Create a new thread record. Returns thread_id."""
    thread_id = str(uuid4())
    async with aiosqlite.connect(db_path) as db:
        await db.execute(
            "INSERT INTO threads(thread_id) VALUES (?)",
            (thread_id,),
        )
        await db.commit()
    return thread_id


async def add_message(
    thread_id: str,
    role: str,
    content: str,
    run_id: Optional[str] = None,
    db_path: str = DB_PATH,
) -> str:
    """Append a message to a thread. Returns message_id.

    Raises:
        ValueError: BRT-MEM-001 if thread not found.
    """
    message_id = str(uuid4())
    async with aiosqlite.connect(db_path) as db:
        # Verify thread exists
        async with db.execute(
            "SELECT thread_id FROM threads WHERE thread_id=?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise ValueError(f"BRT-MEM-001: thread '{thread_id}' not found")
        await db.execute(
            """INSERT INTO messages(message_id, thread_id, run_id, role, content)
               VALUES (?,?,?,?,?)""",
            (message_id, thread_id, run_id, role, content),
        )
        await db.commit()
    return message_id


async def extract_window(
    thread_id: str,
    max_turns: int = 10,
    db_path: str = DB_PATH,
) -> list[dict]:
    """Return the last `max_turns` messages from a thread, oldest first.

    Each item: {"role": str, "content": str}

    Raises:
        ValueError: BRT-MEM-001 if thread not found.
        ValueError: BRT-MEM-002 on DB error.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            # Thread existence check
            async with db.execute(
                "SELECT thread_id FROM threads WHERE thread_id=?", (thread_id,)
            ) as cur:
                row = await cur.fetchone()
            if row is None:
                raise ValueError(f"BRT-MEM-001: thread '{thread_id}' not found")

            # Fetch last N messages, then reverse to chronological order
            async with db.execute(
                """SELECT role, content FROM messages
                   WHERE thread_id=?
                   ORDER BY created_at DESC
                   LIMIT ?""",
                (thread_id, max_turns),
            ) as cur:
                rows = await cur.fetchall()
        return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"BRT-MEM-002: window extraction failed: {exc}") from exc
