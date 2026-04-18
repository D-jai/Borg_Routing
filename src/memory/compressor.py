"""Inactive-turn compressor for thread memory.

Fetches all messages older than the last `keep_last_n` turns from a thread
and calls the orchestrator model to produce a rolling summary string.
The summary is stored in the config table as `rolling_summary:{thread_id}`.

Error codes:
- BRT-MEM-003: model call failed during compression
- BRT-MEM-004: compression response parse failed (empty or malformed)
- BRT-MEM-005: config write failed after compression
"""
import time
from typing import Optional

import aiosqlite
import httpx

from src.db.schema import DB_PATH
from src.db.state import get_config, set_config
from src.logging.writer import write_event

_COMPRESSOR_TIMEOUT_SEC = 120.0

_COMPRESS_PROMPT = """You are summarising a conversation history to save context space.

Below are older turns from a conversation thread. Produce a concise rolling summary
(3-8 sentences) capturing: key facts established, decisions made, tasks completed,
and anything the orchestrator must remember for future turns.

Output only the summary text — no JSON, no headers, no commentary.

Thread turns to summarise:
{turns}
"""


async def compress_inactive_turns(
    thread_id: str,
    model_endpoint: str,
    model_name: str,
    keep_last_n: int = 5,
    db_path: str = DB_PATH,
) -> str:
    """Compress older thread turns into a rolling summary via model call.

    Fetches all messages older than the last `keep_last_n` turns, calls the
    model, stores the summary in config as `rolling_summary:{thread_id}`,
    and returns the summary string.

    If there are `keep_last_n` or fewer total messages, returns the existing
    stored summary (or empty string) without making a model call.

    Raises:
        ValueError: BRT-MEM-001 if thread not found.
        ValueError: BRT-MEM-003 if the model call fails.
        ValueError: BRT-MEM-004 if the response cannot be parsed or is empty.
        ValueError: BRT-MEM-005 if storing the summary fails.
    """
    async with aiosqlite.connect(db_path) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT thread_id FROM threads WHERE thread_id=?", (thread_id,)
        ) as cur:
            row = await cur.fetchone()
        if row is None:
            raise ValueError(f"BRT-MEM-001: thread '{thread_id}' not found")

        async with db.execute(
            """SELECT role, content FROM messages
               WHERE thread_id=?
               ORDER BY created_at ASC""",
            (thread_id,),
        ) as cur:
            all_messages = [
                {"role": r["role"], "content": r["content"]}
                for r in await cur.fetchall()
            ]

    if len(all_messages) <= keep_last_n:
        existing: Optional[str] = await get_config(
            f"rolling_summary:{thread_id}", db_path=db_path
        )
        return existing or ""

    inactive = all_messages[: len(all_messages) - keep_last_n]
    turns_text = "\n".join(
        f"[{m['role'].upper()}]: {m['content']}" for m in inactive
    )
    prompt = _COMPRESS_PROMPT.replace("{turns}", turns_text)

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_COMPRESSOR_TIMEOUT_SEC) as client:
            resp = await client.post(
                f"{model_endpoint}/v1/chat/completions",
                json={
                    "model": model_name,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": 0.2,
                },
            )
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ValueError(
            f"BRT-MEM-003: model call timed out during compression ({model_endpoint})"
        ) from exc
    except (httpx.HTTPStatusError, httpx.RequestError) as exc:
        raise ValueError(
            f"BRT-MEM-003: model call failed during compression: {exc}"
        ) from exc

    latency_ms = int((time.monotonic() - t0) * 1000)

    try:
        body = resp.json()
        summary = body["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        raise ValueError(
            f"BRT-MEM-004: could not parse compression response: {exc}"
        ) from exc

    if not summary:
        raise ValueError("BRT-MEM-004: compression model returned empty summary")

    config_key = f"rolling_summary:{thread_id}"
    try:
        await set_config(config_key, summary, db_path=db_path)
    except Exception as exc:
        raise ValueError(
            f"BRT-MEM-005: failed to store rolling summary for thread '{thread_id}': {exc}"
        ) from exc

    usage = body.get("usage", {})
    await write_event(
        "model_call",
        model_id=model_name,
        tokens_input=usage.get("prompt_tokens"),
        tokens_output=usage.get("completion_tokens"),
        latency_ms=latency_ms,
        payload_in=prompt[:2000],
        payload_out=summary[:2000],
        db_path=db_path,
    )

    return summary
