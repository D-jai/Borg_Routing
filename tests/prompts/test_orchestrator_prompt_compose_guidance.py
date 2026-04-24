"""Tests for BRT-PR-01a prompt content — guardrails against accidental
deletion of the compose-vs-synthesis guidance and the answerer depends_on
slot in the node template.

TC-PR-001 — compose-mode synthesis guidance block is present.
TC-PR-001 — answerer template includes a depends_on field.
"""
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT


def test_prompt_has_compose_synthesis_guidance():
    """TC-PR-001 — the orchestrator knows to synthesise in compose mode
    rather than dispatching an unwired 'final recommendation' answerer."""
    assert "Synthesising Fan-Out" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "stage_results" in ORCHESTRATOR_SYSTEM_PROMPT
    assert "compose" in ORCHESTRATOR_SYSTEM_PROMPT.lower()
    # The exact anti-pattern warning must be present.
    assert "insufficient context" in ORCHESTRATOR_SYSTEM_PROMPT.lower() or \
        "no access to their outputs" in ORCHESTRATOR_SYSTEM_PROMPT.lower()


def test_answerer_template_has_depends_on():
    """TC-PR-001 — answerer node template includes depends_on so the
    orchestrator can emit explicit dependencies."""
    # Find the answerer section and check depends_on appears in the block
    idx = ORCHESTRATOR_SYSTEM_PROMPT.find("### 1. `answerer`")
    assert idx >= 0, "answerer section not found in prompt"
    # Next section heading starts with "### 2."
    next_idx = ORCHESTRATOR_SYSTEM_PROMPT.find("### 2.", idx)
    assert next_idx > idx
    answerer_block = ORCHESTRATOR_SYSTEM_PROMPT[idx:next_idx]
    assert "depends_on" in answerer_block, \
        "depends_on not advertised in answerer template"
