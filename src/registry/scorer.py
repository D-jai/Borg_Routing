"""Capability scorer — LLM-research-based model scoring.

research_models(model_ids, scoring_endpoint, scoring_model) sends a
web-search-enabled LLM one prompt per batch of 5 models and asks it to
assign relative 0-10 scores across seven task types. A rolling summary of
prior-batch scores is passed to each subsequent batch so the LLM can keep
ranking consistent across the whole set.

Replaces the retired benchmark subsystem. Unlike benchmark, the scorer does
NOT actually call the models under evaluation — it only researches public
information about them.

Error codes:
- BRT-REG-030: scorer LLM call failed (HTTP / timeout / request error)
- BRT-REG-031: scorer response could not be parsed into the expected JSON shape
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, Optional

import httpx

logger = logging.getLogger(__name__)

TASK_TYPES = [
    "reasoning",
    "coding",
    "summarize",
    "writing",
    "roleplay",
    "agentic",
    "extraction",
]

DEFAULT_BATCH_SIZE = 5
_TIMEOUT = 90.0


def _build_prompt(
    batch: list[str],
    rolling_summary: dict[str, dict[str, float]],
) -> str:
    """Build the scoring prompt for one batch.

    rolling_summary: {model_id: {task_type: score}} for already-scored batches.
    """
    prior_block = ""
    if rolling_summary:
        prior_lines = []
        for mid, scores in rolling_summary.items():
            score_str = ", ".join(f"{t}={scores.get(t, 5.0):.1f}" for t in TASK_TYPES)
            prior_lines.append(f"  {mid}: {score_str}")
        prior_block = (
            "You have already scored the following models in earlier batches. "
            "Preserve the relative ranking — if a new model is stronger than a "
            "previously scored one on a task, give it a higher score.\n\n"
            + "\n".join(prior_lines)
            + "\n\n"
        )

    batch_block = "\n".join(f"  - {mid}" for mid in batch)

    return (
        "You are a model-capability analyst with web search access. "
        "Research the following AI models using published benchmarks "
        "(MMLU, HumanEval, MT-Bench, LMArena, SWE-Bench, published model cards) "
        "and assign each a relative score on a 0-10 scale for each of these "
        f"seven task types: {', '.join(TASK_TYPES)}.\n\n"
        + prior_block
        + "Score these models now:\n"
        + batch_block
        + "\n\nReturn ONLY a JSON object of this shape (no markdown fences, "
        "no prose, no explanation):\n"
        "{\n"
        '  "<model_id>": {'
        + ", ".join(f'"{t}": <0-10>' for t in TASK_TYPES)
        + "},\n"
        "  ...\n"
        "}\n\n"
        "Guidelines:\n"
        "- 9-10 = frontier class for that task (top 5% of public models).\n"
        "- 7-8 = strong.\n"
        "- 5-6 = adequate.\n"
        "- 3-4 = weak.\n"
        "- 0-2 = unsuitable.\n"
        "- Scores are RELATIVE across this full scoring session — the "
        "absolute numbers matter less than the ordering.\n"
        "- Be decisive. Decimals are fine (7.3, 8.8) but do not cluster "
        "everything at 7.\n"
    )


async def _call_scoring_model(
    endpoint: str,
    model_name: str,
    prompt: str,
    api_key: Optional[str] = None,
) -> str:
    """Call the scoring LLM; return the raw response text.

    Raises:
        RuntimeError: BRT-REG-030 on any HTTP / network failure.
    """
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.1,
        "max_tokens": 2000,
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(endpoint, json=payload, headers=headers)
            resp.raise_for_status()
            data = resp.json()
    except httpx.HTTPError as exc:
        raise RuntimeError(
            f"BRT-REG-030: scoring LLM call failed ({type(exc).__name__}): {exc}"
        ) from exc

    try:
        return data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError(
            f"BRT-REG-030: scoring LLM returned unexpected response shape: {exc}"
        ) from exc


def _parse_scores(raw: str, expected_ids: list[str]) -> dict[str, dict[str, float]]:
    """Parse the scoring LLM response into {model_id: {task_type: score}}.

    Tolerant of markdown fences. Missing task types default to 5.0.

    Raises:
        ValueError: BRT-REG-031 if the response cannot be parsed as JSON.
    """
    text = raw.strip()

    # Strip markdown fences if present
    fence_match = re.search(r"```(?:json)?\s*(.+?)\s*```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1)

    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"BRT-REG-031: scorer response is not valid JSON — {exc}. "
            f"First 200 chars of response: {raw[:200]!r}"
        ) from exc

    if not isinstance(parsed, dict):
        raise ValueError(
            f"BRT-REG-031: scorer returned a {type(parsed).__name__}, expected a dict."
        )

    normalised: dict[str, dict[str, float]] = {}
    for mid in expected_ids:
        raw_entry = parsed.get(mid)
        if not isinstance(raw_entry, dict):
            # Graceful fallback: flat 5.0 across all task types
            logger.warning(
                "BRT-REG-031-partial: scorer did not return scores for '%s'; "
                "defaulting to 5.0 on all task types.", mid
            )
            normalised[mid] = {t: 5.0 for t in TASK_TYPES}
            continue
        scores: dict[str, float] = {}
        for t in TASK_TYPES:
            v = raw_entry.get(t, 5.0)
            try:
                score = float(v)
            except (TypeError, ValueError):
                score = 5.0
            scores[t] = max(0.0, min(10.0, score))
        normalised[mid] = scores
    return normalised


async def research_models(
    model_ids: list[str],
    scoring_endpoint: Optional[str] = None,
    scoring_model: Optional[str] = None,
    scoring_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, dict[str, float]]:
    """Research capability scores for a list of model IDs.

    Models are processed in batches. After each batch, a rolling summary of
    previous batches' scores is prepended to the next prompt so the LLM can
    maintain relative ordering across the whole session.

    Args:
        model_ids: Models to score (any source).
        scoring_endpoint: Full chat-completions URL for the scoring LLM.
            Falls back to SCORING_MODEL_ENDPOINT env var then OpenRouter default.
        scoring_model: Model name for the scoring call.
            Falls back to SCORING_MODEL env var, default 'perplexity/sonar-pro-search'.
        scoring_api_key: API key for the scoring endpoint.
            Falls back to OPENROUTER_API_KEY env var.
        batch_size: Models per LLM call. Default 5.

    Returns:
        {model_id: {task_type: score}} covering every input model_id.

    Raises:
        RuntimeError: BRT-REG-030 on LLM call failure.
        ValueError: BRT-REG-031 on unparseable response.
    """
    if not model_ids:
        return {}

    endpoint = (
        scoring_endpoint
        or os.getenv("SCORING_MODEL_ENDPOINT")
        or "https://openrouter.ai/api/v1/chat/completions"
    )
    model_name = (
        scoring_model
        or os.getenv("SCORING_MODEL")
        or "perplexity/sonar-pro-search"
    )
    api_key = scoring_api_key or os.getenv("OPENROUTER_API_KEY")

    rolling: dict[str, dict[str, float]] = {}

    for i in range(0, len(model_ids), batch_size):
        batch = model_ids[i : i + batch_size]
        prompt = _build_prompt(batch, rolling)
        raw = await _call_scoring_model(endpoint, model_name, prompt, api_key=api_key)
        scored = _parse_scores(raw, batch)
        rolling.update(scored)

    return rolling


async def research_and_apply(
    matrix: Any,  # CapabilityMatrix — late-bound to avoid a cyclic import at top level
    model_ids: list[str],
    scoring_endpoint: Optional[str] = None,
    scoring_model: Optional[str] = None,
    scoring_api_key: Optional[str] = None,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> dict[str, dict[str, float]]:
    """Run research_models() and write the results into the matrix.

    Unknown model_ids are skipped (logged) — the caller should have created
    rows beforehand. task_scores for every returned model are overwritten.

    Returns the scores that were written.
    """
    scored = await research_models(
        model_ids,
        scoring_endpoint=scoring_endpoint,
        scoring_model=scoring_model,
        scoring_api_key=scoring_api_key,
        batch_size=batch_size,
    )

    for mid, scores in scored.items():
        if matrix.get(mid) is None:
            logger.warning(
                "Scorer returned scores for unknown model '%s'; skipping write.",
                mid,
            )
            continue
        for task_type, score in scores.items():
            matrix.update_score(mid, task_type, score)

    return scored
