"""TC-EXE-TOOL-001..004 — executor pre-fetches tool_requests before the LLM call.

The executor must:
- Do nothing different when tool_requests is empty (regression guard).
- Run tool_requests, inject results into dependency_input.tool_results.
- Surface a failed InboundEnvelope when every tool failed.
- Preserve tool results across model_copy (so the answerer prompt sees them).
"""
import json
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.agents.executor import _execute_answerer
from src.db.schema import create_all
from src.envelopes.schema import ToolRequest, WrappedTask
from src.tools.base import BaseTool, ToolError, ToolInvocationResult
from src.tools.registry import TOOL_REGISTRY, register_tool


class _OkTool(BaseTool):
    name = "tool_ok_exe"

    async def run(self, args: dict) -> ToolInvocationResult:
        return ToolInvocationResult(
            tool=self.name, args=args, status="ok",
            result={"hits": [{"title": "T", "url": "u", "snippet": "s"}]},
            provider="stub",
        )


class _FailTool(BaseTool):
    name = "tool_fail_exe"

    async def run(self, args: dict) -> ToolInvocationResult:
        raise ToolError("BRT-TOOL-002", "forced")


@pytest.fixture(autouse=True)
def _register_stubs():
    original = dict(TOOL_REGISTRY)
    register_tool(_OkTool())
    register_tool(_FailTool())
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(original)


@pytest.fixture
async def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    await create_all(path)
    yield path
    try:
        os.remove(path)
    except OSError:
        pass


def _make_answerer_task(tool_requests=None) -> WrappedTask:
    return WrappedTask(
        envelope_id=uuid4(),
        run_id=uuid4(),
        subtask_id=uuid4(),
        type="answerer",
        current_depth=0,
        max_depth=5,
        level_path="root",
        model_endpoint="http://localhost:1234",
        model_name="lmstudio/test-model",
        instruction="Say hi.",
        output_schema={"response": "string"},
        tool_requests=tool_requests or [],
    )


def _patch_model_call(captured: dict, response: dict):
    """Mock _call_model to capture the system prompt and return a fixed response."""
    async def fake_call(model, system_prompt, user_msg, requested_max_tokens=None):
        captured["system_prompt"] = system_prompt
        captured["model"] = model
        captured["requested_max_tokens"] = requested_max_tokens
        return json.dumps(response), 10, 5, "stop"

    return patch("src.agents.executor._call_model", side_effect=fake_call)


@pytest.mark.asyncio
async def test_no_tool_requests_behaves_as_before(db):
    """TC-EXE-TOOL-001 — empty tool_requests → executor never touches tool registry."""
    task = _make_answerer_task()
    captured = {}
    payload = {"status": "resolved", "output": {"response": "ok"}}
    with _patch_model_call(captured, payload):
        env = await _execute_answerer(task, db_path=db)
    assert env.status == "resolved"
    # tool_results must NOT be smuggled into the prompt when no requests exist.
    assert "tool_results" not in captured["system_prompt"]


@pytest.mark.asyncio
async def test_tool_results_injected_into_prompt(db):
    """TC-EXE-TOOL-002 — tool_requests run, results end up in the prompt."""
    task = _make_answerer_task(
        tool_requests=[ToolRequest(tool="tool_ok_exe", args={"q": "hi"})]
    )
    captured = {}
    payload = {"status": "resolved", "output": {"response": "ok"}}
    with _patch_model_call(captured, payload):
        env = await _execute_answerer(task, db_path=db)

    assert env.status == "resolved"
    assert "tool_results" in captured["system_prompt"]
    assert "tool_ok_exe" in captured["system_prompt"]
    assert "\"title\": \"T\"" in captured["system_prompt"] or \
           "'title': 'T'" in captured["system_prompt"]


@pytest.mark.asyncio
async def test_all_tools_failed_surface_bright_tool_002(db):
    """TC-EXE-TOOL-003 — if every tool failed, envelope returns failed with BRT-TOOL-002,
    and the model is never called."""
    task = _make_answerer_task(
        tool_requests=[ToolRequest(tool="tool_fail_exe", args={"q": "hi"})]
    )
    captured = {"called": False}

    async def never(*a, **kw):
        captured["called"] = True
        raise AssertionError("model must not be called when all tools failed")

    with patch("src.agents.executor._call_model", side_effect=never):
        env = await _execute_answerer(task, db_path=db)

    assert env.status == "failed"
    assert "BRT-TOOL-002" in (env.failure_reason or "")
    assert captured["called"] is False


@pytest.mark.asyncio
async def test_partial_tool_failure_still_calls_model(db):
    """TC-EXE-TOOL-004 — one tool fails, one succeeds → model IS called, both outcomes
    visible in the prompt."""
    task = _make_answerer_task(
        tool_requests=[
            ToolRequest(tool="tool_ok_exe", args={"q": "x"}),
            ToolRequest(tool="tool_fail_exe", args={"q": "y"}),
        ]
    )
    captured = {}
    payload = {"status": "resolved", "output": {"response": "ok"}}
    with _patch_model_call(captured, payload):
        env = await _execute_answerer(task, db_path=db)

    assert env.status == "resolved"
    # Both tool names appear in the prompt injection
    assert "tool_ok_exe" in captured["system_prompt"]
    assert "tool_fail_exe" in captured["system_prompt"]
