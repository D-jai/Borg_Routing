"""Registry API endpoints — model discovery, matrix, research, checkbox, API keys.

Endpoints:
- GET    /models                        -> list of ModelInfo
- GET    /models/matrix                 -> full matrix JSON
- POST   /models/discover               -> { upserted }
- POST   /models/research               -> { scored: [model_id] } (Get Info button)
- PATCH  /models/{model_id:path}/checked -> { model_id, checked }
- POST   /keys                          -> persist a provider key

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

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.discovery import discover_all
from src.registry.models import ModelInfo
from src.registry.scorer import research_and_apply

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


class ResearchRequest(BaseModel):
    model_ids: Optional[list[str]] = None  # None = all rows in matrix


class ResearchResponse(BaseModel):
    scored: list[str]


class CheckedRequest(BaseModel):
    checked: bool


class CheckedResponse(BaseModel):
    model_id: str
    checked: bool


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
    """Probe LM Studio, Ollama, and the curated OpenRouter list; upsert results."""
    openrouter_key: Optional[str] = os.getenv("OPENROUTER_API_KEY")
    try:
        count = await discover_all(matrix, openrouter_key=openrouter_key)
        matrix.save()
        return DiscoverResponse(upserted=count)
    except Exception as exc:
        logger.error("Discovery failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Discovery error: {exc}") from exc


@router.post("/models/research", response_model=ResearchResponse)
async def research_models_endpoint(
    body: ResearchRequest,
    matrix: CapabilityMatrix = Depends(get_matrix),
):
    """Run the Get Model Info pass.

    If ``model_ids`` is omitted, every row currently in the matrix is scored.
    Otherwise only the listed model_ids are researched (missing rows are
    skipped with a log warning).
    """
    requested = body.model_ids or [m.model_id for m in matrix.all()]
    if not requested:
        return ResearchResponse(scored=[])

    try:
        scored = await research_and_apply(matrix, requested)
    except RuntimeError as exc:
        # BRT-REG-030
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    except ValueError as exc:
        # BRT-REG-031
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    matrix.save()
    return ResearchResponse(scored=list(scored.keys()))


@router.patch("/models/{model_id:path}/checked", response_model=CheckedResponse)
async def set_model_checked(
    model_id: str,
    body: CheckedRequest,
    matrix: CapabilityMatrix = Depends(get_matrix),
):
    """Flip the `checked` flag on a model. Unchecked models are excluded from routing."""
    if matrix.get(model_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"BRT-API-002: model '{model_id}' not found in capability matrix.",
        )
    matrix.set_checked(model_id, body.checked)
    matrix.save()
    return CheckedResponse(model_id=model_id, checked=body.checked)


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
