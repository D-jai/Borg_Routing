"""Skills API — list, stats, and new-version management.

GET  /skills              — list all skills with usage stats
GET  /skills/{skill_id}   — skill detail + full version history + stats
POST /skills/{skill_id}/version — append a new version to skills.yaml

Error codes:
- BRT-API-050: skills load failed
- BRT-API-051: skill not found
- BRT-API-052: new-version write failed
- BRT-API-053: invalid new-version payload
"""
import copy

import aiosqlite
import yaml
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.db.schema import DB_PATH
from src.skills.library import SKILLS_PATH, SkillLibrary

router = APIRouter(prefix="/skills", tags=["skills"])

_library = SkillLibrary()


def _get_library() -> SkillLibrary:
    try:
        _library.load()
    except RuntimeError:
        pass
    return _library


class NewVersionRequest(BaseModel):
    description: str
    when_to_use: list[str] = []
    not_when: list[str] = []
    recommended_tier: str = "mid"
    outbound_template: dict = {}
    required_args: dict = {}


async def _skill_stats(skill_id: str) -> dict:
    """Aggregate skill_usage_log stats for a skill_id."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            """SELECT
                 COUNT(*) AS call_count,
                 SUM(CASE WHEN outcome='success' THEN 1 ELSE 0 END) AS success_count,
                 SUM(child_residue_count) AS total_residue,
                 COALESCE(AVG(tokens_used), 0) AS avg_tokens,
                 COALESCE(AVG(latency_ms), 0) AS avg_latency_ms
               FROM skill_usage_log WHERE skill_id=?""",
            (skill_id,),
        ) as cur:
            row = await cur.fetchone()
    if not row or row["call_count"] == 0:
        return {"call_count": 0, "success_rate": None, "total_residue": 0,
                "avg_tokens": 0, "avg_latency_ms": 0}
    call_count = row["call_count"]
    return {
        "call_count": call_count,
        "success_rate": round(row["success_count"] / call_count, 3) if call_count else None,
        "total_residue": row["total_residue"] or 0,
        "avg_tokens": round(row["avg_tokens"] or 0),
        "avg_latency_ms": round(row["avg_latency_ms"] or 0),
    }


@router.get("")
async def list_skills() -> JSONResponse:
    """Return all skills with their latest version and usage stats."""
    try:
        lib = _get_library()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-050", "message": f"Skills load failed: {exc}"},
        )

    # Group by skill_id, pick latest version
    seen: dict[str, dict] = {}
    for skill in lib._skills:
        sid = skill.get("skill_id", "")
        ver = skill.get("version", 1)
        if sid not in seen or ver > seen[sid].get("version", 0):
            seen[sid] = skill

    result = []
    for sid, skill in seen.items():
        stats = await _skill_stats(sid)
        result.append({
            "skill_id": sid,
            "version": skill.get("version", 1),
            "description": skill.get("description", ""),
            "envelope_type": skill.get("envelope_type", ""),
            "recommended_tier": skill.get("recommended_tier", ""),
            **stats,
        })

    result.sort(key=lambda s: s["call_count"], reverse=True)
    return JSONResponse(content={"skills": result})


@router.get("/{skill_id}")
async def get_skill(skill_id: str) -> JSONResponse:
    """Return full detail for a skill including all versions and stats."""
    try:
        lib = _get_library()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-050", "message": f"Skills load failed: {exc}"},
        )

    versions = [s for s in lib._skills if s.get("skill_id") == skill_id]
    if not versions:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-API-051", "message": f"Skill '{skill_id}' not found"},
        )

    stats = await _skill_stats(skill_id)
    return JSONResponse(content={
        "skill_id": skill_id,
        "versions": versions,
        "stats": stats,
    })


@router.post("/{skill_id}/version", status_code=201)
async def create_skill_version(skill_id: str, req: NewVersionRequest) -> JSONResponse:
    """Append a new version of an existing skill to skills.yaml.

    The new version number is current_max + 1.
    """
    try:
        lib = _get_library()
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-050", "message": f"Skills load failed: {exc}"},
        )

    existing = [s for s in lib._skills if s.get("skill_id") == skill_id]
    if not existing:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-API-051", "message": f"Skill '{skill_id}' not found"},
        )

    current_max = max(s.get("version", 1) for s in existing)
    base = copy.deepcopy(max(existing, key=lambda s: s.get("version", 1)))
    new_version = current_max + 1

    # Apply overrides from request
    base["version"] = new_version
    if req.description:
        base["description"] = req.description
    if req.when_to_use:
        base["when_to_use"] = req.when_to_use
    if req.not_when:
        base["not_when"] = req.not_when
    if req.recommended_tier:
        base["recommended_tier"] = req.recommended_tier
    if req.outbound_template:
        base["outbound_template"] = req.outbound_template
    if req.required_args:
        base["required_args"] = req.required_args

    try:
        with open(SKILLS_PATH, "r", encoding="utf-8") as f:
            all_skills = yaml.safe_load(f) or []
        all_skills.append(base)
        with open(SKILLS_PATH, "w", encoding="utf-8") as f:
            yaml.dump(all_skills, f, default_flow_style=False, allow_unicode=True, sort_keys=False)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-API-052", "message": f"New-version write failed: {exc}"},
        )

    # Reload the in-memory library
    try:
        lib.load()
    except Exception:
        pass

    return JSONResponse(
        status_code=201,
        content={"skill_id": skill_id, "version": new_version},
    )
