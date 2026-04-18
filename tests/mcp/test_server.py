"""Tests for src/mcp/server.py.

TC-MCP-001 — initialize returns server capabilities
TC-MCP-002 — tools/list returns all 4 tool definitions
TC-MCP-003 — tools/call borg_route.list_models returns model list
TC-MCP-004 — tools/call borg_route.chat returns content array
TC-MCP-005 — BRT-API-031 returned for unknown method
TC-MCP-006 — BRT-API-032 returned for unknown tool name
TC-MCP-007 — BRT-API-030 returned for malformed JSON
TC-MCP-008 — JSON-RPC response has correct structure (id, result, jsonrpc)
"""
import json
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _rpc(method: str, params: dict = None, request_id: int = 1) -> dict:
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return body


def test_initialize(client):
    """TC-MCP-001 — initialize returns server capabilities."""
    resp = client.post("/mcp", json=_rpc("initialize"))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == 1
    assert "result" in body
    assert body["result"]["serverInfo"]["name"] == "borg_route"
    assert "tools" in body["result"]["capabilities"]


def test_tools_list(client):
    """TC-MCP-002 — tools/list returns all 4 tools."""
    resp = client.post("/mcp", json=_rpc("tools/list"))
    assert resp.status_code == 200
    body = resp.json()
    tools = body["result"]["tools"]
    tool_names = {t["name"] for t in tools}
    assert "borg_route.chat" in tool_names
    assert "borg_route.submit_task" in tool_names
    assert "borg_route.get_run" in tool_names
    assert "borg_route.list_models" in tool_names


def test_tools_call_list_models(client):
    """TC-MCP-003 — borg_route.list_models returns model list."""
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "borg_route.list_models", "arguments": {}}),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    content = body["result"]["content"]
    assert len(content) >= 1
    assert content[0]["type"] == "text"


def test_tools_call_chat(client):
    """TC-MCP-004 — borg_route.chat returns content array."""
    async def mock_pipeline(user_instruction, thread_window=None, **kwargs):
        return {
            "run_id": "mcp-run",
            "response": "MCP answer.",
            "status": "completed",
            "latency_ms": 50,
            "envelope_count": 0,
            "error": None,
        }

    with patch("src.mcp.server._tool_chat", new=AsyncMock(return_value={
        "content": [{"type": "text", "text": "MCP answer."}],
        "isError": False,
        "_meta": {"run_id": "mcp-run"},
    })):
        resp = client.post(
            "/mcp",
            json=_rpc("tools/call", {
                "name": "borg_route.chat",
                "arguments": {"messages": [{"role": "user", "content": "Hello MCP"}]},
            }),
        )

    assert resp.status_code == 200
    body = resp.json()
    content = body["result"]["content"]
    assert content[0]["type"] == "text"
    assert content[0]["text"] == "MCP answer."


def test_unknown_method(client):
    """TC-MCP-005 — BRT-API-031 for unknown method."""
    resp = client.post("/mcp", json=_rpc("nonexistent/method"))
    assert resp.status_code == 200  # JSON-RPC errors still return 200
    body = resp.json()
    assert "error" in body
    assert "BRT-API-031" in body["error"]["message"]


def test_unknown_tool(client):
    """TC-MCP-006 — BRT-API-032 for unknown tool."""
    resp = client.post(
        "/mcp",
        json=_rpc("tools/call", {"name": "nonexistent.tool", "arguments": {}}),
    )
    assert resp.status_code == 200
    body = resp.json()
    assert "error" in body
    assert "BRT-API-032" in body["error"]["message"]


def test_malformed_json(client):
    """TC-MCP-007 — BRT-API-030 for invalid JSON."""
    resp = client.post(
        "/mcp",
        content="not json",
        headers={"content-type": "application/json"},
    )
    assert resp.status_code == 400
    body = resp.json()
    assert "error" in body
    assert "BRT-API-030" in body["error"]["message"]


def test_jsonrpc_response_structure(client):
    """TC-MCP-008 — JSON-RPC response has id, result, jsonrpc=2.0."""
    resp = client.post("/mcp", json=_rpc("tools/list", request_id=42))
    body = resp.json()
    assert body["jsonrpc"] == "2.0"
    assert body["id"] == 42
    assert "result" in body
