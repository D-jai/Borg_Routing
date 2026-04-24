"""Tool registry + dispatcher.

Tools are registered once at import time. The executor calls
``run_tool_requests`` with the WrappedTask's ``tool_requests`` list; all
requests run concurrently via ``asyncio.gather`` (BORG_ROUTE leaves the
concurrency bound to the caller — don't dispatch 200 search requests
accidentally).

The return shape is intentionally simple:

    {
      "<tool_name>": [ToolInvocationResult, ToolInvocationResult, ...]
    }

so a leaf with two ``web_search`` calls gets both results in order under
``tool_results["web_search"]``.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Iterable

from src.envelopes.schema import ToolRequest
from src.tools.base import BaseTool, ToolError, ToolInvocationResult

logger = logging.getLogger(__name__)


TOOL_REGISTRY: dict[str, BaseTool] = {}


def register_tool(tool: BaseTool) -> None:
    if not tool.name:
        raise ValueError("BRT-TOOL-000: tool must declare a non-empty 'name'")
    if tool.name in TOOL_REGISTRY:
        logger.debug("register_tool: replacing existing '%s'", tool.name)
    TOOL_REGISTRY[tool.name] = tool


def _lookup_or_raise(name: str) -> BaseTool:
    tool = TOOL_REGISTRY.get(name)
    if tool is None:
        raise ToolError("BRT-TOOL-000", f"unknown tool '{name}'")
    if not tool.is_available():
        raise ToolError(
            "BRT-TOOL-001",
            f"tool '{name}' has no runnable provider (check env vars)",
        )
    return tool


async def _run_one(req: ToolRequest) -> ToolInvocationResult:
    """Run a single request; ToolError becomes a failed result object."""
    try:
        tool = _lookup_or_raise(req.tool)
        return await tool.run(req.args or {})
    except ToolError as exc:
        return ToolInvocationResult(
            tool=req.tool,
            args=req.args or {},
            status="failed",
            error=str(exc),
        )
    except Exception as exc:  # pragma: no cover — defensive
        logger.exception("tool '%s' raised unexpectedly", req.tool)
        return ToolInvocationResult(
            tool=req.tool,
            args=req.args or {},
            status="failed",
            error=f"BRT-TOOL-002: unexpected tool error: {exc}",
        )


async def run_tool_requests(
    requests: Iterable[ToolRequest],
) -> dict[str, list[ToolInvocationResult]]:
    """Dispatch every request concurrently; group results by tool name."""
    reqs = list(requests)
    if not reqs:
        return {}
    outcomes = await asyncio.gather(*(_run_one(r) for r in reqs))
    grouped: dict[str, list[ToolInvocationResult]] = {}
    for outcome in outcomes:
        grouped.setdefault(outcome.tool, []).append(outcome)
    return grouped


# --------------------------------------------------------------------------
# Default registrations
# --------------------------------------------------------------------------
# Import + register web_search at module-load time. Kept here (not in
# __init__.py) so tests that want a clean registry can monkeypatch TOOL_REGISTRY
# without fighting import order.
from src.tools.web_search import WebSearchTool  # noqa: E402

register_tool(WebSearchTool())
