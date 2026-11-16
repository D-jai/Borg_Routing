"""MCP server — Surface D.

Implements a minimal MCP-over-HTTP server using JSON-RPC 2.0 protocol.
Mount at POST /mcp. Supports:
  - initialize         → server capabilities
  - tools/list         → available tools
  - tools/call         → call a named tool

Tools exposed:
  borg_route.chat(messages, model) → chat completion response
  borg_route.submit_task(instruction, inputs_ref) → {run_id}
  borg_route.get_run(run_id) → run state
  borg_route.list_models() → capability matrix

Error codes:
- BRT-API-030: invalid JSON-RPC request
- BRT-API-031: method not found
- BRT-API-032: tool not found
- BRT-API-033: tool execution failed
- BRT-API-034: missing required tool argument
"""
import json
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

router = APIRouter()

# Strong references so started runs are not garbage-collected mid-flight.
_BACKGROUND_RUNS: set = set()

SERVER_INFO = {
    "name": "borg_route",
    "version": "0.1.0",
}

TOOL_DEFINITIONS = [
    {
        "name": "borg_route.chat",
        "description": "Send a chat message through BORG_ROUTE orchestration.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "messages": {
                    "type": "array",
                    "items": {"type": "object"},
                    "description": "OpenAI-format message list",
                },
                "model": {
                    "type": "string",
                    "default": "borg_route",
                    "description": "Model alias (borg_route, borg_route:coding, etc.)",
                },
            },
            "required": ["messages"],
        },
    },
    {
        "name": "borg_route.submit_task",
        "description": "Submit an async task and get a run_id back.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "instruction": {"type": "string"},
                "inputs_ref": {"type": "string"},
            },
            "required": ["instruction"],
        },
    },
    {
        "name": "borg_route.get_run",
        "description": "Fetch the status and result of a run.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "run_id": {"type": "string"},
            },
            "required": ["run_id"],
        },
    },
    {
        "name": "borg_route.list_models",
        "description": "List all models in the capability matrix.",
        "inputSchema": {
            "type": "object",
            "properties": {},
        },
    },
]


def _jsonrpc_response(request_id: Any, result: Any) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


async def _handle_initialize(params: dict) -> dict:
    return {
        "protocolVersion": "2024-11-05",
        "capabilities": {"tools": {"listChanged": False}},
        "serverInfo": SERVER_INFO,
    }


async def _handle_tools_list(params: dict) -> dict:
    return {"tools": TOOL_DEFINITIONS}


async def _handle_tools_call(params: dict) -> dict:
    """Dispatch a tool call to the appropriate handler."""
    tool_name = params.get("name")
    arguments = params.get("arguments", {})

    if tool_name == "borg_route.chat":
        return await _tool_chat(arguments)
    elif tool_name == "borg_route.submit_task":
        return await _tool_submit_task(arguments)
    elif tool_name == "borg_route.get_run":
        return await _tool_get_run(arguments)
    elif tool_name == "borg_route.list_models":
        return await _tool_list_models(arguments)
    else:
        raise ValueError(f"BRT-API-032: tool '{tool_name}' not found")


async def _tool_chat(args: dict) -> dict:
    messages = args.get("messages")
    if not messages:
        raise ValueError("BRT-API-034: 'messages' is required for borg_route.chat")
    model = args.get("model", "borg_route")

    from src.pipeline.runner import run_pipeline
    from src.db.schema import DB_PATH

    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        raise ValueError("BRT-API-034: no user message found in messages")

    instruction = user_msgs[-1]["content"]
    thread_window = [m for m in messages[:-1]]

    result = await run_pipeline(
        user_instruction=instruction,
        thread_window=thread_window,
    )

    return {
        "content": [{"type": "text", "text": result["response"]}],
        "isError": result["status"] == "failed",
        "_meta": {
            "run_id": result.get("run_id"),
            "latency_ms": result.get("latency_ms"),
        },
    }


async def _tool_submit_task(args: dict) -> dict:
    instruction = args.get("instruction")
    if not instruction:
        raise ValueError("BRT-API-034: 'instruction' is required for borg_route.submit_task")

    from src.db.state import create_run
    from src.db.schema import DB_PATH
    run_id = await create_run(instruction, db_path=DB_PATH)

    # R9: create_run only writes the row; start the run the way POST /v1/task
    # does (same background driver: wall-clock cap, failure reason, digest).
    import asyncio
    from src.api.tasks import _run_in_background

    task = asyncio.get_running_loop().create_task(
        _run_in_background(instruction, str(run_id), DB_PATH)
    )
    _BACKGROUND_RUNS.add(task)
    task.add_done_callback(_BACKGROUND_RUNS.discard)

    return {
        "content": [{"type": "text", "text": f"Task submitted. run_id: {run_id}"}],
        "isError": False,
        # Same status POST /v1/task returns; the row is "pending" until the
        # background run ends as completed or failed.
        "_meta": {"run_id": str(run_id), "status": "pending"},
    }


async def _tool_get_run(args: dict) -> dict:
    run_id = args.get("run_id")
    if not run_id:
        raise ValueError("BRT-API-034: 'run_id' is required for borg_route.get_run")

    import uuid as _uuid
    from src.db.state import get_run, get_envelopes
    from src.db.schema import DB_PATH

    run_uuid = _uuid.UUID(run_id)
    run = await get_run(run_uuid, db_path=DB_PATH)
    envelopes = await get_envelopes(run_uuid, db_path=DB_PATH)

    summary = {
        "run_id": run["run_id"],
        "status": run["status"],
        "user_instruction": run["user_instruction"],
        "envelope_count": len(envelopes),
    }
    return {
        "content": [{"type": "text", "text": json.dumps(summary)}],
        "isError": False,
        "_meta": summary,
    }


async def _tool_list_models(args: dict) -> dict:
    from src.orchestrator.engine import _get_matrix
    matrix = _get_matrix()
    models = [
        {
            "model_id": m.model_id,
            "source": m.source,
            "context_length": m.context_length,
        }
        for m in matrix.all()
    ]
    return {
        "content": [{"type": "text", "text": json.dumps(models)}],
        "isError": False,
        "_meta": {"count": len(models)},
    }


_METHOD_HANDLERS = {
    "initialize": _handle_initialize,
    "tools/list": _handle_tools_list,
    "tools/call": _handle_tools_call,
}


@router.post("/mcp")
async def mcp_endpoint(request: Request) -> JSONResponse:
    """MCP protocol over HTTP — JSON-RPC 2.0."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content=_jsonrpc_error(None, -32700, "BRT-API-030: parse error — invalid JSON"),
            status_code=400,
        )

    if not isinstance(body, dict):
        return JSONResponse(
            content=_jsonrpc_error(None, -32600, "BRT-API-030: invalid JSON-RPC request"),
            status_code=400,
        )

    request_id = body.get("id")
    method = body.get("method")
    params = body.get("params", {})

    if not method:
        return JSONResponse(
            content=_jsonrpc_error(request_id, -32600, "BRT-API-030: 'method' is required"),
        )

    handler = _METHOD_HANDLERS.get(method)
    if handler is None:
        return JSONResponse(
            content=_jsonrpc_error(
                request_id, -32601, f"BRT-API-031: method '{method}' not found"
            ),
        )

    try:
        result = await handler(params)
        return JSONResponse(content=_jsonrpc_response(request_id, result))
    except ValueError as exc:
        return JSONResponse(
            content=_jsonrpc_error(request_id, -32603, f"BRT-API-033: {exc}"),
        )
    except Exception as exc:
        return JSONResponse(
            content=_jsonrpc_error(request_id, -32603, f"BRT-API-033: tool execution failed: {exc}"),
        )
