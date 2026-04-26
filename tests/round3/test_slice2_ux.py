"""Round-3 Slice 2 — UX fixes.

BUG-021 — chat.html includes marked + DOMPurify and a safe renderMd helper
BUG-018 — _poll_stage_events surfaces orchestrator_decision rows as stage events
BUG-019 — chat.html persists the thread to localStorage and reloads on page load

The HTML-side BUG-021 / BUG-019 changes are exercised via a shape-check
on the file (the JS doesn't have a test runner here — but we can grep
the contract). The server-side BUG-018 polling helper has a real
end-to-end async test.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from uuid import uuid4

import pytest

from src.api.chat_completions import _poll_stage_events
from src.db.schema import create_all
from src.db.state import create_run
from src.logging.writer import write_event


# ============================================================
# BUG-021 — chat.html shape contract
# ============================================================

def test_chat_html_contains_markdown_renderer():
    """TC-BUG-021 — marked + DOMPurify CDN + renderMd helper present."""
    chat = Path("ui/chat.html").read_text(encoding="utf-8")
    assert "marked.min.js" in chat, "marked CDN script missing"
    assert "purify.min.js" in chat, "DOMPurify CDN script missing"
    assert "function renderMd(" in chat, "renderMd helper missing"
    assert "DOMPurify.sanitize" in chat, "renderMd doesn't sanitize"
    # User bubbles must stay literal — operator's pasted markdown shouldn't
    # become headings before they hit Send.
    assert "div.textContent = content" in chat, (
        "user-bubble path must stay textContent (BUG-021 verified_fix)"
    )


def test_chat_html_renders_assistant_with_innerhtml():
    """TC-BUG-021-B — assistant bubble path uses renderMd→innerHTML."""
    chat = Path("ui/chat.html").read_text(encoding="utf-8")
    # The streaming update site and the final assignment both use renderMd.
    assert "thinkingDiv.innerHTML = renderMd(fullText)" in chat


# ============================================================
# BUG-019 — localStorage persistence contract
# ============================================================

def test_chat_html_persists_thread():
    """TC-BUG-019 — chat.html persists `thread` and `renderedTurns` to
    localStorage, restores on load, and exposes a Clear button."""
    chat = Path("ui/chat.html").read_text(encoding="utf-8")
    assert "localStorage.setItem(THREAD_KEY" in chat
    assert "localStorage.getItem(THREAD_KEY" in chat
    assert "function persistThread" in chat
    assert "function loadPersistedThread" in chat
    assert "loadPersistedThread()" in chat, "load helper must run on page load"
    assert "id=\"clear-btn\"" in chat, "Clear button must be present"


# ============================================================
# BUG-018 — stage indicator + poll helper
# ============================================================

def test_chat_html_has_stage_indicator():
    """TC-BUG-018-A — chat.html attaches a stage indicator with a
    wall-clock counter and recognizes `chunk.event === 'stage'` payloads."""
    chat = Path("ui/chat.html").read_text(encoding="utf-8")
    assert "function attachStageIndicator" in chat
    assert "chunk.event === 'stage'" in chat
    # Wall-clock counter must tick (setInterval driving a clock element).
    assert "setInterval" in chat
    assert "clock" in chat


@pytest.mark.asyncio
async def test_poll_stage_events_returns_recent_orchestrator_decisions(tmp_path):
    """TC-BUG-018-B — _poll_stage_events surfaces orchestrator_decision
    rows from the logs table, formatted as {stage, detail} dicts, and
    advances the last_seen_log_id cursor so subsequent polls don't
    re-emit the same events."""
    import asyncio
    db = str(tmp_path / "bug018.db")
    await create_all(db)

    run_id = await create_run("test prompt", db_path=db)
    started_at = time.time()

    # Emit two orchestrator_decision events.
    await write_event(
        "orchestrator_decision",
        run_id=run_id,
        decision={"mode": "plan", "msg": "decomposing"},
        db_path=db,
    )
    await write_event(
        "orchestrator_decision",
        run_id=run_id,
        decision={"mode": "compose", "msg": "composing final answer"},
        db_path=db,
    )
    # write_event is fire-and-forget — let the tasks actually run.
    await asyncio.sleep(0.15)

    events, new_cursor = await _poll_stage_events(
        started_at=started_at,
        last_seen_log_id=0,
        db_path=db,
    )
    assert len(events) >= 2
    stages = [e["stage"] for e in events]
    assert "plan" in stages
    assert "compose" in stages
    assert new_cursor > 0

    # Second poll with the advanced cursor should be empty.
    events2, _ = await _poll_stage_events(
        started_at=started_at,
        last_seen_log_id=new_cursor,
        db_path=db,
    )
    assert events2 == []


@pytest.mark.asyncio
async def test_poll_stage_events_is_failure_tolerant(tmp_path):
    """TC-BUG-018-C — when the DB doesn't exist or the schema is
    missing, _poll_stage_events returns ([], cursor) rather than
    raising. SSE liveness must never depend on the logs table being
    queryable."""
    bogus = str(tmp_path / "does-not-exist.db")
    events, cursor = await _poll_stage_events(
        started_at=time.time(),
        last_seen_log_id=0,
        db_path=bogus,
    )
    assert events == []
    assert cursor == 0
