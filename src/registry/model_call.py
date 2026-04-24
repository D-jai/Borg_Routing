"""Resolve a ModelInfo row into (url, headers, body_model_name) for HTTP calls.

Primary: single developer running locally (LM Studio / Ollama + optional OpenRouter).
Override: any source='custom_key' row supplies its own endpoint.

Error codes (note: BRT-ORC-008 and BRT-ORC-011 were already claimed by
engine.py and fold_in.py respectively when this helper was added, so this
module uses 013/014 instead — same intent as the Phase 1 spec):
- BRT-ORC-013: OpenRouter row selected but ``OPENROUTER_API_KEY`` not set
- BRT-ORC-014: model row has no ``endpoint`` configured — run discovery
"""
from __future__ import annotations

import os

from .models import ModelInfo

DEFAULT_APP_URL = "https://github.com/D-jai/Borg_Routing"
DEFAULT_APP_NAME = "BORG_ROUTE"


def resolve_model_call(model: ModelInfo) -> tuple[str, dict[str, str], str]:
    """Return (full_url, headers, body_model_name) for one HTTP call.

    Handles the four provider shapes in the matrix uniformly:
      - ``lmstudio`` / ``ollama`` / ``custom_key``: use ``model.endpoint`` as-is, no auth.
      - ``openrouter``: force endpoint to OpenRouter's base, inject bearer auth,
        and strip the ``openrouter/`` prefix from the outbound body model id.

    Raises:
        ValueError: BRT-ORC-013 if an OpenRouter row is selected but
            ``OPENROUTER_API_KEY`` is not in the environment.
        ValueError: BRT-ORC-014 if the row has no endpoint configured
            (common after a stale install before re-running discovery).
    """
    endpoint = (model.endpoint or "").rstrip("/")
    if not endpoint:
        raise ValueError(
            f"BRT-ORC-014: model '{model.model_id}' has no endpoint configured. "
            "Run POST /models/discover to re-populate."
        )
    url = f"{endpoint}/v1/chat/completions"
    headers: dict[str, str] = {}

    if model.source == "openrouter":
        key = os.getenv("OPENROUTER_API_KEY")
        if not key:
            raise ValueError(
                "BRT-ORC-013: OPENROUTER_API_KEY not set; cannot call OpenRouter "
                "model. Set the key in .env or uncheck all OpenRouter rows."
            )
        headers["Authorization"] = f"Bearer {key}"
        headers["HTTP-Referer"] = os.getenv("OPENROUTER_APP_URL", DEFAULT_APP_URL)
        headers["X-Title"] = os.getenv("OPENROUTER_APP_NAME", DEFAULT_APP_NAME)
        body_model = model.model_id.removeprefix("openrouter/")
    else:  # lmstudio, ollama, custom_key
        body_model = model.model_id.split("/", 1)[-1]

    return url, headers, body_model
