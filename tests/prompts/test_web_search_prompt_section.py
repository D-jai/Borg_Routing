"""TC-PR-WEB-001..003 — orchestrator prompt teaches web_search correctly.

Regression guard: the Phase-2 prompt fix (BRT-PR-01a) taught depends_on
synthesis; Phase 4 adds the web_search section. Any future edit that drops
either section should fail these tests before it ships.
"""
import re

from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT


def _normalise(text: str) -> str:
    """Collapse whitespace so phrase assertions survive line-wrapping."""
    return re.sub(r"\s+", " ", text)


def test_prompt_contains_web_search_section():
    """TC-PR-WEB-001 — dedicated heading + tool name appear verbatim."""
    assert "Researching With `web_search`" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "tool_requests" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "\"tool\": \"web_search\"" in ORCHESTRATOR_SYSTEM_PROMPT


def test_prompt_teaches_broad_plus_narrow_heuristic():
    """TC-PR-WEB-002 — multi-search protocol is explicit (broad + narrow pair)."""
    body = _normalise(ORCHESTRATOR_SYSTEM_PROMPT)
    assert "broad" in body.lower()
    assert "narrow" in body.lower()
    assert "two independent search nodes in parallel" in body


def test_prompt_documents_tool_failure_semantics():
    """TC-PR-WEB-003 — BRT-TOOL-DEGRADED + BRT-TOOL-002 are mentioned so the
    orchestrator knows what to do when a search fails."""
    assert "BRT-TOOL-DEGRADED" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "BRT-TOOL-002" in ORCHESTRATOR_SYSTEM_PROMPT


def test_prompt_still_teaches_depends_on_synthesis():
    """TC-PR-WEB-004 — Phase 2 depends_on section must still be present
    (regression guard; adding web_search must not remove it)."""
    assert "Synthesising Fan-Out" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "depends_on" in ORCHESTRATOR_SYSTEM_PROMPT
