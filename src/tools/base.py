"""Abstract BaseTool + invocation result for the tool dispatcher (BRT-WEB-01).

Tools are registered once at import time in src/tools/registry.py. The
executor calls ``run_tool_requests`` with the WrappedTask's ``tool_requests``
list BEFORE dispatching to the answerer model; the aggregated results are
injected into ``dependency_input.tool_results``.

Error codes:
- BRT-TOOL-000: unknown tool name (caught in validate_outbound).
- BRT-TOOL-001: tool is registered but not runnable in this environment
                (typically missing API key for every configured provider).
- BRT-TOOL-002: tool runtime call failed after all providers exhausted.
- BRT-TOOL-DEGRADED: tool ran but fell back to a secondary provider
                     (primary down; informational — run continues).
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional


class ToolError(ValueError):
    """Raised by a tool when it cannot produce a usable result.

    The ``code`` attribute carries the canonical BRT-TOOL-00x identifier so
    the executor / validator can branch on it without string parsing.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


@dataclass
class ToolInvocationResult:
    """A single run of a tool. Success, degraded, or failure."""

    tool: str
    args: dict
    status: str  # "ok" | "degraded" | "failed"
    result: Any = None
    provider: Optional[str] = None  # which sub-provider actually answered
    error: Optional[str] = None      # BRT-TOOL-00x code + message on failure
    latency_ms: int = 0
    extras: dict = field(default_factory=dict)

    def to_public_dict(self) -> dict:
        """Strip internal-only fields so this can be fed to the model safely."""
        return {
            "tool": self.tool,
            "args": self.args,
            "status": self.status,
            "result": self.result,
            "provider": self.provider,
            "error": self.error,
        }


class BaseTool(ABC):
    """Any tool the executor can invoke on behalf of an answerer."""

    name: str = ""

    @abstractmethod
    async def run(self, args: dict) -> ToolInvocationResult:
        """Invoke the tool with the given args.

        Implementations MUST either return a ToolInvocationResult (any
        ``status``) or raise ToolError. Never raise a bare exception.
        """

    def is_available(self) -> bool:
        """Return True when at least one provider can run. Override per tool."""
        return True
