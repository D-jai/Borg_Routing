"""Model discovery — find models from LM Studio, Ollama, OpenRouter, and custom keys.

Each discover_* function is silent on connection failures (logs only) and
returns an empty list so the caller can continue. discover_all() returns the
count of models upserted into the matrix.

OpenRouter discovery is curated: only model IDs listed in
``data/curated_models.yaml`` are pulled. Everything else is ignored. This
replaces the prior flood-every-OpenRouter-model behaviour.

Error codes:
- BRT-REG-001: connection failure (logged, not raised — silent skip)
- BRT-REG-003: rate limit from OpenRouter
- BRT-REG-004: invalid API key for external provider
- BRT-REG-032: data/curated_models.yaml missing or malformed
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

import httpx
import yaml

from .capability_matrix import CapabilityMatrix
from .models import ModelInfo, Pricing

logger = logging.getLogger(__name__)

_OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"
_OPENROUTER_API_BASE = "https://openrouter.ai/api"
_CURATED_MODELS_PATH = "data/curated_models.yaml"
_TIMEOUT = 5.0


def _lmstudio_model_id(raw_id: str) -> str:
    return f"lmstudio/{raw_id}"


def _ollama_model_id(name: str) -> str:
    return f"ollama/{name}"


def _load_curated_ids(path: str = _CURATED_MODELS_PATH) -> set[str]:
    """Load the curated OpenRouter model IDs (without the 'openrouter/' prefix).

    Raises:
        FileNotFoundError: BRT-REG-032 — file missing.
        ValueError: BRT-REG-032 — file malformed.
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(
            f"BRT-REG-032: curated_models.yaml not found at '{path}'. "
            "Create it with a list of OpenRouter model IDs you want in the pool."
        )
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ValueError(f"BRT-REG-032: curated_models.yaml parse error — {exc}") from exc

    if not isinstance(raw, list):
        raise ValueError(
            "BRT-REG-032: curated_models.yaml must be a list of entries with an 'id' field."
        )

    ids: set[str] = set()
    for entry in raw:
        if isinstance(entry, dict) and entry.get("id"):
            ids.add(str(entry["id"]).strip())
        elif isinstance(entry, str):
            ids.add(entry.strip())
    return ids


async def discover_lmstudio(
    ports: list[int] = [1234, 1235, 1236, 1237],
) -> list[ModelInfo]:
    """Discover models loaded in LM Studio on the given ports."""
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
    curated_path: str = _CURATED_MODELS_PATH,
) -> list[ModelInfo]:
    """Fetch only curated OpenRouter models.

    The full OpenRouter catalogue is pulled, then filtered down to the IDs
    listed in ``data/curated_models.yaml``. A missing curated file raises
    BRT-REG-032 (loudly — this is an operator error).

    Raises:
        FileNotFoundError, ValueError: BRT-REG-032 on curated-list problems.
    """
    curated_ids = _load_curated_ids(curated_path)
    if not curated_ids:
        logger.warning("BRT-REG-032: curated_models.yaml is empty; no OpenRouter models will be discovered.")
        return []

    headers: dict[str, str] = {}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(_OPENROUTER_MODELS_URL, headers=headers)
            if resp.status_code == 429:
                logger.warning("BRT-REG-003: OpenRouter rate limit hit during discovery")
                return []
            if resp.status_code == 401:
                logger.warning("BRT-REG-004: Invalid OpenRouter API key")
                return []
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("BRT-REG-001: Could not reach OpenRouter: %s", exc)
        return []

    found: list[ModelInfo] = []
    seen: set[str] = set()
    for item in data.get("data", []):
        model_id = item.get("id", "")
        if not model_id or model_id not in curated_ids:
            continue
        seen.add(model_id)

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
                endpoint=_OPENROUTER_API_BASE,
                context_length=int(context_length),
                pricing=pricing,
            )
        )

    missing = curated_ids - seen
    if missing:
        logger.warning(
            "BRT-REG-032: %d curated IDs not found in OpenRouter catalogue: %s",
            len(missing), sorted(missing),
        )
    return found


async def discover_custom_key(
    name: str,
    base_url: str,
    api_key: str,
) -> list[ModelInfo]:
    """Discover models from a custom OpenAI-compatible API endpoint."""
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


async def reconcile_local_rows(
    matrix: CapabilityMatrix,
    discovered_ids: set[str],
) -> int:
    """Mark local rows (lmstudio/ollama) unavailable if not seen this pass.

    "Unavailable" means ``endpoint`` is cleared and ``checked`` forced to False.
    The row is kept so operator-assigned scores / notes survive; it just can't
    be routed to until discovery sees it again.

    OpenRouter and custom_key rows are never reconciled — their authoritative
    source is the curated list / operator-provided credentials, not live probing.

    Returns:
        Number of rows reconciled.
    """
    reconciled = 0
    for m in matrix.all():
        if m.source not in ("lmstudio", "ollama"):
            continue
        if m.model_id in discovered_ids:
            continue
        if m.endpoint or m.checked:
            m.endpoint = None
            m.checked = False
            reconciled += 1
    return reconciled


async def discover_all(
    matrix: CapabilityMatrix,
    openrouter_key: Optional[str] = None,
) -> int:
    """Run all discovery sources and upsert results into the matrix.

    Existing task_scores, specialty_tags, throughput_tps, and the `checked`
    flag are preserved — discovery only refreshes endpoint, context_length,
    and pricing. Local rows (lmstudio/ollama) that were NOT seen in this pass
    are reconciled by :func:`reconcile_local_rows` (endpoint cleared,
    checked=False) so stale ghosts from previous installs don't keep routing.
    """
    import asyncio

    results = await asyncio.gather(
        discover_lmstudio(),
        discover_ollama(),
        discover_openrouter(api_key=openrouter_key),
        return_exceptions=True,
    )

    upserted = 0
    all_found: list[ModelInfo] = []
    for batch in results:
        if isinstance(batch, Exception):
            logger.warning("Discovery batch failed: %s", batch)
            continue
        for model in batch:
            existing = matrix.get(model.model_id)
            if existing:
                # Preserve task_scores, throughput_tps, specialty_tags, checked
                merged = ModelInfo(
                    **{
                        **existing.model_dump(),
                        "endpoint": model.endpoint or existing.endpoint,
                        "context_length": model.context_length,
                        "pricing": model.pricing or existing.pricing,
                    }
                )
                matrix.upsert(merged)
            else:
                matrix.upsert(model)
            all_found.append(model)
            upserted += 1

    discovered_ids = {m.model_id for m in all_found}
    reconciled = await reconcile_local_rows(matrix, discovered_ids)
    if reconciled:
        logger.info(
            "Reconciled %d stale local row(s): endpoint cleared, checked=False.",
            reconciled,
        )

    return upserted
