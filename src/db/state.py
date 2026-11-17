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

from src.db.connect import connect
from src.db.schema import DB_PATH
from src.envelopes.schema import InboundEnvelope, WrappedTask


async def create_run(
    user_instruction: str,
    parent_run_id: Optional[UUID] = None,
    thread_id: Optional[str] = None,
    db_path: str = DB_PATH,
    run_id: Optional[UUID] = None,
    kind: str = "orchestrated",
    route_options: Optional[dict] = None,
    documents: Optional[list] = None,
    lane_mode: Optional[str] = None,
) -> UUID:
    """Insert a new run record with status=pending. Returns the new run_id.

    ``run_id`` is for a caller that already issued the id (a bypass request);
    omit it to get a fresh one. ``documents`` is the manifest of a run that
    reads documents (on-premise lane); such a run is never resumed.
    ``lane_mode`` is the lane mode the run runs under (``off`` when no lane);
    None leaves it unrecorded.

    BRT-LOG-DB-COMPAT — also stamps instance_id so The_Borg_DB can tell
    apart runs from different devices when aggregated via hive-collective.
    """
    # Local import keeps this module side-effect free for tests that
    # monkey-patch the provenance singletons.
    from src.logging.provenance import resolve_instance_id  # noqa: PLC0415

    run_id = run_id or uuid4()
    async with connect(db_path) as db:
        if documents:
            await db.execute(
                """INSERT INTO runs(
                    run_id, parent_run_id, thread_id, status, user_instruction, instance_id,
                    kind, route_options, documents
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?, ?)""",
                (
                    str(run_id),
                    str(parent_run_id) if parent_run_id else None,
                    thread_id,
                    user_instruction,
                    resolve_instance_id(),
                    kind,
                    json.dumps(route_options) if route_options else None,
                    json.dumps(documents),
                ),
            )
        else:
            await db.execute(
                """INSERT INTO runs(
                    run_id, parent_run_id, thread_id, status, user_instruction, instance_id,
                    kind, route_options
                ) VALUES (?, ?, ?, 'pending', ?, ?, ?, ?)""",
                (
                    str(run_id),
                    str(parent_run_id) if parent_run_id else None,
                    thread_id,
                    user_instruction,
                    resolve_instance_id(),
                    kind,
                    json.dumps(route_options) if route_options else None,
                ),
            )
        if lane_mode is not None:
            await db.execute(
                "UPDATE runs SET lane_mode=? WHERE run_id=?", (lane_mode, str(run_id))
            )
        await db.commit()
    return run_id


def lane_mode_of(lane) -> str:
    """The mode string recorded on a run: the lane's mode, ``off`` with no lane."""
    return lane.settings.mode if lane is not None else "off"


async def set_run_lane_mode(run_id: UUID, lane_mode: str, db_path: str = DB_PATH) -> None:
    """Record the lane mode a run (re)starts under."""
    async with connect(db_path) as db:
        await db.execute("UPDATE runs SET lane_mode=? WHERE run_id=?", (lane_mode, str(run_id)))
        await db.commit()


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

    async with connect(db_path) as db:
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


ANSWER_KINDS = ("composed", "salvaged")


async def set_run_answer_kind(run_id: UUID, kind: str, db_path: str = DB_PATH) -> None:
    """Record how the run's final answer was made (planner ruling 20, R2).

    ``composed``: the compose call wrote it. ``salvaged``: compose failed
    twice and the answer is the finished steps' results with a note
    (BRT-ORC-030). NULL (never set): a direct answer, a failed run, or a run
    from before this column. Shown in ``GET /runs/{id}`` as ``run.answer_kind``.
    A run id with no row is ignored.

    Raises:
        ValueError: BRT-DB-012 if ``kind`` is not one of ANSWER_KINDS.
    """
    if kind not in ANSWER_KINDS:
        raise ValueError(f"BRT-DB-012: invalid answer_kind '{kind}'. Must be one of {list(ANSWER_KINDS)}")
    async with connect(db_path) as db:
        await db.execute("UPDATE runs SET answer_kind=? WHERE run_id=?", (kind, str(run_id)))
        await db.commit()


async def save_envelope(task: WrappedTask, db_path: str = DB_PATH) -> None:
    """Insert an outbound envelope record with status=in_flight."""
    async with connect(db_path) as db:
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
    baseline_dollars: Optional[float] = None,
) -> None:
    """Store the inbound envelope payload and mark the envelope completed."""
    async with connect(db_path) as db:
        cur = await db.execute(
            """UPDATE envelopes SET
                status=?,
                inbound_payload=?,
                tokens_input=?,
                tokens_output=?,
                dollars=?,
                latency_ms=?,
                baseline_dollars=?,
                completed_at=CURRENT_TIMESTAMP
               WHERE envelope_id=?""",
            (
                env.status,
                env.model_dump_json(),
                tokens_input,
                tokens_output,
                dollars,
                latency_ms,
                baseline_dollars,
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
    async with connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM runs WHERE run_id=?", (str(run_id),)
        ) as cur:
            row = await cur.fetchone()
    if row is None:
        raise ValueError(f"BRT-DB-010: run_id '{run_id}' not found")
    return dict(row)


async def get_runs_list(
    limit: int = 50, db_path: str = DB_PATH, include_bypass: bool = False
) -> list[dict]:
    """Return the most recent runs ordered by created_at descending.

    Direct-to-model (bypass) runs are left out unless ``include_bypass``.
    """
    where = "" if include_bypass else "WHERE COALESCE(kind, 'orchestrated') != 'bypass' "
    async with connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT * FROM runs {where}ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_run_counts(db_path: str = DB_PATH, include_bypass: bool = False) -> dict:
    """True totals over every run (not a page): total, per status, thumbs.

    Direct-to-model (bypass) runs are left out unless ``include_bypass``.
    """
    where = "" if include_bypass else "WHERE COALESCE(kind, 'orchestrated') != 'bypass' "
    counts = {"total": 0, "completed": 0, "failed": 0, "running": 0, "pending": 0,
              "thumbs_up": 0, "thumbs_down": 0}
    async with connect(db_path) as db:
        async with db.execute(
            f"SELECT status, COUNT(*), "
            f"SUM(CASE WHEN user_rating = 1 THEN 1 ELSE 0 END), "
            f"SUM(CASE WHEN user_rating = -1 THEN 1 ELSE 0 END) "
            f"FROM runs {where}GROUP BY status"
        ) as cur:
            rows = await cur.fetchall()
    for status, n, up, down in rows:
        counts["total"] += n
        counts[status] = counts.get(status, 0) + n
        counts["thumbs_up"] += up or 0
        counts["thumbs_down"] += down or 0
    return counts


async def get_envelopes(run_id: UUID, db_path: str = DB_PATH) -> list[dict]:
    """Return all envelope records for a run, ordered by created_at."""
    async with connect(db_path) as db:
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

    async with connect(db_path) as db:
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


async def _sum_run_tree(db, run_id: UUID):
    """Sum logged model_call / orchestrator_decision rows for a run and every
    descendant run (child runs spawned by orchestrate nodes, via parent_run_id)."""
    async with db.execute(
        """WITH RECURSIVE tree(run_id) AS (
               SELECT ?
               UNION
               SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id
           )
           SELECT COALESCE(SUM(tokens_input), 0) AS tin,
                  COALESCE(SUM(tokens_output), 0) AS tout,
                  COALESCE(SUM(dollars), 0) AS total_cost,
                  COALESCE(SUM(baseline_dollars), 0) AS total_baseline
           FROM logs
           WHERE run_id IN (SELECT run_id FROM tree)
             AND event_type IN ('model_call','orchestrator_decision')""",
        (str(run_id),),
    ) as cur:
        return await cur.fetchone()


async def get_descendant_run_ids(run_id: UUID, db_path: str = DB_PATH) -> list[str]:
    """Run ids of every descendant of a run (child runs spawned by orchestrate
    nodes, via parent_run_id), oldest first. The run itself is not included."""
    async with connect(db_path) as db:
        async with db.execute(
            """WITH RECURSIVE tree(run_id) AS (
                   SELECT ?
                   UNION
                   SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id
               )
               SELECT run_id FROM runs
               WHERE run_id IN (SELECT run_id FROM tree) AND run_id != ?
               ORDER BY created_at""",
            (str(run_id), str(run_id)),
        ) as cur:
            rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_outbound_requests(
    run_id, db_path: str = DB_PATH, left_machine_only: bool = True
) -> list[dict]:
    """On-premise lane: "what left this machine" for a run and every
    descendant run (via parent_run_id), oldest first. Each row holds the exact
    body that was sent. ``left_machine_only=False`` adds the local calls."""
    where = "AND left_machine = 1" if left_machine_only else ""
    async with connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"""WITH RECURSIVE tree(run_id) AS (
                   SELECT ?
                   UNION
                   SELECT r.run_id FROM runs r JOIN tree t ON r.parent_run_id = t.run_id
               )
               SELECT * FROM outbound_requests
               WHERE run_id IN (SELECT run_id FROM tree) {where}
               ORDER BY started_at, rowid""",
            (str(run_id),),
        ) as cur:
            rows = await cur.fetchall()
    return [dict(r) for r in rows]


async def get_run_documents(run_id, db_path: str = DB_PATH) -> Optional[list]:
    """The document manifest of a run or of any run above it, else None.
    A run that read documents is never resumed (on-premise lane)."""
    current = str(run_id)
    for _ in range(33):
        async with connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT * FROM runs WHERE run_id=?", (current,)) as cur:
                row = await cur.fetchone()
        if row is None:
            return None
        row = dict(row)
        if row.get("documents"):
            try:
                return json.loads(row["documents"])
            except (TypeError, ValueError):
                return [{"status": "unreadable manifest"}]
        if not row.get("parent_run_id"):
            return None
        current = row["parent_run_id"]
    return None


async def sum_run_tree(run_id: UUID, db_path: str = DB_PATH) -> dict:
    """Logged tokens and dollars for a run plus all its descendant runs."""
    async with connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        row = await _sum_run_tree(db, run_id)
    return {
        "tokens_input": int(row["tin"]),
        "tokens_output": int(row["tout"]),
        "dollars": float(row["total_cost"]),
        "baseline_dollars": float(row["total_baseline"]),
    }


async def get_run_digest(run_id: UUID, db_path: str = DB_PATH) -> Optional[dict]:
    """The stored digest row for a run, or None if no digest was saved."""
    async with connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT summary, cost_dollars, baseline_dollars, tokens_input,
                      tokens_output, token_count
               FROM run_digests WHERE run_id=?""",
            (str(run_id),),
        ) as cur:
            row = await cur.fetchone()
    return dict(row) if row else None


async def compute_and_save_digest(run_id: UUID, db_path: str = DB_PATH) -> dict:
    """Aggregate cost/tokens for a run from its logged calls and persist to run_digests.

    Totals are the sums over the run's logged ``model_call`` and
    ``orchestrator_decision`` rows, so the digest equals what the log shows.
    The envelope count still comes from the envelopes table.

    Returns the digest dict: {run_id, summary, cost_dollars, token_count,
    tokens_input, tokens_output, baseline_dollars}.
    Safe to call multiple times — upserts on conflict.
    """
    async with connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT COUNT(*) AS cnt FROM envelopes WHERE run_id=?",
            (str(run_id),),
        ) as cur:
            env_row = await cur.fetchone()
        row = await _sum_run_tree(db, run_id)

    envelope_count = env_row["cnt"] if env_row else 0
    tokens_in_total = int(row["tin"]) if row else 0
    tokens_out_total = int(row["tout"]) if row else 0
    token_count = tokens_in_total + tokens_out_total
    cost_dollars = float(row["total_cost"]) if row else 0.0
    baseline_total = float(row["total_baseline"]) if row else 0.0

    summary = (
        f"{envelope_count} envelope(s) dispatched; "
        f"{token_count:,} tokens; "
        f"${cost_dollars:.4f} cost."
    )

    async with connect(db_path) as db:
        await db.execute(
            """INSERT INTO run_digests(run_id, summary, cost_dollars, token_count,
                                       baseline_dollars, tokens_input, tokens_output)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(run_id) DO UPDATE SET
                 summary=excluded.summary,
                 cost_dollars=excluded.cost_dollars,
                 token_count=excluded.token_count,
                 baseline_dollars=excluded.baseline_dollars,
                 tokens_input=excluded.tokens_input,
                 tokens_output=excluded.tokens_output""",
            (str(run_id), summary, cost_dollars, token_count,
             baseline_total, tokens_in_total, tokens_out_total),
        )
        await db.commit()

    return {
        "run_id": str(run_id),
        "summary": summary,
        "cost_dollars": cost_dollars,
        "token_count": token_count,
        "tokens_input": tokens_in_total,
        "tokens_output": tokens_out_total,
        "baseline_dollars": baseline_total,
    }


async def set_run_rating(run_id: UUID, rating: int, db_path: str = DB_PATH) -> None:
    """Set user_rating (1 = thumbs up, -1 = thumbs down) on a run.

    Raises:
        ValueError: if rating is not 1 or -1.
        ValueError: BRT-DB-010 if run_id not found.
    """
    if rating not in (1, -1):
        raise ValueError(f"Invalid rating '{rating}': must be 1 (up) or -1 (down)")
    async with connect(db_path) as db:
        cur = await db.execute(
            "UPDATE runs SET user_rating=? WHERE run_id=?",
            (rating, str(run_id)),
        )
        if cur.rowcount == 0:
            raise ValueError(f"BRT-DB-010: run_id '{run_id}' not found")
        await db.commit()


async def get_config(key: str, db_path: str = DB_PATH) -> Optional[str]:
    """Return config value for key, or None if not set."""
    async with connect(db_path) as db:
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


async def delete_config(key: str, db_path: str = DB_PATH) -> bool:
    """Delete a saved config key so env/default applies again. True if it existed."""
    async with connect(db_path) as db:
        cur = await db.execute("DELETE FROM config WHERE key=?", (key,))
        await db.commit()
        return cur.rowcount > 0


async def set_config(key: str, value, db_path: str = DB_PATH) -> None:
    """Upsert a config value. Value is JSON-serialised if not a string."""
    stored = json.dumps(value) if not isinstance(value, str) else value
    async with connect(db_path) as db:
        await db.execute(
            """INSERT INTO config(key, value) VALUES (?,?)
               ON CONFLICT(key) DO UPDATE SET value=excluded.value,
               updated_at=CURRENT_TIMESTAMP""",
            (key, stored),
        )
        await db.commit()
