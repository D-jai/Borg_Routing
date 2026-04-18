"""Slice 4 tests — historical memory + rolling summary.

TC-S4-001 — compress_inactive_turns: fewer messages than keep_last_n returns existing summary
TC-S4-002 — compress_inactive_turns: nonexistent thread raises BRT-MEM-001
TC-S4-003 — compress_inactive_turns: calls model for inactive turns, stores summary, returns it
TC-S4-004 — compress_inactive_turns: model timeout raises BRT-MEM-003
TC-S4-005 — compress_inactive_turns: empty model response raises BRT-MEM-004
TC-S4-006 — run_pipeline: auto-loads rolling_summary from config when thread_id provided
TC-S4-007 — run_pipeline: explicit rolling_summary is not overridden by stored value
TC-S4-008 — 15-turn thread: compress compresses first 10, runner passes summary to orchestrate_run
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from src.db.schema import create_all
from src.db.state import get_config, set_config
from src.memory.window import create_thread, add_message
from src.memory.compressor import compress_inactive_turns
from src.pipeline.runner import run_pipeline


# ──────────────────────────────────────────────────────────────────────────────
# Fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "slice4_test.db")
    await create_all(path)
    return path


def _mock_model_response(summary_text: str, tokens_in: int = 10, tokens_out: int = 20) -> MagicMock:
    """Return a mock httpx.Response that looks like a chat completion."""
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json = MagicMock(return_value={
        "choices": [{"message": {"content": summary_text}}],
        "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out},
    })
    return resp


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-001 — not enough messages to compress
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_fewer_than_keep_last_n_returns_existing(db):
    """TC-S4-001 — fewer messages than keep_last_n: no model call, returns stored summary."""
    thread_id = await create_thread(db_path=db)
    for i in range(3):
        await add_message(thread_id, "user", f"msg {i}", db_path=db)

    # Pre-store a summary
    await set_config(f"rolling_summary:{thread_id}", "existing summary", db_path=db)

    with patch("httpx.AsyncClient") as mock_client_cls:
        result = await compress_inactive_turns(
            thread_id=thread_id,
            model_endpoint="http://localhost:1234",
            model_name="test-model",
            keep_last_n=5,
            db_path=db,
        )
        mock_client_cls.assert_not_called()

    assert result == "existing summary"


@pytest.mark.asyncio
async def test_compress_fewer_than_keep_last_n_no_stored_returns_empty(db):
    """TC-S4-001b — fewer messages, no stored summary: returns empty string."""
    thread_id = await create_thread(db_path=db)
    await add_message(thread_id, "user", "hello", db_path=db)

    with patch("httpx.AsyncClient"):
        result = await compress_inactive_turns(
            thread_id=thread_id,
            model_endpoint="http://localhost:1234",
            model_name="test-model",
            keep_last_n=5,
            db_path=db,
        )

    assert result == ""


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-002 — thread not found
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_nonexistent_thread_raises_mem001(db):
    """TC-S4-002 — BRT-MEM-001 raised for unknown thread_id."""
    with pytest.raises(ValueError, match="BRT-MEM-001"):
        await compress_inactive_turns(
            thread_id="nonexistent-thread",
            model_endpoint="http://localhost:1234",
            model_name="test-model",
            db_path=db,
        )


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-003 — happy path: model called, summary stored and returned
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_calls_model_stores_and_returns_summary(db):
    """TC-S4-003 — 8 messages, keep_last_n=5: compresses first 3, stores summary."""
    thread_id = await create_thread(db_path=db)
    for i in range(8):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(thread_id, role, f"turn {i}", db_path=db)

    mock_resp = _mock_model_response("Compressed summary of turns 0-2.")

    with patch("src.memory.compressor.write_event", new=AsyncMock()):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)

            result = await compress_inactive_turns(
                thread_id=thread_id,
                model_endpoint="http://localhost:1234",
                model_name="test-model",
                keep_last_n=5,
                db_path=db,
            )

    assert result == "Compressed summary of turns 0-2."

    stored = await get_config(f"rolling_summary:{thread_id}", db_path=db)
    assert stored == "Compressed summary of turns 0-2."

    # Only the last 5 messages should remain uncompressed — verify inactive
    # turns were in the prompt
    call_args = mock_client.post.call_args
    prompt_messages = call_args[1]["json"]["messages"]
    assert len(prompt_messages) == 1
    prompt_text = prompt_messages[0]["content"]
    assert "turn 0" in prompt_text
    assert "turn 2" in prompt_text
    # Active turns must NOT be in the prompt
    assert "turn 7" not in prompt_text


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-004 — model timeout raises BRT-MEM-003
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_model_timeout_raises_mem003(db):
    """TC-S4-004 — BRT-MEM-003 raised on httpx timeout."""
    thread_id = await create_thread(db_path=db)
    for i in range(6):
        await add_message(thread_id, "user", f"msg {i}", db_path=db)

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))

        with pytest.raises(ValueError, match="BRT-MEM-003"):
            await compress_inactive_turns(
                thread_id=thread_id,
                model_endpoint="http://localhost:1234",
                model_name="test-model",
                keep_last_n=5,
                db_path=db,
            )


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-005 — empty model response raises BRT-MEM-004
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_compress_empty_response_raises_mem004(db):
    """TC-S4-005 — BRT-MEM-004 raised when model returns empty content."""
    thread_id = await create_thread(db_path=db)
    for i in range(6):
        await add_message(thread_id, "user", f"msg {i}", db_path=db)

    mock_resp = _mock_model_response("")  # empty summary

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_resp)

        with pytest.raises(ValueError, match="BRT-MEM-004"):
            await compress_inactive_turns(
                thread_id=thread_id,
                model_endpoint="http://localhost:1234",
                model_name="test-model",
                keep_last_n=5,
                db_path=db,
            )


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-006 — runner auto-loads rolling_summary from config
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runner_autoloads_rolling_summary_from_config(db):
    """TC-S4-006 — run_pipeline loads rolling_summary from config when thread_id provided."""
    thread_id = await create_thread(db_path=db)
    await set_config(f"rolling_summary:{thread_id}", "stored summary text", db_path=db)

    captured = {}

    async def mock_orchestrate(run_id, user_instruction, thread_window,
                                rolling_summary, db_path, current_depth):
        captured["rolling_summary"] = rolling_summary
        return "ok"

    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate)):
        result = await run_pipeline(
            user_instruction="test task",
            thread_id=thread_id,
            db_path=db,
        )

    assert result["status"] == "completed"
    assert captured["rolling_summary"] == "stored summary text"


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-007 — explicit rolling_summary is not overridden
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_runner_explicit_summary_not_overridden(db):
    """TC-S4-007 — explicit rolling_summary beats stored config value."""
    thread_id = await create_thread(db_path=db)
    await set_config(f"rolling_summary:{thread_id}", "stored summary", db_path=db)

    captured = {}

    async def mock_orchestrate(run_id, user_instruction, thread_window,
                                rolling_summary, db_path, current_depth):
        captured["rolling_summary"] = rolling_summary
        return "ok"

    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate)):
        await run_pipeline(
            user_instruction="test task",
            thread_id=thread_id,
            rolling_summary="explicit summary",
            db_path=db,
        )

    assert captured["rolling_summary"] == "explicit summary"


# ──────────────────────────────────────────────────────────────────────────────
# TC-S4-008 — 15-turn thread: compress + runner passes summary to orchestrate_run
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_15_turn_thread_compact_context_passed_to_orchestrator(db):
    """TC-S4-008 — 15-turn thread; compress first 10; runner passes summary to orchestrate_run."""
    thread_id = await create_thread(db_path=db)
    for i in range(15):
        role = "user" if i % 2 == 0 else "assistant"
        await add_message(thread_id, role, f"message content {i}", db_path=db)

    mock_resp = _mock_model_response("Summary of turns 0 through 9.")

    # Step 1: compress — should compress turns 0..9, keep 10..14
    with patch("src.memory.compressor.write_event", new=AsyncMock()):
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client_cls.return_value.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client_cls.return_value.__aexit__ = AsyncMock(return_value=False)
            mock_client.post = AsyncMock(return_value=mock_resp)

            summary = await compress_inactive_turns(
                thread_id=thread_id,
                model_endpoint="http://localhost:1234",
                model_name="compressor-model",
                keep_last_n=5,
                db_path=db,
            )

    assert summary == "Summary of turns 0 through 9."

    # Verify inactive turns (0..9) are in the compression prompt; active (10..14) are not
    compress_call_args = mock_client.post.call_args
    compress_prompt = compress_call_args[1]["json"]["messages"][0]["content"]
    assert "message content 0" in compress_prompt
    assert "message content 9" in compress_prompt
    assert "message content 10" not in compress_prompt

    # Step 2: run_pipeline — should auto-load summary and pass it to orchestrate_run
    captured = {}

    async def mock_orchestrate(run_id, user_instruction, thread_window,
                                rolling_summary, db_path, current_depth):
        captured["rolling_summary"] = rolling_summary
        return "pipeline response"

    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate)):
        result = await run_pipeline(
            user_instruction="follow-up question",
            thread_id=thread_id,
            db_path=db,
        )

    assert result["status"] == "completed"
    assert captured["rolling_summary"] == "Summary of turns 0 through 9."
