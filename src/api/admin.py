"""Admin API — config management and admin rules ledger.

GET  /config                      → all current config key:value pairs (DB only)
GET  /config/effective            → resolved settings (DB → env → default) + source hint
POST /config                      → upsert a config key:value
GET  /config/rules                → list all rules in the admin rules ledger
POST /config/rules                → append a new rule to the ledger
POST /config/rules/{rule_id}/retire → retire a rule (sets retired_at)

Error codes:
- BRT-ADM-001: missing required field in request body
- BRT-ADM-002: rule not found
- BRT-ADM-003: rule already retired
- BRT-ADM-004: config read failed
- BRT-ADM-005: config write failed
"""
import json
import os
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.db.schema import DB_PATH
from src.db.state import get_config, set_config


# Canonical operator-facing settings. Each row:
#   key           — config-table key
#   env_var       — environment variable read by the engine, or None
#   default       — hardcoded fallback
#   cast          — callable to coerce the string value to its runtime type
#   label         — human label for the UI
#   help_text     — one-line description for the UI
#   env_only_hint — if True, Admin UI treats it as purely env-driven (no DB override expected)
_EFFECTIVE_SETTINGS: list[dict[str, Any]] = [
    {
        "key": "max_run_seconds",
        "env_var": "BORG_MAX_RUN_SECONDS",
        "default": 300,
        "cast": lambda v: int(float(v)),
        "label": "Max Run Seconds",
        "help_text": "Wall-clock budget per run. Runs that exceed this are killed "
                     "and marked failed with BRT-PIP-008.",
    },
    {
        "key": "time_cost_per_sec",
        "env_var": "TIME_COST_PER_SEC",
        "default": 0.0001,
        "cast": float,
        "label": "Time Cost Per Second ($/s)",
        "help_text": "Dollar cost per wall-second used by the router to penalise slow models.",
    },
    {
        "key": "orchestrator_quality_requirement",
        "env_var": "ORCHESTRATOR_QUALITY_REQUIREMENT",
        "default": 6.5,
        "cast": float,
        "label": "Orchestrator Quality Requirement",
        "help_text": "Minimum task_score for a model row to qualify as orchestrator "
                     "(degrades to 5.1 if no model meets the bar).",
    },
    {
        "key": "orchestrator_max_tokens",
        "env_var": "ORCHESTRATOR_MAX_TOKENS",
        "default": 2000,
        "cast": lambda v: int(float(v)),
        "label": "Orchestrator Max Tokens",
        "help_text": "Ceiling on orchestrator JSON responses.",
    },
    {
        "key": "answerer_max_tokens",
        "env_var": "ANSWERER_MAX_TOKENS",
        "default": 2000,
        "cast": lambda v: int(float(v)),
        "label": "Answerer Max Tokens",
        "help_text": "Ceiling on answerer responses.",
    },
    {
        "key": "rule_zero_shortcut",
        "env_var": "BORG_RULE_ZERO_SHORTCUT",
        "default": True,
        "cast": lambda v: str(v).strip().lower() != "false",
        "label": "Rule Zero Shortcut",
        "help_text": "When true, trivial conversational prompts skip plan/re_plan "
                     "and go straight to compose.",
    },
    {
        "key": "max_recursion_depth",
        "env_var": None,
        "default": 5,
        "cast": lambda v: int(float(v)),
        "label": "Max Recursion Depth",
        "help_text": "Hardcoded in src/orchestrator/engine.py (MAX_DEPTH). "
                     "Change requires a code edit + redeploy.",
    },
    {
        "key": "default_strategy",
        "env_var": None,
        "default": "SYSTEM_CHOOSE",
        "cast": str,
        "label": "Default Strategy",
        "help_text": "Router strategy when the caller doesn't pick one. "
                     "SYSTEM_CHOOSE | LOCAL_FIRST.",
    },
    {
        "key": "context_window_size",
        "env_var": None,
        "default": 10,
        "cast": lambda v: int(float(v)),
        "label": "Context Window Size (turns)",
        "help_text": "Number of recent thread turns exposed to the orchestrator.",
    },
]


async def _resolve_setting(row: dict[str, Any]) -> dict[str, Any]:
    """Resolve one setting through DB → env → default, returning {value, source}."""
    db_val = await get_config(row["key"], db_path=DB_PATH)
    if db_val is not None:
        try:
            return {"value": row["cast"](db_val), "source": "db", "raw": db_val}
        except Exception:
            return {"value": db_val, "source": "db", "raw": db_val}

    if row["env_var"]:
        env_val = os.getenv(row["env_var"])
        if env_val is not None and env_val != "":
            try:
                return {"value": row["cast"](env_val), "source": "env", "raw": env_val}
            except Exception:
                return {"value": env_val, "source": "env", "raw": env_val}

    return {"value": row["default"], "source": "default", "raw": None}

router = APIRouter(prefix="/config", tags=["admin"])


class ConfigSetRequest(BaseModel):
    key: str
    value: str


class RuleAppendRequest(BaseModel):
    text: str


@router.get("")
async def get_all_config() -> JSONResponse:
    """Return all config entries as a key:value dict (DB-only, backward-compat)."""
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            async with db.execute("SELECT key, value FROM config ORDER BY key") as cur:
                rows = await cur.fetchall()
        result = {}
        for row in rows:
            try:
                result[row["key"]] = json.loads(row["value"])
            except (json.JSONDecodeError, TypeError):
                result[row["key"]] = row["value"]
        return JSONResponse(content=result)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-004", "message": f"Config read failed: {exc}"},
        )


@router.get("/effective")
async def get_effective_config() -> JSONResponse:
    """Return each known operator-facing setting resolved through DB → env → default.

    Response shape:
        {"settings": [
            {"key": "...", "label": "...", "help_text": "...",
             "value": ..., "source": "db" | "env" | "default",
             "env_var": "BORG_MAX_RUN_SECONDS" | null}
            ...
        ]}

    Intended for the read-only Admin UI. Writes still go through POST /config
    (though the Admin UI no longer exposes that for these keys).
    """
    try:
        out = []
        for row in _EFFECTIVE_SETTINGS:
            resolved = await _resolve_setting(row)
            out.append({
                "key": row["key"],
                "label": row["label"],
                "help_text": row["help_text"],
                "env_var": row["env_var"],
                "value": resolved["value"],
                "source": resolved["source"],
            })
        return JSONResponse(content={"settings": out})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-004", "message": f"Effective-config read failed: {exc}"},
        )


@router.post("")
async def set_config_entry(req: ConfigSetRequest) -> JSONResponse:
    """Upsert a config key:value pair."""
    if not req.key or not req.key.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-ADM-001", "message": "key is required"},
        )
    try:
        await set_config(req.key, req.value, db_path=DB_PATH)
        return JSONResponse(content={"key": req.key, "status": "saved"})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-005", "message": f"Config write failed: {exc}"},
        )


@router.get("/rules")
async def list_rules() -> JSONResponse:
    """Return all admin rules (active and retired)."""
    try:
        ledger = await get_config("admin_rules_ledger", db_path=DB_PATH) or []
        return JSONResponse(content={"rules": ledger})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-004", "message": f"Rules read failed: {exc}"},
        )


@router.post("/rules")
async def append_rule(req: RuleAppendRequest) -> JSONResponse:
    """Append a new rule to the admin rules ledger."""
    if not req.text or not req.text.strip():
        raise HTTPException(
            status_code=400,
            detail={"error": "BRT-ADM-001", "message": "text is required"},
        )
    try:
        ledger = await get_config("admin_rules_ledger", db_path=DB_PATH) or []
        new_rule = {
            "id": str(uuid4())[:8],
            "text": req.text.strip(),
            "added_at": datetime.now(timezone.utc).isoformat(),
            "retired_at": None,
        }
        ledger.append(new_rule)
        await set_config("admin_rules_ledger", ledger, db_path=DB_PATH)
        return JSONResponse(content={"rule": new_rule, "status": "added"})
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-005", "message": f"Rule append failed: {exc}"},
        )


@router.post("/rules/{rule_id}/retire")
async def retire_rule(rule_id: str) -> JSONResponse:
    """Retire a rule by setting its retired_at timestamp.

    Retired rules are excluded from governance bundles sent to models.
    """
    try:
        ledger = await get_config("admin_rules_ledger", db_path=DB_PATH) or []
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-004", "message": f"Rules read failed: {exc}"},
        )

    target = next((r for r in ledger if r.get("id") == rule_id), None)
    if target is None:
        raise HTTPException(
            status_code=404,
            detail={"error": "BRT-ADM-002", "message": f"Rule '{rule_id}' not found"},
        )
    if target.get("retired_at") is not None:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "BRT-ADM-003",
                "message": f"Rule '{rule_id}' is already retired",
            },
        )

    target["retired_at"] = datetime.now(timezone.utc).isoformat()
    try:
        await set_config("admin_rules_ledger", ledger, db_path=DB_PATH)
    except Exception as exc:
        raise HTTPException(
            status_code=500,
            detail={"error": "BRT-ADM-005", "message": f"Rule retire failed: {exc}"},
        )

    return JSONResponse(content={"rule_id": rule_id, "status": "retired"})
