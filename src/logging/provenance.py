"""Provenance singletons — instance_id, source_version, domain_profile.

Resolved once at process start, cached, and stamped onto every log row so
The_Borg_DB (or any downstream consumer) can answer:
    - which device produced this log? (instance_id)
    - which version of BORG_ROUTE? (source_version)
    - which vertical / domain profile was active? (domain_profile)

Resolution priority:
    instance_id     -> env var BORG_INSTANCE_ID -> hash(hostname + uid)
    source_version  -> env var BORG_SOURCE_VERSION -> `git rev-parse --short HEAD` -> 'unknown'
    domain_profile  -> env var BORG_DOMAIN_PROFILE -> 'general'

All resolutions are cheap, deterministic, and run-once. Subprocess calls
to git are guarded with a 1-second timeout and never raise; failures fall
back to 'unknown'.

Error codes:
- BRT-LOG-PROV-001: subprocess git probe timed out (logged once, downgraded to 'unknown')
"""
from __future__ import annotations

import hashlib
import os
import socket
import subprocess
import sys
from functools import lru_cache
from typing import Optional


@lru_cache(maxsize=1)
def resolve_instance_id() -> str:
    """Stable identifier for this machine + user combination.

    Used by The_Borg_DB hive-collective pattern to tell apart logs from
    different devices that push to the same aggregator. Same machine +
    same user always returns the same id; different users on the same
    box get different ids.
    """
    explicit = os.getenv("BORG_INSTANCE_ID")
    if explicit:
        return explicit.strip()

    hostname = socket.gethostname() or "unknown-host"
    # On Windows getuid is missing; fall back to USERNAME / USER env.
    uid_part: str
    try:
        uid_part = str(os.getuid())  # type: ignore[attr-defined]
    except AttributeError:
        uid_part = os.getenv("USERNAME") or os.getenv("USER") or "unknown-user"

    digest = hashlib.sha256(f"{hostname}:{uid_part}".encode()).hexdigest()[:16]
    return f"borg-route:{digest}"


@lru_cache(maxsize=1)
def resolve_source_version() -> str:
    """Best-effort identifier of the running BORG_ROUTE codebase version.

    Used by The_Borg_DB to weight the trustworthiness of older vs newer
    extractions as ROUTE matures. Returns 'unknown' if neither env var
    nor git is available; never raises.
    """
    explicit = os.getenv("BORG_SOURCE_VERSION")
    if explicit:
        return explicit.strip()

    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=1.0,
            check=False,
        )
        sha = (result.stdout or "").strip()
        if sha and result.returncode == 0:
            return f"git:{sha}"
    except subprocess.TimeoutExpired:
        print(
            "BRT-LOG-PROV-001: git rev-parse timed out; using 'unknown'",
            file=sys.stderr,
        )
    except Exception:
        # FileNotFoundError when git isn't installed; OSError on weird
        # filesystems. Either way, downgrade silently.
        pass

    return "unknown"


@lru_cache(maxsize=1)
def resolve_domain_profile() -> str:
    """The active vertical / domain profile (healthcare, legal, general...).

    Today this is purely env-driven. When ROUTE adopts The_Borg_DB's domain
    profile YAML system, this should read the active profile name from
    the loaded profile config instead.
    """
    return (os.getenv("BORG_DOMAIN_PROFILE") or "general").strip()


def _reset_for_tests() -> None:
    """Clear the lru_caches — for unit tests that twiddle env vars."""
    resolve_instance_id.cache_clear()
    resolve_source_version.cache_clear()
    resolve_domain_profile.cache_clear()
