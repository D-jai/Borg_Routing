"""TC-ENV-006 — WrappedTask.tool_requests + ToolRequest schema.

Validates that:
- WrappedTask defaults tool_requests to [].
- WrappedTask accepts a list of ToolRequest objects.
- ToolRequest rejects empty tool names with BRT-ENV-006.
- validate_outbound raises BRT-TOOL-000 on unknown tool names.
"""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.envelopes.schema import ToolRequest, WrappedTask
from src.envelopes.validator import validate_outbound


def _make_task(**overrides) -> WrappedTask:
    defaults = dict(
        envelope_id=uuid4(),
        run_id=uuid4(),
        subtask_id=uuid4(),
        type="answerer",
        current_depth=0,
        max_depth=5,
        level_path="root",
        instruction="test instruction",
        output_schema={},
    )
    defaults.update(overrides)
    return WrappedTask(**defaults)


def test_tool_requests_default_empty():
    task = _make_task()
    assert task.tool_requests == []


def test_tool_requests_accepts_valid_entries():
    reqs = [
        ToolRequest(tool="web_search", args={"query": "hi"}),
        ToolRequest(tool="web_search", args={"query": "bye", "max_results": 3}),
    ]
    task = _make_task(tool_requests=reqs)
    assert len(task.tool_requests) == 2
    assert task.tool_requests[0].tool == "web_search"
    assert task.tool_requests[1].args["max_results"] == 3


def test_tool_request_rejects_empty_tool():
    with pytest.raises(ValidationError) as exc:
        ToolRequest(tool="  ")
    assert "BRT-ENV-006" in str(exc.value)


def test_validate_outbound_rejects_unknown_tool_with_bright_tool_000():
    task = _make_task(
        tool_requests=[ToolRequest(tool="definitely_not_registered")]
    )
    with pytest.raises(ValueError) as exc:
        validate_outbound(task)
    assert "BRT-TOOL-000" in str(exc.value)


def test_validate_outbound_accepts_registered_tool():
    # web_search is registered by src/tools/registry.py at import time.
    task = _make_task(
        tool_requests=[ToolRequest(tool="web_search", args={"query": "x"})]
    )
    # No raise expected.
    validate_outbound(task)


def test_tool_request_strips_whitespace_from_tool_name():
    req = ToolRequest(tool="  web_search  ")
    assert req.tool == "web_search"
