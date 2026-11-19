"""connect(): an aiosqlite connection that waits for a lock instead of failing.

Many steps of one run write to the same SQLite file at once (the lane's
outbound ledger, the log writer, envelope saves). Without a busy timeout a
writer that finds the database locked fails at once with "database is
locked". Every connection opened here waits up to ``BUSY_TIMEOUT_MS`` for the
lock (``PRAGMA busy_timeout``, set through sqlite3's ``timeout``).
"""
from __future__ import annotations

from typing import Optional

import aiosqlite

BUSY_TIMEOUT_MS = 30000


def connect(db_path: str, timeout_s: Optional[float] = None) -> aiosqlite.Connection:
    """``aiosqlite.connect`` with ``PRAGMA busy_timeout`` = ``BUSY_TIMEOUT_MS``
    (or ``timeout_s`` seconds, for a caller with its own time budget, such as
    one try of a ledger write).

    Use it as ``async with connect(path) as db:``, like ``aiosqlite.connect``.
    """
    seconds = BUSY_TIMEOUT_MS / 1000 if timeout_s is None else timeout_s
    return aiosqlite.connect(db_path, timeout=seconds)
