"""Admin status endpoints: which keys are saved, and which local servers answer.

GET /keys/status          -> {"providers": [{id, label, env_var, saved}], "admin_token_set": bool, "admin_token_valid": bool}
GET /status/local         -> {"reachable": bool, "servers": [{name, url, reachable}]}

Neither endpoint returns, logs or echoes a key value. ``/keys/status`` only
tests whether a variable is present and non-empty, in the process environment
(which ``load_dotenv`` fills from ``.env`` at start-up). A key that is only a
line in the ``.env`` file is not saved yet: the running server does not have
it, so ``env_file_only`` is true and the page says a restart is needed.
"""
from __future__ import annotations

import asyncio
import os
from pathlib import Path

import httpx
from fastapi import APIRouter
from fastapi.responses import JSONResponse

from src.api.auth import admin_token_set, admin_token_valid
from src.registry.discovery import (
    lmstudio_base_urls,
    local_openai_base_urls,
    ollama_base_url,
)

router = APIRouter(tags=["admin"])

# Providers the engine actually reads a key for: the id, a label, the variable.
_PROVIDERS = (
    ("openrouter", "OpenRouter", "OPENROUTER_API_KEY"),
    ("tavily", "Tavily (web search, optional)", "TAVILY_API_KEY"),
)

_PROBE_TIMEOUT = 1.5


def _env_file_has(var: str, env_path: str = ".env") -> bool:
    """True when ``env_path`` holds a non-empty ``var=...`` line. Never returns the value."""
    path = Path(env_path)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return False
    for line in lines:
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip() == var and value.strip().strip("\"'") != "":
            return True
    return False


def key_saved(var: str) -> bool:
    """True only when the running process has the key (what the engine reads)."""
    return bool((os.environ.get(var) or "").strip())


@router.get("/keys/status")
async def keys_status() -> JSONResponse:
    """Per provider: does the running server have a key? Never the key itself."""
    return JSONResponse(content={
        "providers": [
            {
                "id": pid, "label": label, "env_var": var,
                "saved": key_saved(var),
                "env_file_only": not key_saved(var) and _env_file_has(var),
            }
            for pid, label, var in _PROVIDERS
        ],
        # Whether BORG_ADMIN_TOKEN is set; never the token itself.
        "admin_token_set": admin_token_set(),
        "admin_token_valid": admin_token_valid(),
    })


async def _probe(client: httpx.AsyncClient, name: str, url: str, path: str) -> dict:
    try:
        res = await client.get(url.rstrip("/") + path)
        ok = res.status_code < 500
    except Exception:
        ok = False
    return {"name": name, "url": url, "reachable": ok}


@router.get("/status/local")
async def local_status() -> JSONResponse:
    """Do the configured local model servers answer? (A short probe, no models loaded.)"""
    targets = [("Ollama", ollama_base_url(), "/api/tags")]
    targets += [("LM Studio", u, "/v1/models") for u in lmstudio_base_urls()]
    targets += [("Local OpenAI-compatible", u, "/v1/models") for u in local_openai_base_urls()]
    async with httpx.AsyncClient(timeout=_PROBE_TIMEOUT) as client:
        servers = await asyncio.gather(*(_probe(client, n, u, p) for n, u, p in targets))
    return JSONResponse(content={
        "reachable": any(s["reachable"] for s in servers),
        "servers": list(servers),
    })
