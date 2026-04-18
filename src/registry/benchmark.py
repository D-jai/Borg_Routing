"""Model benchmarking — quality score + throughput measurement.

benchmark_model() calls a model N times (one per prompt for the task_type),
times each call, scores the output via a heavy judge model, and returns
aggregated quality score + throughput_tps.

benchmark_all() runs benchmark_model() for every model in the matrix across
all requested task types.

Error codes:
- BRT-REG-011: benchmark_prompts.yaml not found or malformed
- BRT-REG-013: computed throughput_tps is zero/negative (impossible timing)
- BRT-REG-014: judge model call failed
- BRT-REG-015: model under test did not respond
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Optional

import httpx
import yaml

from .capability_matrix import CapabilityMatrix
from .models import ModelInfo

logger = logging.getLogger(__name__)

_JUDGE_MODEL_ENV_KEY = "BENCHMARK_JUDGE_ENDPOINT"
_DEFAULT_JUDGE_ENDPOINT = "http://localhost:1234/v1/chat/completions"
_DEFAULT_JUDGE_MODEL = "auto"
_TIMEOUT = 60.0


def _load_prompts(path: str = "data/benchmark_prompts.yaml") -> dict[str, list[dict]]:
    """Load benchmark prompts keyed by task_type.

    Returns:
        dict mapping task_type -> list of {prompt, expected_shape} dicts.

    Raises:
        FileNotFoundError: BRT-REG-011 if file missing.
        ValueError: BRT-REG-011 if file malformed.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"BRT-REG-011: benchmark_prompts.yaml not found at '{path}'."
        )
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"BRT-REG-011: benchmark_prompts.yaml parse error — {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError("BRT-REG-011: benchmark_prompts.yaml must be a list of task_type sections.")

    by_type: dict[str, list[dict]] = {}
    for section in raw:
        task_type = section.get("task_type", "")
        prompts = section.get("prompts", [])
        if task_type:
            by_type[task_type] = prompts
    return by_type


async def _call_model(
    endpoint: str,
    model_name: str,
    prompt: str,
    timeout: float = _TIMEOUT,
) -> tuple[str, int]:
    """Call a model and return (response_text, output_tokens).

    Raises:
        httpx.HTTPError: BRT-REG-015 if model not reachable.
    """
    payload = {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 512,
        "temperature": 0,
    }
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(endpoint, json=payload)
        resp.raise_for_status()
        data = resp.json()

    choice = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    tokens_out = usage.get("completion_tokens", len(choice.split()))
    return choice, tokens_out


async def _judge_response(
    judge_endpoint: str,
    judge_model: str,
    prompt: str,
    response: str,
    expected_shape: str,
) -> float:
    """Ask the judge model to score a response 0-10.

    Returns:
        float score (0.0 to 10.0).

    Raises:
        RuntimeError: BRT-REG-014 if judge fails.
    """
    judge_prompt = (
        f"You are an evaluation judge. Score the following AI response from 0 to 10.\n\n"
        f"TASK:\n{prompt}\n\n"
        f"EXPECTED SHAPE (rubric):\n{expected_shape}\n\n"
        f"RESPONSE TO SCORE:\n{response}\n\n"
        f"Reply with ONLY a number between 0 and 10 (decimals allowed). "
        f"No explanation, no prefix, just the number."
    )
    try:
        text, _ = await _call_model(judge_endpoint, judge_model, judge_prompt, timeout=30.0)
        score = float(text.strip().split()[0])
        return max(0.0, min(10.0, score))
    except Exception as exc:
        raise RuntimeError(f"BRT-REG-014: judge model failed — {exc}") from exc


async def benchmark_model(
    model: ModelInfo,
    task_type: str,
    prompts: list[dict],
    judge_endpoint: str = _DEFAULT_JUDGE_ENDPOINT,
    judge_model: str = _DEFAULT_JUDGE_MODEL,
    time_cost_per_sec: float = 0.0083,
) -> dict[str, Any]:
    """Benchmark a single model on a task_type.

    Args:
        model: The ModelInfo to benchmark.
        task_type: Task type string (must match a key in benchmark_prompts.yaml).
        prompts: List of {prompt, expected_shape} dicts for this task_type.
        judge_endpoint: OpenAI-compat endpoint for the judge model.
        judge_model: Model name for the judge.
        time_cost_per_sec: Used for throughput cost calculation.

    Returns:
        dict with keys: score (0-10), throughput_tps (float), elapsed_sec (float).

    Raises:
        ValueError: BRT-REG-013 if computed throughput is non-positive.
        RuntimeError: BRT-REG-015 if the model under test is unreachable.
    """
    if not model.endpoint:
        raise RuntimeError(
            f"BRT-REG-015: model '{model.model_id}' has no endpoint configured. "
            "Run discovery first."
        )

    endpoint = f"{model.endpoint}/v1/chat/completions"
    model_name = model.model_id.split("/", 1)[-1]  # strip source prefix

    total_tokens_out = 0
    total_wall_sec = 0.0
    scores: list[float] = []

    for item in prompts:
        prompt_text = item.get("prompt", "")
        expected_shape = item.get("expected_shape", "")
        if not prompt_text:
            continue

        t0 = time.monotonic()
        try:
            response_text, tokens_out = await _call_model(endpoint, model_name, prompt_text)
        except Exception as exc:
            logger.warning(
                "BRT-REG-015: model '%s' failed on prompt '%s…': %s",
                model.model_id, prompt_text[:60], exc,
            )
            scores.append(0.0)
            continue
        elapsed = time.monotonic() - t0

        total_tokens_out += tokens_out
        total_wall_sec += elapsed

        if expected_shape:
            try:
                score = await _judge_response(
                    judge_endpoint, judge_model, prompt_text, response_text, expected_shape
                )
            except RuntimeError as exc:
                logger.warning("%s", exc)
                score = 5.0  # neutral fallback if judge fails
            scores.append(score)

    avg_score = sum(scores) / len(scores) if scores else 0.0

    if total_wall_sec <= 0:
        raise ValueError(
            f"BRT-REG-013: benchmark for '{model.model_id}' recorded zero elapsed time. "
            "This should not be possible — check the clock."
        )
    throughput_tps = total_tokens_out / total_wall_sec if total_tokens_out > 0 else 1.0

    return {
        "score": round(avg_score, 2),
        "throughput_tps": round(throughput_tps, 2),
        "elapsed_sec": round(total_wall_sec, 2),
    }


async def benchmark_all(
    matrix: CapabilityMatrix,
    task_types: list[str],
    prompts_path: str = "data/benchmark_prompts.yaml",
    judge_endpoint: str = _DEFAULT_JUDGE_ENDPOINT,
    judge_model: str = _DEFAULT_JUDGE_MODEL,
) -> dict[str, dict]:
    """Run benchmark_model() for every model in the matrix across all task_types.

    Returns:
        dict mapping model_id -> {task_type -> result_dict}
    """
    all_prompts = _load_prompts(prompts_path)
    results: dict[str, dict] = {}

    for model in matrix.all():
        results[model.model_id] = {}
        for task_type in task_types:
            prompts = all_prompts.get(task_type, [])
            if not prompts:
                logger.debug("No prompts for task_type='%s'; skipping.", task_type)
                continue
            try:
                result = await benchmark_model(
                    model, task_type, prompts,
                    judge_endpoint=judge_endpoint, judge_model=judge_model,
                )
                results[model.model_id][task_type] = result
                matrix.update_score(model.model_id, task_type, result["score"])
                matrix.update_throughput(model.model_id, result["throughput_tps"])
            except Exception as exc:
                logger.warning(
                    "Benchmark failed for model '%s' on task '%s': %s",
                    model.model_id, task_type, exc,
                )
                results[model.model_id][task_type] = {"error": str(exc)}

    return results
