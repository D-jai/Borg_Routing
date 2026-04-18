"""Model discovery — find models from LM Studio, Ollama, OpenRouter, and custom keys.

Each discover_* function is silent on connection failures (logs only) and
returns an empty list so the caller can continue. discover_all() returns the
count of models upserted into the matrix.

Error codes:
- BRT-REG-001: connection failure (logged, not raised — silent skip)
- BRT-REG-003: rate limit from OpenRouter
- BRT-REG-004: invalid API key for external provider
"""
from __future__ import annotations

import logging
from typing import Optional

import httpx

from .capability_matrix import CapabilityMatrix
from .models import ModelInfo, Pricing

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_TIMEOUT = 5.0  # seconds


def _lmstudio_model_id(raw_id: str) -> str:
    return f"lmstudio/{raw_id}"


def _ollama_model_id(name: str) -> str:
    return f"ollama/{name}"


async def discover_lmstudio(
    ports: list[int] = [1234, 1235, 1236, 1237],
) -> list[ModelInfo]:
    """Discover models loaded in LM Studio on the given ports.

    LM Studio exposes the OpenAI-compatible `GET /v1/models` endpoint.
    Each port may have a different model loaded. All reachable models are
    returned; unreachable ports are silently skipped.
    """
    found: list[ModelInfo] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        for port in ports:
            base = f"http://localhost:{port}"
            try:
                resp = await client.get(f"{base}/v1/models")
                resp.raise_for_status()
                data = resp.json()
            except Exception as exc:
                logger.debug("LM Studio port %d not reachable: %s", port, exc)
                continue

            for item in data.get("data", []):
                raw_id = item.get("id", "")
                if not raw_id:
                    continue
                model_id = _lmstudio_model_id(raw_id)
                context_length = item.get("context_length") or item.get("context_window") or 4096
                found.append(
                    ModelInfo(
                        model_id=model_id,
                        source="lmstudio",
                        endpoint=base,
                        context_length=int(context_length),
                    )
                )
    return found


async def discover_ollama(
    base_url: str = "http://localhost:11434",
) -> list[ModelInfo]:
    """Discover models available in Ollama via `GET /api/tags`."""
    found: list[ModelInfo] = []
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(f"{base_url}/api/tags")
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.debug("Ollama not reachable at %s: %s", base_url, exc)
            return found

        for item in data.get("models", []):
            name = item.get("name", "")
            if not name:
                continue
            details = item.get("details", {})
            context_length = details.get("context_length") or 4096
            found.append(
                ModelInfo(
                    model_id=_ollama_model_id(name),
                    source="ollama",
                    endpoint=base_url,
                    context_length=int(context_length),
                )
            )
    return found


async def discover_openrouter(
    api_key: Optional[str] = None,
) -> list[ModelInfo]:
    """Fetch all text-generation models from OpenRouter's public model list.

    No auth required for the list endpoint. api_key is passed as a header
    if provided (improves rate limits). Image-generation models are filtered out.

    Raises:
        httpx.HTTPStatusError: BRT-REG-003 on 429 (rate limit),
                               BRT-REG-004 on 401 (invalid key).
    """
    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    found: list[ModelInfo] = []
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_OPENROUTER_MODELS_URL, headers=headers)
            if resp.status_code == 429:
                logger.warning("BRT-REG-003: OpenRouter rate limit hit during discovery")
                return found
            if resp.status_code == 401:
                logger.warning("BRT-REG-004: Invalid OpenRouter API key")
                return found
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("BRT-REG-001: Could not reach OpenRouter: %s", exc)
        return found

    for item in data.get("data", []):
        model_id = item.get("id", "")
        if not model_id:
            continue

        # Filter out image-generation models
        architecture = item.get("architecture", {})
        modality = architecture.get("modality", "")
        if "image" in modality:
            continue

        context_length = item.get("context_length") or 4096
        pricing_raw = item.get("pricing", {})

        pricing: Optional[Pricing] = None
        try:
            input_rate = float(pricing_raw.get("prompt", 0) or 0)
            output_rate = float(pricing_raw.get("completion", 0) or 0)
            if input_rate >= 0 and output_rate >= 0:
                pricing = Pricing(
                    input_per_token=input_rate,
                    output_per_token=output_rate,
                )
        except (TypeError, ValueError):
            pass

        found.append(
            ModelInfo(
                model_id=f"openrouter/{model_id}",
                source="openrouter",
                context_length=int(context_length),
                pricing=pricing,
            )
        )

    return found


async def discover_custom_key(
    name: str,
    base_url: str,
    api_key: str,
) -> list[ModelInfo]:
    """Discover models from a custom OpenAI-compatible API endpoint.

    The endpoint must support `GET /v1/models`.
    """
    found: list[ModelInfo] = []
    headers = {"Authorization": f"Bearer {api_key}"}
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        try:
            resp = await client.get(f"{base_url}/v1/models", headers=headers)
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            logger.warning("BRT-REG-001: Custom key '%s' at %s not reachable: %s", name, base_url, exc)
            return found

        for item in data.get("data", []):
            raw_id = item.get("id", "")
            if not raw_id:
                continue
            context_length = item.get("context_length") or item.get("context_window") or 4096
            found.append(
                ModelInfo(
                    model_id=f"custom_{name}/{raw_id}",
                    source="custom_key",
                    endpoint=base_url,
                    context_length=int(context_length),
                )
            )
    return found


async def discover_all(
    matrix: CapabilityMatrix,
    openrouter_key: Optional[str] = None,
) -> int:
    """Run all discovery sources and upsert results into the matrix.

    Existing task_scores and throughput_tps are preserved — discovery only
    adds/updates endpoint, context_length, and pricing from live data.

    Returns:
        Number of models upserted (new + updated).
    """
    import asyncio

    results = await asyncio.gather(
        discover_lmstudio(),
        discover_ollama(),
        discover_openrouter(api_key=openrouter_key),
        return_exceptions=True,
    )

    upserted = 0
    for batch in results:
        if isinstance(batch, Exception):
            logger.warning("Discovery batch failed: %s", batch)
            continue
        for model in batch:
            existing = matrix.get(model.model_id)
            if existing:
                # Preserve task_scores, throughput_tps, specialty_tags from existing
                model = ModelInfo(
                    **{
                        **existing.model_dump(),
                        "endpoint": model.endpoint or existing.endpoint,
                        "context_length": model.context_length,
                        "pricing": model.pricing or existing.pricing,
                    }
                )
            matrix.upsert(model)
            upserted += 1

    return upserted
