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
  - Orchestration mode: word-level SSE after pipeline completion.

Error codes:
- BRT-API-010: malformed request body
- BRT-API-011: model not found (bypass mode)
- BRT-API-012: pipeline execution failed
- BRT-API-013: bypass model streaming relay failed
"""
import asyncio
import json
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
) -> dict:
    """Build an OpenAI-shaped chat completion response."""
    return {
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
        "usage": {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
        },
    }


async def _stream_response(response_text: str, model_alias: str, run_id: str) -> AsyncIterator[str]:
    """Emit OpenAI-compat SSE chunks for the response text."""
    chunk_id = f"chatcmpl-{run_id}"
    created = int(time.time())

    # Stream word by word for a natural feel
    words = response_text.split()
    for i, word in enumerate(words):
        delta_content = word + (" " if i < len(words) - 1 else "")
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


async def _bypass_to_model(
    matrix: CapabilityMatrix,
    model_id: str,
    messages: list[ChatMessage],
) -> str:
    """Send the request directly to a specific model, bypassing orchestration."""
    model_info = matrix.get(model_id)
    if not model_info:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-API-011", "message": f"Model '{model_id}' not found in matrix"},
        )
    if not model_info.endpoint:
        raise HTTPException(
            status_code=422,
            detail={"error": "BRT-API-011", "message": f"Model '{model_id}' has no endpoint configured"},
        )

    payload = {
        "model": "default",
        "messages": [{"role": m.role, "content": m.content} for m in messages],
    }
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{model_info.endpoint}/v1/chat/completions", json=payload)
    resp.raise_for_status()
    body = resp.json()
    return body["choices"][0]["message"]["content"]


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
    if not model_info or not model_info.endpoint:
        # Fallback: non-streaming fetch then simulate
        try:
            text = await _bypass_to_model(matrix, model_id, messages)
        except Exception as exc:
            err = {"id": f"chatcmpl-{run_id}", "object": "chat.completion.chunk",
                   "created": int(time.time()), "model": model_alias,
                   "choices": [{"index": 0, "delta": {"content": f"[BRT-API-013] {exc}"}, "finish_reason": "stop"}]}
            yield f"data: {json.dumps(err)}\n\ndata: [DONE]\n\n"
            return
        async for chunk in _stream_response(text, model_alias, run_id):
            yield chunk
        return

    payload = {
        "model": "default",
        "messages": [{"role": m.role, "content": m.content} for m in messages],
        "stream": True,
    }
    try:
        async with httpx.AsyncClient(timeout=120) as client:
            async with client.stream(
                "POST", f"{model_info.endpoint}/v1/chat/completions", json=payload
            ) as resp:
                if resp.status_code != 200:
                    # Fallback to non-stream
                    text = await _bypass_to_model(matrix, model_id, messages)
                    async for chunk in _stream_response(text, model_alias, run_id):
                        yield chunk
                    return
                async for line in resp.aiter_lines():
                    if not line:
                        continue
                    if line.startswith("data:"):
                        payload_str = line[5:].strip()
                        if payload_str == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        # Re-emit with our model alias substituted
                        try:
                            chunk = json.loads(payload_str)
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
        try:
            response_text = await _bypass_to_model(app_matrix, base_model, chat_req.messages)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(
                status_code=502,
                detail={"error": "BRT-API-012", "message": f"Bypass model failed: {exc}"},
            )
        return JSONResponse(content=_build_openai_response(run_id, model_alias, response_text))

    # Full orchestration via pipeline runner
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

    if result["status"] == "failed":
        # Return a 200 with the failure message (OpenAI compat — errors go in content)
        response_text = result["response"] or f"Request failed: {result.get('error', 'unknown')}"
    else:
        response_text = result["response"]

    run_id = result.get("run_id") or str(uuid.uuid4())

    if chat_req.stream:
        return StreamingResponse(
            _stream_response(response_text, model_alias, run_id),
            media_type="text/event-stream",
        )

    return JSONResponse(content=_build_openai_response(run_id, model_alias, response_text))
