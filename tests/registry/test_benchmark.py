"""Tests for src/registry/benchmark.py (mocked httpx + model calls).

TC-REG-050 — _load_prompts loads benchmark_prompts.yaml with all task_types
TC-REG-051 — _load_prompts raises BRT-REG-011 on missing file
TC-REG-052 — benchmark_model returns score, throughput_tps, elapsed_sec
TC-REG-053 — benchmark_model score averaging across prompts
TC-REG-054 — benchmark_model raises BRT-REG-015 when model has no endpoint
TC-REG-055 — benchmark_all updates matrix scores and throughput
"""
import pytest
from unittest.mock import AsyncMock, patch

from src.registry.benchmark import _load_prompts, benchmark_model, benchmark_all
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo


# ---------------------------------------------------------------------------
# _load_prompts
# ---------------------------------------------------------------------------

def test_load_prompts_seed_file():
    """TC-REG-050 — loads real benchmark_prompts.yaml; all task_types present."""
    prompts = _load_prompts("data/benchmark_prompts.yaml")
    expected_types = {"reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction"}
    assert expected_types.issubset(set(prompts.keys()))
    for tt, items in prompts.items():
        assert len(items) == 5, f"{tt} should have 5 prompts, got {len(items)}"
        for item in items:
            assert "prompt" in item
            assert "expected_shape" in item


def test_load_prompts_missing_file():
    """TC-REG-051 — BRT-REG-011 raised on missing file."""
    with pytest.raises(FileNotFoundError) as exc_info:
        _load_prompts("data/does_not_exist.yaml")
    assert "BRT-REG-011" in str(exc_info.value)


# ---------------------------------------------------------------------------
# benchmark_model
# ---------------------------------------------------------------------------

def _make_model_with_endpoint():
    return ModelInfo(
        model_id="lmstudio/test-bench",
        source="lmstudio",
        context_length=8192,
        endpoint="http://localhost:1234",
    )


def _model_call_side_effect(url, json=None, **kwargs):
    """Fake model response."""
    resp = AsyncMock()
    resp.status_code = 200
    resp.json.return_value = {
        "choices": [{"message": {"content": "Test answer."}}],
        "usage": {"completion_tokens": 10},
    }
    resp.raise_for_status = AsyncMock()
    return resp


@pytest.mark.asyncio
async def test_benchmark_model_returns_shape():
    """TC-REG-052 — benchmark_model returns score, throughput_tps, elapsed_sec."""
    model = _make_model_with_endpoint()
    prompts = [
        {"prompt": "What is 2+2?", "expected_shape": "4"},
        {"prompt": "Capital of France?", "expected_shape": "Paris"},
    ]

    with patch("src.registry.benchmark._call_model", new_callable=AsyncMock) as mock_call, \
         patch("src.registry.benchmark._judge_response", new_callable=AsyncMock) as mock_judge:

        mock_call.return_value = ("Answer text", 15)
        mock_judge.return_value = 8.5

        result = await benchmark_model(model, "reasoning", prompts)

    assert "score" in result
    assert "throughput_tps" in result
    assert "elapsed_sec" in result
    assert result["score"] == pytest.approx(8.5)
    assert result["throughput_tps"] > 0


@pytest.mark.asyncio
async def test_benchmark_model_score_averaging():
    """TC-REG-053 — score is averaged across all prompts."""
    model = _make_model_with_endpoint()
    prompts = [
        {"prompt": "Q1", "expected_shape": "A1"},
        {"prompt": "Q2", "expected_shape": "A2"},
        {"prompt": "Q3", "expected_shape": "A3"},
    ]

    scores = [7.0, 9.0, 8.0]
    call_count = {"n": 0}

    async def fake_judge(*args, **kwargs):
        idx = call_count["n"]
        call_count["n"] += 1
        return scores[idx]

    with patch("src.registry.benchmark._call_model", new_callable=AsyncMock) as mock_call, \
         patch("src.registry.benchmark._judge_response", new=fake_judge):

        mock_call.return_value = ("Answer", 10)
        result = await benchmark_model(model, "reasoning", prompts)

    assert result["score"] == pytest.approx(8.0)  # (7+9+8)/3


@pytest.mark.asyncio
async def test_benchmark_model_no_endpoint_raises():
    """TC-REG-054 — BRT-REG-015 raised when model has no endpoint."""
    model = ModelInfo(
        model_id="openrouter/some-model",
        source="openrouter",
        context_length=4096,
    )
    with pytest.raises(RuntimeError) as exc_info:
        await benchmark_model(model, "coding", [{"prompt": "x", "expected_shape": "y"}])
    assert "BRT-REG-015" in str(exc_info.value)


# ---------------------------------------------------------------------------
# benchmark_all
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_benchmark_all_updates_matrix():
    """TC-REG-055 — benchmark_all updates matrix scores and throughput."""
    cm = CapabilityMatrix("data/capability_matrix.yaml")
    cm.load()

    # Only test with the first local model that has an endpoint
    local_models = [m for m in cm.all() if m.endpoint and m.source == "lmstudio"]
    if not local_models:
        pytest.skip("No local model with endpoint in matrix")

    test_model = local_models[0]

    with patch("src.registry.benchmark.benchmark_model", new_callable=AsyncMock) as mock_bm:
        mock_bm.return_value = {"score": 7.5, "throughput_tps": 25.0, "elapsed_sec": 3.0}

        results = await benchmark_all(cm, task_types=["coding"])

    # The model should have been updated in the matrix
    updated = cm.get(test_model.model_id)
    assert updated is not None
    # Score and throughput should be updated
    assert updated.task_scores.get("coding") == pytest.approx(7.5)
    assert updated.throughput_tps == pytest.approx(25.0)
