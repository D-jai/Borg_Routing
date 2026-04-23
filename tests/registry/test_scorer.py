"""Tests for src/registry/scorer.py.

TC-REG-050 — _parse_scores returns normalised dict from valid JSON
TC-REG-051 — _parse_scores strips markdown fences
TC-REG-052 — _parse_scores clamps scores to [0, 10] and defaults missing tasks to 5.0
TC-REG-053 — _parse_scores raises BRT-REG-031 on invalid JSON
TC-REG-054 — research_models batches inputs and feeds rolling summary to subsequent calls
TC-REG-055 — research_and_apply writes scores into the matrix
TC-REG-056 — research_models raises BRT-REG-030 on HTTP failure
"""
import json

import httpx
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo
from src.registry.scorer import (
    TASK_TYPES,
    _build_prompt,
    _parse_scores,
    research_and_apply,
    research_models,
)


SEVEN = dict.fromkeys(TASK_TYPES, 7.0)


def _scores_json(model_to_scores: dict[str, dict[str, float]]) -> str:
    return json.dumps(model_to_scores)


def _openrouter_response(content: str) -> MagicMock:
    resp = MagicMock()
    resp.status_code = 200
    resp.raise_for_status = MagicMock()
    resp.json.return_value = {
        "choices": [{"message": {"content": content}}]
    }
    return resp


def test_parse_scores_valid_json():
    """TC-REG-050 — normalised output for a well-formed response."""
    raw = _scores_json({"m/a": SEVEN, "m/b": SEVEN})
    out = _parse_scores(raw, ["m/a", "m/b"])
    assert set(out.keys()) == {"m/a", "m/b"}
    for mid in out:
        assert set(out[mid].keys()) == set(TASK_TYPES)
        assert all(v == 7.0 for v in out[mid].values())


def test_parse_scores_strips_markdown_fences():
    """TC-REG-051 — tolerant of ```json ... ``` wrapping."""
    inner = _scores_json({"m/a": SEVEN})
    raw = f"```json\n{inner}\n```"
    out = _parse_scores(raw, ["m/a"])
    assert out["m/a"]["coding"] == 7.0


def test_parse_scores_clamps_and_defaults():
    """TC-REG-052 — out-of-range scores clamped; missing task_types default to 5.0."""
    raw = json.dumps({
        "m/a": {"reasoning": 15, "coding": -3},  # coding missing most types
    })
    out = _parse_scores(raw, ["m/a"])
    assert out["m/a"]["reasoning"] == 10.0
    assert out["m/a"]["coding"] == 0.0
    assert out["m/a"]["writing"] == 5.0  # defaulted


def test_parse_scores_invalid_json_raises_brt_reg_031():
    """TC-REG-053 — malformed JSON raises BRT-REG-031."""
    with pytest.raises(ValueError) as exc_info:
        _parse_scores("not json at all", ["m/a"])
    assert "BRT-REG-031" in str(exc_info.value)


@pytest.mark.asyncio
async def test_research_models_batches_with_rolling_summary():
    """TC-REG-054 — six models run in 2 batches; prior scores flow to the next prompt."""
    ids = [f"m/{i}" for i in range(6)]

    # First call returns scores for batch 1 (first 5 ids)
    # Second call returns scores for batch 2 (last 1 id)
    first_payload = _scores_json({mid: SEVEN for mid in ids[:5]})
    second_payload = _scores_json({ids[5]: SEVEN})

    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=[
            _openrouter_response(first_payload),
            _openrouter_response(second_payload),
        ])
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        out = await research_models(
            ids,
            scoring_endpoint="https://example/api/chat/completions",
            scoring_model="test-model",
            batch_size=5,
        )

    assert set(out.keys()) == set(ids)
    # Two POST calls — one per batch
    assert mock_client.post.await_count == 2
    # The second call's prompt must reference at least one previously scored model
    second_call_args = mock_client.post.await_args_list[1]
    body = second_call_args.kwargs["json"]
    prompt_text = body["messages"][0]["content"]
    assert "m/0" in prompt_text, "rolling summary should surface previous-batch model IDs"


@pytest.mark.asyncio
async def test_research_and_apply_writes_to_matrix():
    """TC-REG-055 — research_and_apply mutates the matrix via update_score."""
    cm = CapabilityMatrix("data/capability_matrix.yaml")
    cm.load()
    target_id = cm.all()[0].model_id
    # Baseline score to overwrite
    cm.update_score(target_id, "coding", 5.0)
    assert cm.get(target_id).task_scores["coding"] == 5.0

    async def fake_research(model_ids, **kwargs):
        return {target_id: dict.fromkeys(TASK_TYPES, 8.5)}

    with patch("src.registry.scorer.research_models", new=fake_research):
        out = await research_and_apply(cm, [target_id])

    assert out[target_id]["coding"] == 8.5
    assert cm.get(target_id).task_scores["coding"] == 8.5


@pytest.mark.asyncio
async def test_research_models_http_failure_raises_brt_reg_030():
    """TC-REG-056 — HTTP error during scorer call raises BRT-REG-030."""
    with patch("httpx.AsyncClient") as mock_client_cls:
        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.ConnectError("refused"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)
        mock_client_cls.return_value = mock_client

        with pytest.raises(RuntimeError) as exc_info:
            await research_models(
                ["m/a"],
                scoring_endpoint="https://example/api/chat/completions",
                scoring_model="test-model",
            )

    assert "BRT-REG-030" in str(exc_info.value)


def test_build_prompt_without_rolling_summary():
    """Prompt for the first batch should not contain an 'already scored' block."""
    prompt = _build_prompt(["m/a", "m/b"], {})
    assert "already scored" not in prompt
    assert "m/a" in prompt and "m/b" in prompt


def test_build_prompt_with_rolling_summary():
    """Prompt for later batches should include prior-batch scores."""
    prev = {"m/prev": dict.fromkeys(TASK_TYPES, 6.2)}
    prompt = _build_prompt(["m/new"], prev)
    assert "already scored" in prompt
    assert "m/prev" in prompt
    assert "m/new" in prompt
