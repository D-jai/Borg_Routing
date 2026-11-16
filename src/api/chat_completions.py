"""OpenAI-compatible chat completions gateway — Surface B.

POST /v1/chat/completions — accepts standard OpenAI request bodies.
Model aliases:
  borg_route               → full orchestration, SYSTEM_CHOOSE
  borg_route:light         → quality_requirement=5
  borg_route:mid           → quality_requirement=7
  borg_route:heavy         → quality_requirement=9
  borg_route:coding|creative|reasoning|extraction → specialty hint
  borg_route:local         → LOCAL_FIRST strategy
  {exact model_id}         → bypass orchestration, direct to that model

Streaming: if stream=true, emit SSE with OpenAI-compat data: lines.
  - Bypass mode: relays actual SSE chunks from the underlying model endpoint.
  - Orchestration mode: word-level SSE after pipeline completion; every
    character, including newlines, is preserved.

Error codes:
- BRT-API-010: malformed request body
- BRT-API-011: model not found (bypass mode)
- BRT-API-012: pipeline execution failed
- BRT-API-013: bypass model streaming relay failed
"""
import asyncio
import json
import os
import re
import time
import uuid
from typing import AsyncIterator, Optional

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ValidationError

from src.db.schema import DB_PATH, create_all
from src.pipeline.runner import run_pipeline
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.model_call import resolve_model_call
from src.registry.router import select_model

router = APIRouter()

_db_initialised = False


async def _ensure_db(db_path: str = DB_PATH) -> None:
    global _db_initialised
    if not _db_initialised:
        await create_all(db_path)
        _db_initialised = True


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatRequest(BaseModel):
    model: str = "borg_route"
    messages: list[ChatMessage]
    stream: bool = False
    max_tokens: Optional[int] = None
    temperature: Optional[float] = None


def _parse_model_alias(model: str) -> tuple[str, float, Optional[str], str]:
    """Parse model alias into (base, quality_req, specialty_hint, strategy).

    Returns:
        (base_model, quality_requirement, specialty_hint, strategy)
    """
    if not model.startswith("borg_route"):
        return (model, 7.0, None, "SYSTEM_CHOOSE")

    parts = model.split(":")
    if len(parts) == 1:
        return ("borg_route", 7.0, None, "SYSTEM_CHOOSE")

    hint = parts[1]
    if hint == "light":
        return ("borg_route", 5.0, None, "SYSTEM_CHOOSE")
    elif hint == "mid":
        return ("borg_route", 7.0, None, "SYSTEM_CHOOSE")
    elif hint == "heavy":
        return ("borg_route", 9.0, None, "SYSTEM_CHOOSE")
    elif hint == "local":
        return ("borg_route", 6.0, None, "LOCAL_FIRST")
    elif hint in ("coding", "creative", "reasoning", "extraction"):
        return ("borg_route", 7.0, hint, "SYSTEM_CHOOSE")
    else:
        return ("borg_route", 7.0, None, "SYSTEM_CHOOSE")


def _build_openai_response(
    run_id: str,
    model_alias: str,
    response_text: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
    include_usage: bool = True,
) -> dict:
    """Build an OpenAI-shaped chat completion response.

    ``include_usage=False`` leaves the ``usage`` block out (a provider that
    reported none), rather than claiming zero tokens.
    """
    body = {
        "id": f"chatcmpl-{run_id}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": model_alias,
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": response_text,
                },
                "finish_reason": "stop",
            }
        ],
    }
    if include_usage:
        body["usage"] = {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        }
    return body


async def _stream_response(response_text: str, model_alias: str, run_id: str) -> AsyncIterator[str]:
    """Emit OpenAI-compat SSE chunks for the response text."""
    chunk_id = f"chatcmpl-{run_id}"
    created = int(time.time())

    # Word-sized chunks, each word keeping the whitespace after it, so the
    # concatenated stream equals the text exactly (newlines and indents too).
    pieces = re.findall(r"\S+\s*|\s+", response_text)
    for delta_content in pieces:
        chunk = {
            "id": chunk_id,
            "object": "chat.completion.chunk",
            "created": created,
            "model": model_alias,
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": delta_content},
                    "finish_reason": None,
                }
            ],
        }
        yield f"data: {json.dumps(chunk)}\n\n"
        await asyncio.sleep(0)  # yield to event loop

    # Final chunk
    final_chunk = {
        "id": chunk_id,
        "object": "chat.completion.chunk",
        "created": created,
        "model": model_alias,
        "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
    }
    yield f"data: {json.dumps(final_chunk)}\n\n"
    yield "data: [DONE]\n\n"


async def _poll_stage_events(
    started_at: float,
    last_seen_log_id: int,
    db_path: str,
    run_id: Optional[str] = None,
) -> tuple[list[dict], int]:
    """BUG-018 — pull recent orchestrator_decision rows from the logs
    table so the SSE layer can surface them as stage transitions.

    Returns a list of {stage, detail, ts} dicts and the highest log_id
    seen so the next poll only fetches new rows. ``run_id`` limits the rows
    to one run (R3); without it every run's rows come back.
    """
    import aiosqlite
    events: list[dict] = []
    new_max = last_seen_log_id
    try:
        async with aiosqlite.connect(db_path) as db:
            db.row_factory = aiosqlite.Row
            run_filter = "AND run_id = ?" if run_id else ""
            params: tuple = (last_seen_log_id, str(int(started_at)))
            if run_id:
                params += (str(run_id),)
            async with db.execute(
                f"""SELECT log_id, model_id, decision_json, created_at
                   FROM logs
                   WHERE event_type = 'orchestrator_decision'
                     AND log_id > ?
                     AND strftime('%s', created_at) >= ?
                     {run_filter}
                   ORDER BY log_id ASC
                   LIMIT 20""",
                params,
            ) as cur:
                rows = await cur.fetchall()
        for row in rows:
            new_max = max(new_max, row["log_id"])
            decision: dict = {}
            try:
                decision = json.loads(row["decision_json"]) if row["decision_json"] else {}
            except Exception:
                pass
            stage = decision.get("mode") or decision.get("code") or "orchestrator"
            detail = decision.get("msg") or decision.get("rationale") or row["model_id"] or ""
            events.append({
                "stage": str(stage),
                "detail": str(detail)[:200],
            })
    except Exception:
        # Polling is best-effort; never let a DB hiccup kill the SSE.
        pass
    return events, new_max


async def _stream_orchestration_with_heartbeats(
    user_instruction: str,
    thread_window: list,
    model_alias: str,
) -> AsyncIterator[str]:
    """BRT-STR-01 — keep the SSE connection alive while the pipeline runs.

    run_pipeline produces bytes only after orchestrate_run completes. On a
    multi-node plan this can take 60-90s, and browsers time out the fetch
    before any data arrives. We emit SSE comment lines (": heartbeat\\n\\n")
    every BORG_STREAM_HEARTBEAT_SEC until the pipeline task resolves, then
    word-stream the real response. Comments are ignored by SSE consumers
    but reset their idle timers.

    BUG-018 — between heartbeats we poll the logs table for new
    orchestrator_decision rows and emit them as stage events. The chat
    UI listens for `chunk.event === 'stage'` and updates a thinking
    indicator in place, so multi-stage runs show real progress instead
    of an opaque spinner.
    """
    heartbeat_every = float(os.getenv("BORG_STREAM_HEARTBEAT_SEC", "5"))
    started_at = time.time()
    last_log_id = 0
    # Issue the run id here so stage polling can be limited to this run.
    own_run_id = uuid.uuid4()
    pipeline_task = asyncio.create_task(
        run_pipeline(
            user_instruction=user_instruction,
            thread_window=thread_window,
            run_id=own_run_id,
        )
    )

    while not pipeline_task.done():
        try:
            await asyncio.wait_for(
                asyncio.shield(pipeline_task),
                timeout=heartbeat_every,
            )
        except asyncio.TimeoutError:
            # BUG-018 — emit stage events found since the last poll;
            # fall back to a heartbeat comment when nothing's new so
            # the connection still stays warm.
            events, last_log_id = await _poll_stage_events(
                started_at=started_at,
                last_seen_log_id=last_log_id,
                db_path=DB_PATH,
                run_id=str(own_run_id),
            )
            if events:
                for ev in events:
                    payload = {
                        "event": "stage",
                        "stage": ev["stage"],
                        "detail": ev["detail"],
                        "elapsed_ms": int((time.time() - started_at) * 1000),
                    }
                    yield f"data: {json.dumps(payload)}\n\n"
            else:
                yield f": heartbeat {int(time.time())}\n\n"

    try:
        result = pipeline_task.result()
    except Exception as exc:
        err_run_id = str(uuid.uuid4())
        async for chunk in _stream_response(
            f"Pipeline error: {exc}", model_alias, err_run_id
        ):
            yield chunk
        return

    if result["status"] == "failed":
        response_text = result["response"] or f"Request failed: {result.get('error', 'unknown')}"
    else:
        response_text = result["response"]

    run_id = result.get("run_id") or str(uuid.uuid4())
    async for chunk in _stream_response(response_text, model_alias, run_id):
        yield chunk


def _usage_tokens(usage: Optional[dict]) -> Optional[tuple[int, int]]:
    """(prompt, completion) tokens from a provider usage block, or None."""
    if not isinstance(usage, dict):
        return None
    tin = usage.get("prompt_tokens")
    tout = usage.get("completion_tokens")
    if tin is None and tout is None:
        return None
    return int(tin or 0), int(tout or 0)


async def _record_bypass_call(
    matrix: CapabilityMatrix,
    model_id: str,
    run_id: str,
    messages: list[ChatMessage],
    text: str,
    usage: Optional[dict],
    latency_ms: int,
) -> None:
    """Log a direct-to-model call as a ``model_call`` row under its run id,
    priced like any other call, and save the run's digest. Never raises: the
    caller already has its answer."""
    try:
        from src.db.state import (  # noqa: PLC0415
            compute_and_save_digest,
            create_run,
            update_run_status,
        )
        from src.logging.writer import flush_pending, write_event  # noqa: PLC0415
        from src.registry.cost import price_with_baseline  # noqa: PLC0415

        db_path = DB_PATH
        await create_all(db_path)
        run_uuid = uuid.UUID(run_id)
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        await create_run(
            last_user or "(bypass)", db_path=db_path, run_id=run_uuid, kind="bypass"
        )
        await update_run_status(run_uuid, "completed", db_path=db_path)

        tokens = _usage_tokens(usage)
        model_info = matrix.get(model_id)
        dollars = baseline = None
        if tokens is not None:
            dollars, baseline = await price_with_baseline(
                model_info, tokens[0], tokens[1], db_path, matrix=matrix
            )
        await write_event(
            "model_call",
            run_id=run_uuid,
            model_id=model_id,
            tokens_input=tokens[0] if tokens else None,
            tokens_output=tokens[1] if tokens else None,
            dollars=dollars,
            baseline_dollars=baseline,
            latency_ms=latency_ms,
            decision={"mode": "bypass"},
            payload_in=json.dumps([{"role": m.role, "content": m.content} for m in messages])[:2000],
            payload_out=text[:2000],
            db_path=db_path,
        )
        await flush_pending()
        await compute_and_save_digest(run_uuid, db_path=db_path)
    except Exception:
        pass


async def _bypass_to_model(
    matrix: CapabilityMatrix,
    model_id: str,
    messages: list[ChatMessage],
) -> tuple[str, Optional[dict]]:
    """Send the request directly to a specific model, bypassing orchestration.

    Returns (reply text, the provider's ``usage`` block or None if it sent none).
    """
    model_info = matrix.get(model_id)
    if not model_info:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-API-011", "message": f"Model '{model_id}' not found in matrix"},
        )
    try:
        url, headers, body_model = resolve_model_call(model_info)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={"error": "BRT-API-011", "message": str(exc)},
        ) from exc

    max_tokens = int(os.getenv("ANSWERER_MAX_TOKENS", "2000"))
    payload = {
        "model": body_model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "max_tokens": max_tokens,
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(url, json=payload, headers=headers)
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"], body.get("usage")


async def _stream_bypass_relay(
    matrix: CapabilityMatrix,
    model_id: str,
    model_alias: str,
    messages: list[ChatMessage],
    run_id: str,
) -> AsyncIterator[str]:
    """Relay actual SSE chunks from an underlying model endpoint.

    If the model endpoint does not support streaming, falls back to
    word-by-word simulation so the caller always gets a valid SSE stream.
    """
    model_info = matrix.get(model_id)
    if not model_info:
        # Fallback: non-streaming fetch then simulate
        try:
            text, _usage = await _bypass_to_model(matrix, model_id, messages)
        except Exception as exc:
            err = {"id": f"chatcmpl-{run_id}", "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model_alias,
                   "choices": [{"index": 0, "delta": {"content": f"[BRT-API-013] {exc}"}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n"
            return
        async for chunk in _stream_response(text, model_alias, run_id):
            yield chunk
        return

    try:
        url, headers, body_model = resolve_model_call(model_info)
    except ValueError:
        try:
            text, _usage = await _bypass_to_model(matrix, model_id, messages)
        except Exception as exc:
            err = {"id": f"chatcmpl-{run_id}", "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model_alias,
                   "choices": [{"index": 0, "delta": {"content": f"[BRT-API-013] {exc}"}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n"
            return
        async for chunk in _stream_response(text, model_alias, run_id):
            yield chunk
        return

    max_tokens = int(os.getenv("ANSWERER_MAX_TOKENS", "2000"))
    payload = {
        "model": body_model,
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "stream": True,
        "max_tokens": max_tokens,
    }
    t_stream = time.monotonic()
    relay_usage: Optional[dict] = None
    recorded = False
    relay_text: list[str] = []

    async def _record_relay() -> None:
        nonlocal recorded
        if recorded:
            return
        recorded = True
        await _record_bypass_call(
            matrix, model_id, run_id, messages, "".join(relay_text), relay_usage,
            int((time.monotonic() - t_stream) * 1000),
        )

    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", url, json=payload, headers=headers
            ) as resp:
                if resp.status_code != 200:
                    # Fallback to non-stream
                    t_fb = time.monotonic()
                    text, fb_usage = await _bypass_to_model(matrix, model_id, messages)
                    recorded = True   # the fallback row below is this run's one record
                    await _record_bypass_call(
                        matrix, model_id, run_id, messages, text, fb_usage,
                        int((time.monotonic() - t_fb) * 1000),
                    )
                    async for chunk in _stream_response(text, model_alias, run_id):
                        yield chunk
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        payload_str = line[5:].strip()
                        if payload_str == "[DONE]":
                            await _record_relay()
                            yield "data: [DONE]\n\n"
                            return
                        # Re-emit with our model alias substituted
                        try:
                            chunk = json.loads(payload_str)
                            # Providers that report usage send it on the last
                            # chunk; if none does, usage stays absent.
                            if isinstance(chunk.get("usage"), dict):
                                relay_usage = chunk["usage"]
                            for choice in chunk.get("choices") or []:
                                piece = (choice.get("delta") or {}).get("content")
                                if piece:
                                    relay_text.append(piece)
                            chunk["model"] = model_alias
                            yield f"data: {json.dumps(chunk)}\n\n"
                        except json.JSONDecodeError:
                            yield f"data: {payload_str}\n\n"
    except Exception as exc:
        err_chunk = {
            "id": f"chatcmpl-{run_id}", "object": "chat.completion.chunk",
            "created": int(time.time()), "model": model_alias,
            "choices": [{"index": 0, "delta": {"content": f"[BRT-API-013] Streaming relay failed: {exc}"}, "finish_reason": "stop"}],
        }
        yield f"data: {json.dumps(err_chunk)}\n\ndata: [DONE]\n\n"
    finally:
        # Normal end, provider error mid-stream and client disconnect
        # (GeneratorExit) all record the call once, with whatever usage is known.
        await _record_relay()


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """OpenAI-compatible chat completions endpoint."""
    await _ensure_db()

    # Parse request
    try:
        body = await request.json()
        chat_req = ChatRequest(**body)
    except (json.JSONDecodeError, ValidationError, Exception) as exc:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-010", "message": f"Malformed request: {exc}"},
        )

    model_alias = chat_req.model
    base_model, quality_req, specialty_hint, strategy = _parse_model_alias(model_alias)

    # Determine user instruction from the last user message
    user_messages = [m for m in chat_req.messages if m.role == "user"]
    if not user_messages:
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-API-010", "message": "No user message found in messages"},
        )
    user_instruction = user_messages[-1].content

    # Build thread window (all messages except the last user message)
    thread_window = [
        {"role": m.role, "content": m.content}
        for m in chat_req.messages[:-1]
    ]

    # Bypass mode: exact model_id, not a borg_route alias
    if base_model != "borg_route":
        from main import matrix as app_matrix
        run_id = str(uuid.uuid4())
        if chat_req.stream:
            return StreamingResponse(
                _stream_bypass_relay(app_matrix, base_model, model_alias, chat_req.messages, run_id),
                media_type="text/event-stream",
            )
        t_bypass = time.monotonic()
        try:
            response_text, bypass_usage = await _bypass_to_model(
                app_matrix, base_model, chat_req.messages
            )
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "BRT-API-012", "message": f"Bypass model failed: {exc}"},
            )
        await _record_bypass_call(
            app_matrix, base_model, run_id, chat_req.messages, response_text,
            bypass_usage, int((time.monotonic() - t_bypass) * 1000),
        )
        tokens = _usage_tokens(bypass_usage)
        if tokens is None:
            return JSONResponse(content=_build_openai_response(
                run_id, model_alias, response_text, include_usage=False))
        return JSONResponse(content=_build_openai_response(
            run_id, model_alias, response_text,
            prompt_tokens=tokens[0], completion_tokens=tokens[1]))

    # Full orchestration via pipeline runner.
    # BRT-STR-01 — streaming path uses heartbeats to keep the browser SSE
    # connection alive while run_pipeline churns through a multi-node plan;
    # non-streaming path behaves as before.
    if chat_req.stream:
        return StreamingResponse(
            _stream_orchestration_with_heartbeats(
                user_instruction=user_instruction,
                thread_window=thread_window,
                model_alias=model_alias,
            ),
            media_type="text/event-stream",
        )

    try:
        result = await run_pipeline(
            user_instruction=user_instruction,
            thread_window=thread_window,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-012", "message": f"Pipeline error: {exc}"},
        )

    run_id = result.get("run_id") or str(uuid.uuid4())
    if result["status"] == "failed":
        # Return a 200 with the failure message (OpenAI compat — errors go in content).
        # Always include the failure reason and run_id so the user has something
        # to grep for in the trace; generic fallbacks helped nobody.
        err_text = result.get("error") or "unknown error"
        response_text = (
            f"Request failed: {err_text}. "
            f"See GET /runs/{run_id} for the full trace."
        )
    else:
        response_text = result["response"]

    usage = result.get("usage") or {}
    return JSONResponse(
        content=_build_openai_response(
            run_id,
            model_alias,
            response_text,
            prompt_tokens=int(usage.get("prompt_tokens", 0)),
            completion_tokens=int(usage.get("completion_tokens", 0)),
        )
    )
