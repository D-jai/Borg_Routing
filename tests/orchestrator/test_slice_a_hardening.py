"""
Slice A hardening tests — install-fix-03.

TC-JSON-010 — _extract_json tolerates <thinking>...</thinking> wrappers
              (reasoning-family models leak private chain-of-thought
              even when response_format=json_object is set).

TC-JSON-011 — _extract_json tolerates multiple tag variants
              (<think>, <reasoning>, case variation, attributes).

TC-RM-010  — _is_reasoning_model detects o1/o3, DeepSeek-R1, Qwen-QwQ,
             and specialty_tags=['reasoning']; returns False for
             conventional model ids.

TC-RM-011  — _resolve_max_tokens_for_model returns >= 8000 for reasoning
             models and the env-configured default for non-reasoning.

TC-WEB-020 — InboundEnvelope carries tool_results; the compose prompt
             surfaces them as an aggregated block.

TC-PROMPT-010 — Compose formatting + delegation-discipline clauses
                are present in ORCHESTRATOR_SYSTEM_PROMPT.
"""
import os
from unittest.mock import patch
from uuid import uuid4

import pytest

from src.envelopes.schema import InboundEnvelope
from src.orchestrator.engine import (
    _extract_json,
    _is_reasoning_model,
    _resolve_max_tokens_for_model,
    _strip_reasoning_wrappers,
)
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from src.registry.models import ModelInfo


# ---------- Fix #3 — tolerant JSON extractor ----------

def test_extract_json_strips_thinking_tag():
    """TC-JSON-010 — DeepSeek-R1-style <thinking> wrapper removed."""
    raw = (
        "<thinking>The user wants me to do X. I'll plan...</thinking>\n"
        '{"direct_answer": "hello", "plan": null, "rationale": "greeting"}'
    )
    parsed = _extract_json(raw)
    assert parsed["direct_answer"] == "hello"
    assert parsed["plan"] is None


def test_extract_json_strips_think_variants():
    """TC-JSON-011 — <think>/<reasoning>/<analysis> all stripped."""
    for tag in ("think", "reasoning", "analysis", "scratchpad"):
        raw = f"<{tag}>private {tag}...</{tag}>\n{{\"ok\": true}}"
        assert _extract_json(raw) == {"ok": True}


def test_extract_json_case_insensitive_tag():
    """TC-JSON-011b — uppercase tag variation still stripped."""
    raw = '<THINKING>leak</THINKING>\n{"ok": true}'
    assert _extract_json(raw) == {"ok": True}


def test_extract_json_tag_then_fence():
    """TC-JSON-011c — thinking wrapper + fenced JSON code block."""
    raw = (
        "<thinking>let me think...</thinking>\n"
        "```json\n"
        '{"action": "compose", "next_nodes": null}\n'
        "```"
    )
    parsed = _extract_json(raw)
    assert parsed["action"] == "compose"


def test_extract_json_still_raises_on_garbage():
    """TC-JSON-011d — nothing recoverable still raises BRT-ORC-002."""
    with pytest.raises(ValueError, match="BRT-ORC-002"):
        _extract_json("<thinking>just thinking, no json at all</thinking>")


def test_strip_reasoning_wrappers_leaves_plain_text_alone():
    """TC-JSON-011e — non-reasoning text passes through unchanged."""
    plain = '{"just": "json"}'
    assert _strip_reasoning_wrappers(plain) == plain


# ---------- Fix #1 — reasoning-model token budget ----------

def _mk(model_id: str, *, specialty_tags=None) -> ModelInfo:
    return ModelInfo(
        model_id=model_id,
        source="openrouter",
        context_length=128000,
        specialty_tags=list(specialty_tags or []),
    )


def test_is_reasoning_model_detects_deepseek_r1():
    """TC-RM-010a — DeepSeek-R1 family matched."""
    assert _is_reasoning_model(_mk("deepseek/deepseek-r1"))
    assert _is_reasoning_model(_mk("deepseek-r1-distill-qwen-32b"))


def test_is_reasoning_model_detects_openai_o_series():
    """TC-RM-010b — o1/o3/o4 matched."""
    assert _is_reasoning_model(_mk("openai/o1"))
    assert _is_reasoning_model(_mk("openai/o3-mini"))
    assert _is_reasoning_model(_mk("o4-preview"))


def test_is_reasoning_model_detects_qwen_qwq():
    """TC-RM-010c — Qwen-QwQ matched."""
    assert _is_reasoning_model(_mk("qwen/qwq-32b-preview"))
    assert _is_reasoning_model(_mk("Qwen-QwQ-32B"))


def test_is_reasoning_model_honours_specialty_tag():
    """TC-RM-010d — explicit specialty_tags=['reasoning'] forces True."""
    assert _is_reasoning_model(_mk("some/obscure-model", specialty_tags=["reasoning"]))


def test_is_reasoning_model_false_for_plain_models():
    """TC-RM-010e — conventional chat models are not flagged."""
    assert not _is_reasoning_model(_mk("openai/gpt-4o"))
    assert not _is_reasoning_model(_mk("anthropic/claude-3-5-sonnet"))
    assert not _is_reasoning_model(_mk("google/gemini-2-5-pro"))
    assert not _is_reasoning_model(_mk("meta-llama/llama-3-70b"))


def test_resolve_max_tokens_bumps_for_reasoning(monkeypatch):
    """TC-RM-011a — reasoning model gets >= 8000 tokens regardless of env."""
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "2000")
    assert _resolve_max_tokens_for_model(
        _mk("deepseek/deepseek-r1"), "ORCHESTRATOR_MAX_TOKENS"
    ) >= 8000


def test_resolve_max_tokens_keeps_default_for_plain(monkeypatch):
    """TC-RM-011b — non-reasoning model gets the env default."""
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "2000")
    assert _resolve_max_tokens_for_model(
        _mk("openai/gpt-4o"), "ORCHESTRATOR_MAX_TOKENS"
    ) == 2000


def test_resolve_max_tokens_respects_large_env(monkeypatch):
    """TC-RM-011c — operator raising env above 2000 still flows through."""
    monkeypatch.setenv("ORCHESTRATOR_MAX_TOKENS", "16000")
    # Plain model: take env value (16000).
    assert _resolve_max_tokens_for_model(
        _mk("openai/gpt-4o"), "ORCHESTRATOR_MAX_TOKENS"
    ) == 16000
    # Reasoning model: max(env*4, 8000) = 64000.
    assert _resolve_max_tokens_for_model(
        _mk("deepseek/deepseek-r1"), "ORCHESTRATOR_MAX_TOKENS"
    ) == 64000


# ---------- Fix #4 — tool_results survive fold-in ----------

def test_inbound_envelope_carries_tool_results():
    """TC-WEB-020a — InboundEnvelope accepts and stores tool_results."""
    env = InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="resolved",
        output={"summary": "dropped the citations"},
        tool_results={
            "web_search": [
                {"title": "Big News", "url": "https://example.com/news",
                 "snippet": "it happened today."}
            ]
        },
    )
    assert env.tool_results["web_search"][0]["url"] == "https://example.com/news"


def test_inbound_envelope_tool_results_defaults_to_empty():
    """TC-WEB-020b — envelopes without tools carry an empty dict, not None."""
    env = InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="resolved",
        output={"answer": "no tools needed"},
    )
    assert env.tool_results == {}


# ---------- Fix #5 + Fix #2 — prompt clauses ----------

def test_compose_formatting_clause_present():
    """TC-PROMPT-010a — markdown formatting discipline exists in the prompt."""
    p = ORCHESTRATOR_SYSTEM_PROMPT
    assert "formatting discipline" in p.lower()
    assert "paragraphs" in p.lower()
    assert "```" in p  # reference to fenced code blocks
    assert "bold" in p.lower() or "**bold**" in p


def test_delegation_override_clause_present():
    """TC-PROMPT-010b — Rule Zero Override block present; covers live-data."""
    p = ORCHESTRATOR_SYSTEM_PROMPT
    assert "Rule Zero Override" in p
    assert "current / time-sensitive" in p.lower() or "time-sensitive" in p.lower()
    assert "web_search" in p
    assert "bluffing" in p.lower() or "bluff" in p.lower()


def test_tool_results_placeholder_in_prompt():
    """TC-PROMPT-010c — {tool_results} placeholder is wired into the template."""
    assert "{tool_results}" in ORCHESTRATOR_SYSTEM_PROMPT


def test_compose_evidence_discipline_clause_present():
    """TC-PROMPT-010d — composer is told to ground in evidence, not bluff."""
    p = ORCHESTRATOR_SYSTEM_PROMPT
    assert "evidence discipline" in p.lower()
    assert "grounded" in p.lower() or "grounded in" in p.lower()


# ---------- Slice A.5 — Adaptive Internal Reasoning ----------

def test_adaptive_reasoning_clause_present():
    """TC-PROMPT-011a — model self-decides number of internal passes;
    Trivial / Moderate / Complex tiers are explicit."""
    p = ORCHESTRATOR_SYSTEM_PROMPT
    assert "Adaptive Internal Reasoning" in p
    # All three complexity tiers must be named.
    assert "Trivial" in p
    assert "Moderate" in p
    assert "Complex" in p
    # The model — not a hardcoded loop — chooses depth.
    assert "you — not a hardcoded loop — decide" in p.lower() \
        or "you, not a hardcoded loop, decide" in p.lower()


def test_adaptive_reasoning_no_chain_leak_clause_present():
    """TC-PROMPT-011b — internal reasoning must not leak into the JSON
    response (model uses it privately, only rationale gets a hint)."""
    p = ORCHESTRATOR_SYSTEM_PROMPT
    assert "do NOT emit your reasoning" in p.lower() \
        or "do not emit your reasoning" in p.lower()
    assert "verified via internal review" in p.lower() \
        or "internal review" in p.lower()


def test_adaptive_reasoning_escalation_clause_present():
    """TC-PROMPT-011c — escalation path: spawn an `orchestrate` node with
    `recommended_model` set to a frontier model."""
    p = ORCHESTRATOR_SYSTEM_PROMPT
    assert "Escalation" in p
    assert "frontier" in p.lower()
    assert "recommended_model" in p
