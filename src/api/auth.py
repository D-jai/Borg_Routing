"""Admin lock: one dependency for every route that changes state for operators.

The token is ``BORG_ADMIN_TOKEN``, read at request time (never cached) and sent
as ``Authorization: Bearer <token>``. It is compared with ``hmac.compare_digest``
on bytes. The token is never logged, echoed or returned.

- Token set: a missing or wrong token gets 401 (BRT-API-045), from any address.
- Token unset or empty: only loopback clients (127.0.0.0/8, ::1) are allowed;
  any other address gets 403 (BRT-API-046). A request with no client address
  counts as not loopback.

Error codes:
- BRT-API-045: admin token missing or wrong
- BRT-API-046: remote admin refused, no BORG_ADMIN_TOKEN configured
- BRT-API-047: BORG_ADMIN_TOKEN holds characters other than printable ASCII
"""
from __future__ import annotations

import hmac
import ipaddress
import os
from typing import Optional

from fastapi import HTTPException, Request


def configured_token() -> str:
    """The admin token from the environment ('' when unset). Read at call time."""
    return (os.environ.get("BORG_ADMIN_TOKEN") or "").strip()


def admin_token_set() -> bool:
    return bool(configured_token())


def admin_token_valid() -> bool:
    """True when the token is unset, or is printable ASCII only (no spaces).

    HTTP headers carry ASCII, so a token with other characters could never match
    what a client sends. It is reported as misconfigured instead.
    """
    token = configured_token()
    return all(0x21 <= ord(ch) <= 0x7E for ch in token)


def _is_loopback(host: Optional[str]) -> bool:
    if not host:
        return False
    try:
        addr = ipaddress.ip_address(host.split("%", 1)[0])
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    return addr.is_loopback


def _client_host(request: Optional[Request]) -> Optional[str]:
    client = getattr(request, "client", None)
    return getattr(client, "host", None) if client is not None else None


def _bearer(request: Request) -> Optional[str]:
    header = request.headers.get("authorization") or ""
    scheme, _, value = header.partition(" ")
    if scheme.lower() != "bearer" or not value.strip():
        return None
    return value.strip()


def _token_matches(request: Request, token: str) -> bool:
    sent = _bearer(request)
    if sent is None:
        return False
    return hmac.compare_digest(sent.encode("utf-8"), token.encode("utf-8"))


def is_admin(request: Optional[Request]) -> bool:
    """True when this request may use admin routes (same rule as require_admin)."""
    if request is None:
        return False
    token = configured_token()
    if token:
        return admin_token_valid() and _token_matches(request, token)
    return _is_loopback(_client_host(request))


async def require_admin(request: Request) -> None:
    """FastAPI dependency: raise 401 or 403 unless the request is an admin."""
    token = configured_token()
    if token:
        if not admin_token_valid():
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "BRT-API-047",
                    "message": "BORG_ADMIN_TOKEN is misconfigured: use printable ASCII characters only (no spaces or accents), then restart the server.",
                },
            )
        if not _token_matches(request, token):
            raise HTTPException(
                status_code=401,
                detail={
                    "error": "BRT-API-045",
                    "message": "Admin token missing or wrong. Send 'Authorization: Bearer <BORG_ADMIN_TOKEN>'.",
                },
                headers={"WWW-Authenticate": "Bearer"},
            )
        return
    if not _is_loopback(_client_host(request)):
        raise HTTPException(
            status_code=403,
            detail={
                "error": "BRT-API-046",
                "message": "Remote admin is refused because BORG_ADMIN_TOKEN is not set on the server. Set it, or use this computer.",
            },
        )
