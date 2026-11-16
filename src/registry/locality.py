"""What "local" means: judged by where a request goes, never by a label.

The matrix ``source`` field is not trusted (the executor calls any unknown
model id ``lmstudio``, and a pinned endpoint is labelled ``lmstudio`` whatever
its address). Design note section 3.

- ``is_local_endpoint(url)``: the URL's host is a loopback IP literal,
  ``localhost``, or an entry of ``BORG_LOCAL_HOSTS`` (host names, IPs or CIDR
  ranges). No DNS lookup. Anything unusual is not local: user info in the
  URL, a scheme other than http/https, numeric forms ``ipaddress`` cannot parse
  as written (``2130706433``, ``0x7f.1``, ``127.1``) and ``0.0.0.0``.
- ``is_local_model(model)``: its endpoint is local, it is not an Ollama
  ``-cloud`` tag nor a row discovery marked ``remote``, and, when an allow-list
  is set (``lane_local_allowlist`` / ``BORG_LANE_LOCAL_ALLOWLIST``), its id or
  its ``host:port`` is on the list.

Pure functions; the only input outside the arguments is ``BORG_LOCAL_HOSTS``
when no host list is passed.
"""
from __future__ import annotations

import ipaddress
import os
from typing import Iterable, Optional
from urllib.parse import urlsplit

from .models import ModelInfo

LOCAL_HOSTS_ENV = "BORG_LOCAL_HOSTS"


def parse_list(raw: Optional[str]) -> list[str]:
    """Comma-separated entries, trimmed, lower-cased, empties dropped."""
    if not raw:
        return []
    return [p.strip().lower() for p in str(raw).split(",") if p.strip()]


def local_hosts_from_env() -> list[str]:
    return parse_list(os.getenv(LOCAL_HOSTS_ENV))


def _host_of(url: str) -> tuple[Optional[str], Optional[int]]:
    """(host, port) of a URL that passes the strict shape rules, else (None, None)."""
    try:
        parts = urlsplit(str(url).strip())
    except ValueError:
        return None, None
    if parts.scheme.lower() not in ("http", "https"):
        return None, None
    netloc = parts.netloc
    if not netloc or "@" in netloc:
        return None, None
    try:
        host = parts.hostname
        port = parts.port
    except ValueError:
        return None, None
    if not host:
        return None, None
    host = host.lower().rstrip(".")
    return host, port


def _ip(host: str):
    """The host as an IP address, or None when it is not a literal ``ipaddress``
    accepts as written (so ``127.1`` and ``2130706433`` are None)."""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        return None


def _in_listed(host: str, ip, listed: Iterable[str]) -> bool:
    for entry in listed:
        if "/" in entry:
            if ip is None:
                continue
            try:
                if ip in ipaddress.ip_network(entry, strict=False):
                    return True
            except ValueError:
                continue
        elif entry.rstrip(".") == host:
            return True
        elif ip is not None:
            listed_ip = _ip(entry)
            if listed_ip is not None and listed_ip == ip:
                return True
    return False


def is_local_endpoint(url: Optional[str], local_hosts: Optional[list[str]] = None) -> bool:
    """True when a request to ``url`` stays on this machine (or on a host the
    administrator listed in ``BORG_LOCAL_HOSTS``)."""
    if not url:
        return False
    host, _port = _host_of(url)
    if host is None:
        return False
    listed = local_hosts if local_hosts is not None else local_hosts_from_env()
    ip = _ip(host)
    if ip is not None:
        mapped = getattr(ip, "ipv4_mapped", None)
        if mapped is not None:
            ip = mapped
        if ip.is_unspecified:          # 0.0.0.0 and ::
            return False
        if ip.is_loopback:
            return True
        return _in_listed(host, ip, listed)
    if host == "localhost":
        return True
    return _in_listed(host, None, listed)


def is_cloud_tag(model_id: str) -> bool:
    """Ollama lists models served by a cloud service with a ``-cloud`` tag."""
    mid = (model_id or "").lower()
    return mid.endswith("-cloud") or mid.endswith(":cloud")


def host_port(url: Optional[str]) -> Optional[str]:
    """``host:port`` of a URL (port defaulted from the scheme), or None."""
    host, port = _host_of(url or "")
    if host is None:
        return None
    if port is None:
        port = 443 if str(url).lower().startswith("https") else 80
    return f"{host}:{port}"


def is_local_model(
    model: Optional[ModelInfo],
    local_hosts: Optional[list[str]] = None,
    allowlist: Optional[list[str]] = None,
    url: Optional[str] = None,
) -> bool:
    """True when ``model`` runs on this machine.

    ``url`` is the address a call is really about to use, when it differs from
    the row's endpoint (the call guard passes it); it must be local too.
    """
    if model is None:
        return False
    if not is_local_endpoint(model.endpoint, local_hosts):
        return False
    if url is not None and not is_local_endpoint(url, local_hosts):
        return False
    if getattr(model, "remote", False) or is_cloud_tag(model.model_id):
        return False
    if allowlist:
        listed = {e.lower() for e in allowlist}
        if model.model_id.lower() not in listed and (host_port(model.endpoint) or "") not in listed:
            return False
    return True
