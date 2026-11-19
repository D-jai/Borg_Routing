"""Where a model runs, worked out by the server for the pages (4.2 review).

The pages never guess locality from a source label. ``is_local`` on a model
row and ``calls_left_machine`` on a run come from ``src/registry/locality.py``
(endpoint host: loopback or ``BORG_LOCAL_HOSTS``; ``-cloud`` tags and rows
marked ``remote`` refused; the ``lane_local_allowlist`` when set).

``calls_left_machine`` is ``{"count": int | None, "source": "ledger" | "locality" | None}``:
- ``ledger``: the run was made with the lane on (or has ledger rows); the
  count is the number of requests the outbound ledger shows leaving the machine,
  child runs included (an empty ledger is a real 0).
- ``locality``: no ledger; each logged model call is judged by its model's
  locality in the matrix. Descendant runs' calls are included.
- unknown (``count`` null, ``source`` null): a call's model is not in the matrix
  or no call was logged. Never 0.
"""
from __future__ import annotations

import os
from typing import Optional

from src.registry.locality import is_local_model, local_hosts_from_env, parse_list

_CALL_EVENTS = ("model_call", "orchestrator_decision", "tool_call")


async def locality_rules(db_path: str) -> tuple[list[str], list[str]]:
    """(local hosts, allow-list) as the lane would apply them. The allow-list
    is read whatever the lane mode, so a model row is judged by the same rule
    the lane would use. An unreadable config falls back to the env value."""
    allow_raw = None
    try:
        from src.db.state import get_config  # noqa: PLC0415

        allow_raw = await get_config("lane_local_allowlist", db_path=db_path)
    except Exception:
        allow_raw = None
    if allow_raw is None or str(allow_raw).strip() == "":
        allow_raw = os.getenv("BORG_LANE_LOCAL_ALLOWLIST")
    if isinstance(allow_raw, list):
        allow_raw = ",".join(map(str, allow_raw))
    return local_hosts_from_env(), parse_list(allow_raw)


def model_is_local(model, rules: tuple[list[str], list[str]]) -> bool:
    hosts, allow = rules
    return is_local_model(model, local_hosts=hosts, allowlist=allow or None)


def model_row(model, rules) -> dict:
    """A matrix row as the pages read it, with the computed ``is_local``."""
    row = model.model_dump()
    row["is_local"] = model_is_local(model, rules)
    return row


def _is_call(ev: dict) -> bool:
    return (
        ev.get("event_type") in _CALL_EVENTS
        and bool(ev.get("model_id"))
        and (ev.get("tokens_input") is not None or ev.get("dollars") is not None)
    )


def _is_post(ev: dict) -> bool:
    """A logged call counted once per post: a packed call's item shares
    count once, by the first share (planner ruling 23)."""
    from src.orchestrator.packing import counts_as_call  # noqa: PLC0415

    return _is_call(ev) and counts_as_call(ev.get("decision_json"))


def _left_by_rules(model_id: str, matrix, rules) -> Optional[bool]:
    """True when a call to ``model_id`` left the machine, False when it did
    not, None when its locality is unknown. A ``tool:*`` id (web search) is a
    call to an outside service, so it always left."""
    if model_id.startswith("tool:"):
        return True
    model = matrix.get(model_id) if matrix is not None else None
    if model is None:
        return None
    return not model_is_local(model, rules)


def calls_left_machine(
    *,
    lane_mode: Optional[str],
    outbound: list[dict],
    events: list[dict],
    matrix,
    rules,
    envelopes: Optional[list[dict]] = None,
) -> dict:
    """See the module docstring. ``events`` are the log rows of the run and
    every descendant run; ``envelopes`` are their envelope rows. With the lane
    off, a step whose model call failed has an envelope but no ``model_call``
    event, so the envelope's model counts too (once: a step with an event is
    not counted twice). The allow-list applies only when the lane is on."""
    lane_on = lane_mode in ("raw_local", "all_local", "by_reference")
    if lane_on or outbound:
        return {"count": len(outbound), "source": "ledger"}
    hosts, allow = rules
    rules = (hosts, allow if lane_on else [])
    calls = [ev for ev in events if _is_post(ev)]
    seen = {str(ev["envelope_id"]) for ev in events if _is_call(ev) and ev.get("envelope_id")}
    judged = [ev["model_id"] for ev in calls]
    for env in envelopes or []:
        mid = env.get("model_id")
        if (mid and env.get("type") == "answerer"
                and str(env.get("envelope_id")) not in seen):
            judged.append(mid)
    if not judged:
        return {"count": None, "source": None}
    count = 0
    for mid in judged:
        left = _left_by_rules(mid, matrix, rules)
        if left is None:
            return {"count": None, "source": None}
        count += 1 if left else 0
    return {"count": count, "source": "locality"}
