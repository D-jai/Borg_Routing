"""Registry API endpoints — model discovery, matrix, benchmark, API keys.

Endpoints:
- GET  /models                     → list of ModelInfo
- POST /models/discover             → { added, updated }
- POST /models/benchmark/{model_id} → { score, throughput_tps }
- GET  /models/matrix               → full capability_matrix content as JSON
- POST /keys                        → write provider key to .env

Error codes:
- BRT-API-001: malformed request body
- BRT-API-002: model not found in matrix
"""
from __future__ import annotations

import logging
import os
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from src.registry.benchmark import benchmark_model
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.discovery import discover_all
from src.registry.models import ModelInfo

logger = logging.getLogger(__name__)

router = APIRouter(tags=["models"])


# ---------------------------------------------------------------------------
# Dependency — shared matrix from app state
# ---------------------------------------------------------------------------

def get_matrix(request: Request) -> CapabilityMatrix:
    return request.app.state.matrix if hasattr(request.app.state, "matrix") else _get_main_matrix()


def _get_main_matrix() -> CapabilityMatrix:
    """Fallback: import the singleton from main.py."""
    from main import matrix  # noqa: PLC0415
    return matrix


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class DiscoverResponse(BaseModel):
    upserted: int


class BenchmarkResponse(BaseModel):
    score: float
    throughput_tps: float
    elapsed_sec: float


class KeyRequest(BaseModel):
    name: str
    base_url: str
    api_key: str


class KeyResponse(BaseModel):
    message: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/models", response_model=list[ModelInfo])
async def list_models(matrix: CapabilityMatrix = Depends(get_matrix)):
    """Return all models currently in the capability matrix."""
    return matrix.all()


@router.get("/models/matrix")
async def get_matrix_json(matrix: CapabilityMatrix = Depends(get_matrix)):
    """Return the full capability matrix as a JSON array."""
    return [m.model_dump() for m in matrix.all()]


@router.post("/models/discover", response_model=DiscoverResponse)
async def discover_models(matrix: CapabilityMatrix = Depends(get_matrix)):
    """Probe LM Studio, Ollama, and OpenRouter; upsert discovered models."""
    openrouter_key: Optional[str] = os.getenv("OPENROUTER_API_KEY")
    try:
        count = await discover_all(matrix, openrouter_key=openrouter_key)
        matrix.save()
        return DiscoverResponse(upserted=count)
    except Exception as exc:
        logger.error("Discovery failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Discovery error: {exc}") from exc


@router.post("/models/benchmark/{model_id:path}", response_model=BenchmarkResponse)
async def run_benchmark(
    model_id: str,
    matrix: CapabilityMatrix = Depends(get_matrix),
):
    """Run a 5-prompt quality + throughput benchmark against a specific model.

    Raises HTTP 404 (BRT-API-002) if model_id is not in the matrix.
    """
    model = matrix.get(model_id)
    if model is None:
        raise HTTPException(
            status_code=404,
            detail=f"BRT-API-002: model '{model_id}' not found in capability matrix.",
        )

    from src.registry.benchmark import _load_prompts

    all_prompts = _load_prompts()
    # Use the first available task_type for the model
    task_type = next(
        (tt for tt in model.task_scores if tt in all_prompts),
        next(iter(all_prompts), "reasoning"),
    )
    prompts = all_prompts.get(task_type, [])

    try:
        result = await benchmark_model(model, task_type, prompts)
    except RuntimeError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    matrix.update_score(model_id, task_type, result["score"])
    matrix.update_throughput(model_id, result["throughput_tps"])
    matrix.save()

    return BenchmarkResponse(**result)


@router.post("/keys", response_model=KeyResponse)
async def add_key(body: KeyRequest):
    """Save an external provider API key to the .env file.

    The key is written as KEY_{NAME_UPPER}=value. The base_url is also stored
    as ENDPOINT_{NAME_UPPER}=value.
    """
    if not body.name or not body.api_key:
        raise HTTPException(
            status_code=400,
            detail="BRT-API-001: 'name' and 'api_key' are required.",
        )

    env_path = ".env"
    name_upper = body.name.upper().replace("-", "_").replace(" ", "_")
    key_var = f"KEY_{name_upper}"
    endpoint_var = f"ENDPOINT_{name_upper}"

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()

    # Remove existing entries for this provider
    lines = [
        line for line in lines
        if not line.startswith(f"{key_var}=") and not line.startswith(f"{endpoint_var}=")
    ]
    lines.append(f"{key_var}={body.api_key}\n")
    lines.append(f"{endpoint_var}={body.base_url}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # Reload into current process environment
    os.environ[key_var] = body.api_key
    os.environ[endpoint_var] = body.base_url

    return KeyResponse(message=f"Key '{body.name}' saved successfully.")
