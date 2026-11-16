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

from src.api.auth import require_admin
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


def _share_with_engine(matrix: CapabilityMatrix) -> None:
    """R2: after a change, make sure the engine routes on this matrix object."""
    from src.orchestrator.engine import reload_matrix  # noqa: PLC0415
    reload_matrix(matrix)


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


async def _refuse_in_all_local(action: str) -> None:
    """On-premise lane: in all_local, admin actions that call out are refused
    (BRT-LANE-005, design note 6.4)."""
    from src.db.schema import DB_PATH  # noqa: PLC0415
    from src.lane.errors import LaneError  # noqa: PLC0415
    from src.lane.settings import load_lane_settings  # noqa: PLC0415

    try:
        mode = (await load_lane_settings(DB_PATH)).mode
    except LaneError as exc:
        raise HTTPException(status_code=400, detail={"error": exc.code, "message": str(exc)})
    if mode == "all_local":
        raise HTTPException(
            status_code=403,
            detail={
                "error": "BRT-LANE-005",
                "message": f"{action} calls out and is refused while lane_mode is all_local",
            },
        )


@router.post("/models/discover", response_model=DiscoverResponse, dependencies=[Depends(require_admin)])
async def discover_models(matrix: CapabilityMatrix = Depends(get_matrix)):
    """Probe LM Studio, Ollama, and the curated OpenRouter list; upsert results.

    Refused (BRT-LANE-005) while lane_mode is all_local.
    """
    await _refuse_in_all_local("Discover")
    openrouter_key: Optional[str] = os.getenv("OPENROUTER_API_KEY")
    try:
        count = await discover_all(matrix, openrouter_key=openrouter_key)
        matrix.save()
        _share_with_engine(matrix)
        return DiscoverResponse(upserted=count)
    except Exception as exc:
        logger.error("Discovery failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Discovery error: {exc}") from exc


@router.post("/models/research", response_model=ResearchResponse, dependencies=[Depends(require_admin)])
async def research_models_endpoint(
    body: ResearchRequest,
    matrix: CapabilityMatrix = Depends(get_matrix),
):
    """Run the Get Model Info pass.

    If ``model_ids`` is omitted, every row currently in the matrix is scored.
    Otherwise only the listed model_ids are researched (missing rows are
    skipped with a log warning). Refused (BRT-LANE-005) while lane_mode is
    all_local.
    """
    await _refuse_in_all_local("Get Model Info")
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
    _share_with_engine(matrix)
    return ResearchResponse(scored=list(scored.keys()))


@router.patch("/models/{model_id:path}/checked", response_model=CheckedResponse, dependencies=[Depends(require_admin)])
async def set_model_checked(
    model_id: str,
    body: CheckedRequest,
    matrix: CapabilityMatrix = Depends(get_matrix),
):
    """Flip the `checked` flag on a model. Unchecked models are excluded from routing.

    BUG-023 — after writing the YAML, also invalidate the orchestrator
    engine's in-memory matrix cache so the next routed run honours the
    new checked state. Without this, operators saw `GET /models/matrix`
    update immediately but the router kept routing to old (un)checked
    models until uvicorn was restarted.
    """
    if matrix.get(model_id) is None:
        raise HTTPException(
            status_code=404,
            detail=f"BRT-API-002: model '{model_id}' not found in capability matrix.",
        )
    matrix.set_checked(model_id, body.checked)
    matrix.save()

    # BUG-023 / R2 — point the engine at this matrix so the next route sees it.
    # Local import to avoid a top-level cycle (engine imports from many
    # registry modules; the API router is wired in main after engine).
    try:
        from src.orchestrator.engine import reload_matrix  # noqa: PLC0415
        reload_matrix(matrix)
    except Exception as exc:
        logger.warning(
            "BUG-023: failed to invalidate orchestrator matrix cache after "
            "PATCH on model_id=%s: %s. Operator will need to restart "
            "uvicorn for the change to affect routing.", model_id, exc,
        )

    return CheckedResponse(model_id=model_id, checked=body.checked)


_CANONICAL_KEY_VARS = {"openrouter": "OPENROUTER_API_KEY"}


@router.post("/keys", response_model=KeyResponse, dependencies=[Depends(require_admin)])
async def add_key(body: KeyRequest):
    """Save an external provider API key to the .env file.

    Writes ``{KEY_VAR}=value`` and ``ENDPOINT_{NAME_UPPER}=value``. For
    well-known providers the canonical env-var name is preferred
    (e.g. ``OPENROUTER_API_KEY`` instead of ``KEY_OPENROUTER``) so that
    discovery, the scorer, and chat completions all read the same variable.
    """
    if not body.name or not body.api_key:
        raise HTTPException(
            status_code=400,
            detail="BRT-API-001: 'name' and 'api_key' are required.",
        )

    env_path = ".env"
    name_lower = body.name.lower()
    name_upper = body.name.upper().replace("-", "_").replace(" ", "_")
    key_var = _CANONICAL_KEY_VARS.get(name_lower, f"KEY_{name_upper}")
    endpoint_var = f"ENDPOINT_{name_upper}"
    # For well-known providers we also strip any legacy KEY_{NAME_UPPER} entry
    # so operators upgrading from the old naming don't end up with two keys.
    legacy_key_var = (
        f"KEY_{name_upper}"
        if key_var != f"KEY_{name_upper}"
        else None
    )

    lines: list[str] = []
    if os.path.exists(env_path):
        with open(env_path, encoding="utf-8") as f:
            lines = f.readlines()

    # Remove existing entries for this provider (and any legacy alias)
    def _prefix(varname: str) -> str:
        return f"{varname}="

    drop_prefixes = [_prefix(key_var), _prefix(endpoint_var)]
    if legacy_key_var:
        drop_prefixes.append(_prefix(legacy_key_var))
    lines = [
        line for line in lines
        if not any(line.startswith(p) for p in drop_prefixes)
    ]
    lines.append(f"{key_var}={body.api_key}\n")
    lines.append(f"{endpoint_var}={body.base_url}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # Reload into current process environment
    os.environ[key_var] = body.api_key
    os.environ[endpoint_var] = body.base_url
    if legacy_key_var and legacy_key_var in os.environ:
        os.environ.pop(legacy_key_var, None)

    return KeyResponse(
        message=f"Key '{body.name}' saved successfully.",
    )
