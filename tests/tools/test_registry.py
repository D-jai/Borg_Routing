"""TC-TOOL-REG-001..005 — tool registry, dispatcher, error-code mapping."""
import asyncio

import pytest

from src.envelopes.schema import ToolRequest
from src.tools.base import BaseTool, ToolError, ToolInvocationResult
from src.tools.registry import TOOL_REGISTRY, register_tool, run_tool_requests


class _StubOk(BaseTool):
    name = "stub_ok"

    async def run(self, args: dict) -> ToolInvocationResult:
        return ToolInvocationResult(
            tool=self.name, args=args, status="ok", result={"echo": args}
        )


class _StubUnavailable(BaseTool):
    name = "stub_unavail"

    def is_available(self) -> bool:
        return False

    async def run(self, args: dict) -> ToolInvocationResult:  # pragma: no cover
        raise AssertionError("should not be called when unavailable")


class _StubFails(BaseTool):
    name = "stub_fails"

    async def run(self, args: dict) -> ToolInvocationResult:
        raise ToolError("BRT-TOOL-002", "simulated failure")


@pytest.fixture(autouse=True)
def _register_stubs():
    original = dict(TOOL_REGISTRY)
    register_tool(_StubOk())
    register_tool(_StubUnavailable())
    register_tool(_StubFails())
    yield
    TOOL_REGISTRY.clear()
    TOOL_REGISTRY.update(original)


@pytest.mark.asyncio
async def test_single_request_ok():
    """TC-TOOL-REG-001 — one request → one grouped entry."""
    out = await run_tool_requests([ToolRequest(tool="stub_ok", args={"q": "hi"})])
    assert list(out.keys()) == ["stub_ok"]
    assert len(out["stub_ok"]) == 1
    assert out["stub_ok"][0].status == "ok"
    assert out["stub_ok"][0].result == {"echo": {"q": "hi"}}


@pytest.mark.asyncio
async def test_multiple_requests_run_in_parallel_and_preserve_order():
    """TC-TOOL-REG-002 — three requests → three results under same key, emission order preserved."""
    reqs = [
        ToolRequest(tool="stub_ok", args={"i": 1}),
        ToolRequest(tool="stub_ok", args={"i": 2}),
        ToolRequest(tool="stub_ok", args={"i": 3}),
    ]
    out = await run_tool_requests(reqs)
    assert len(out["stub_ok"]) == 3
    assert [r.result["echo"]["i"] for r in out["stub_ok"]] == [1, 2, 3]


@pytest.mark.asyncio
async def test_unknown_tool_returns_failed_result_not_raise():
    """TC-TOOL-REG-003 — unknown tool → failed ToolInvocationResult with BRT-TOOL-000 (not raise)."""
    out = await run_tool_requests([ToolRequest(tool="totally_unknown")])
    assert out["totally_unknown"][0].status == "failed"
    assert "BRT-TOOL-000" in out["totally_unknown"][0].error


@pytest.mark.asyncio
async def test_unavailable_tool_returns_bright_tool_001():
    """TC-TOOL-REG-004 — registered but is_available()=False → BRT-TOOL-001."""
    out = await run_tool_requests([ToolRequest(tool="stub_unavail")])
    assert out["stub_unavail"][0].status == "failed"
    assert "BRT-TOOL-001" in out["stub_unavail"][0].error


@pytest.mark.asyncio
async def test_failing_tool_surfaced_as_failed_result():
    """TC-TOOL-REG-005 — tool raising ToolError becomes a failed result, not an exception."""
    out = await run_tool_requests([ToolRequest(tool="stub_fails")])
    assert out["stub_fails"][0].status == "failed"
    assert "BRT-TOOL-002" in out["stub_fails"][0].error


@pytest.mark.asyncio
async def test_empty_requests_list_returns_empty_dict():
    """TC-TOOL-REG-006 — empty requests list short-circuits to {}."""
    out = await run_tool_requests([])
    assert out == {}


def test_register_tool_rejects_empty_name():
    """TC-TOOL-REG-007 — register_tool raises ValueError on blank name."""

    class _NoName(BaseTool):
        name = ""

        async def run(self, args: dict) -> ToolInvocationResult:  # pragma: no cover
            return ToolInvocationResult(tool="", args=args, status="ok")

    with pytest.raises(ValueError):
        register_tool(_NoName())
