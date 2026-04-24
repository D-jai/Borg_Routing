"""BORG_ROUTE tool dispatcher — executor-mediated, not model-native.

The executor runs every tool BEFORE the LLM call; results land in
``dependency_input.tool_results[<tool_name>]`` (a list of per-invocation
results in emission order). No in-model tool-call loops. See BRT-WEB-01.
"""
from src.tools.base import BaseTool, ToolError, ToolInvocationResult
from src.tools.registry import (
    TOOL_REGISTRY,
    register_tool,
    run_tool_requests,
)

__all__ = [
    "BaseTool",
    "ToolError",
    "ToolInvocationResult",
    "TOOL_REGISTRY",
    "register_tool",
    "run_tool_requests",
]
