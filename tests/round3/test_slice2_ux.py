"""Round-3 Slice 2 — UX fixes.

- the Ask page (ui/ask.html, which replaced chat.html in 4.2) renders
  answers through Borg.markdown (marked + DOMPurify)
- _poll_stage_events surfaces orchestrator_decision rows as stage events
- the Ask page persists the thread to localStorage and reloads on page load

The HTML-side changes are exercised via a shape-check
on the file (the JS doesn't have a test runner here, but we can grep
the contract). The server-side polling helper has a real
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
# The Ask page contract
# (ui/chat.html was archived by work order 4.2; ui/ask.html replaces it)
# ============================================================

ASK = Path("ui/ask.html")
ASK_JS = Path("ui/assets/pages/ask.js")  # the page script lives beside the page (CSP: no inline script)


def _ask_page() -> str:
    return ASK.read_text(encoding="utf-8") + "\n" + ASK_JS.read_text(encoding="utf-8")


def test_ask_html_renders_markdown_only_through_the_sanitiser():
    """vendored marked + DOMPurify, answers through Borg.markdown,
    user bubbles stay literal text."""
    page = _ask_page()
    assert 'src="assets/vendor/marked.min.js"' in page
    assert 'src="assets/vendor/purify.min.js"' in page
    assert "B.markdown(" in page
    # Every innerHTML assignment takes Borg.markdown output, nothing else.
    for line in page.splitlines():
        if ".innerHTML" in line:
            assert "= B.markdown(" in line, line
    # User bubbles: built with text, never markdown.
    assert "h('div', { class: 'bubble' }, text)" in page
    app = Path("ui/assets/app.js").read_text(encoding="utf-8")
    assert "DOMPurify.sanitize" in app


def test_ask_html_persists_history_in_local_storage_with_the_lane_tag():
    """the thread survives a reload; each turn carries its lane mode."""
    page = _ask_page()
    assert "B.storage.get('localStorage', KEY)" in page
    assert "B.storage.set('localStorage', KEY" in page
    assert "lane: tag" in page
    assert "W.prepareSend(B.api, turns)" in page  # the mode is re-read for every send
    assert 'id="clear"' in page, "Clear button must be present"


def test_ask_html_has_stage_indicator():
    """stage events update a progress line with a ticking clock."""
    page = _ask_page()
    assert "chunk.event === 'stage'" in page
    assert "function stageLine" in page
    assert "setInterval" in page


@pytest.mark.asyncio
async def test_poll_stage_events_returns_recent_orchestrator_decisions(tmp_path):
    """_poll_stage_events surfaces orchestrator_decision
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
    """when the DB doesn't exist or the schema is
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
