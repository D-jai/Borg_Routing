"""Admin API — config management and admin rules ledger.

GET  /config                      → all current config key:value pairs
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
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

import aiosqlite
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from src.db.schema import DB_PATH
from src.db.state import get_config, set_config

router = APIRouter(prefix="/config", tags=["admin"])


class ConfigSetRequest(BaseModel):
    key: str
    value: str


class RuleAppendRequest(BaseModel):
    text: str


@router.get("")
async def get_all_config() -> JSONResponse:
    """Return all config entries as a key:value dict."""
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
