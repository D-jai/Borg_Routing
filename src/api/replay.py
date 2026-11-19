"""Replay mode (work order 4.5): ``BORG_DEMO_MODE=replay`` plays recorded runs and nothing else.

The recorded runs are the export files ``scripts/export_run.py`` writes (``demo/runs/*.json``,
or the folder in ``BORG_REPLAY_DIR``). In replay mode ``ReplayMiddleware`` (added in main.py)
answers every request before the app's own routes see it:

- the pages (``/``, ``/ui/...``) and ``/health`` are served as usual;
- ``POST /v1/chat/completions`` with a recorded prompt starts a playback and streams the
  recorded stage events at recorded speed (times divided by a ``speed`` factor:
  ``replay_speed`` in the body, else ``BORG_REPLAY_SPEED``, else ``default_speed``: a run plays
  in at most 60 seconds), then the recorded answer,
  in the same chunks the live endpoint sends; any other prompt gets 403 (BRT-RPL-002);
- ``GET /runs/{id}`` and ``GET /runs/{id}/stream`` serve that playback (the run view as it
  stood at that moment, envelope updates as they happened, then the end); a recorded run's own
  id opens it finished; ``GET /runs`` lists the recorded runs; ``GET /models/matrix`` holds
  the recorded runs' models (id and locality only); ``GET /replay/status`` gives the prompts
  and the notice every page shows;
- everything else gets 403 (BRT-RPL-001): every route that changes state, and every route
  that could read a key or call out (Discover, local status probes, the admin reads).

No model, key, database or network is touched in replay mode: the handlers here read only the
export files. Demo runs are labelled "demo run" and back no claim (planner ruling 24).
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import uuid
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.routing import Match

logger = logging.getLogger(__name__)

FORMAT = "borg_route.replay/1"
LABEL = "demo run"
DEFAULT_DIR = Path(__file__).resolve().parents[2] / "demo" / "runs"
MAX_SESSIONS = 500
SPEED_MIN, SPEED_MAX = 0.1, 100000.0
HEARTBEAT_S = 5.0
STREAM_LIMIT_S = 90.0
_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
_TS = "%Y-%m-%d %H:%M:%S"


def demo_mode() -> Optional[str]:
    """"replay" or None (live). A value that is neither empty, ``off`` nor ``replay`` fails
    closed to replay (BRT-RPL-005): a typo on a hosted demo never opens the live app."""
    raw = (os.getenv("BORG_DEMO_MODE") or "").strip().lower()
    if raw in ("", "off"):
        return None
    if raw != "replay":
        logger.warning("BRT-RPL-005: BORG_DEMO_MODE=%r is not 'replay' or 'off'; replay mode is on", raw[:20])
    return "replay"


def _now() -> float:
    return time.monotonic()


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


# --- the recorded runs ----------------------------------------------------------------------

_cache: dict = {"key": None, "runs": {}}
_sessions: "OrderedDict[str, dict]" = OrderedDict()


def reset() -> None:
    """Forget the loaded files and every playback (tests)."""
    _cache.update(key=None, runs={})
    _sessions.clear()


def library() -> dict:
    """``{run_id: export}`` from the replay folder, reloaded when a file changes. A file that
    is not a replay export is skipped (BRT-RPL-003 in the log)."""
    folder = Path(os.getenv("BORG_REPLAY_DIR") or DEFAULT_DIR)
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    key = (str(folder), tuple((f.name, f.stat().st_mtime_ns) for f in files))
    if key == _cache["key"]:
        return _cache["runs"]
    runs: dict = {}
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.error("BRT-RPL-003: replay file %s could not be read", f.name)
            continue
        if not isinstance(doc, dict):
            continue                     # manifest.json and other lists are not exports
        if doc.get("format") != FORMAT or not isinstance(doc.get("view"), dict) \
                or not isinstance(doc.get("prompt"), str) or not doc.get("run_id"):
            logger.error("BRT-RPL-003: replay file %s is not a replay export", f.name)
            continue
        runs[str(doc["run_id"])] = doc
    if not runs:
        logger.error("BRT-RPL-003: no replay export found in %s", folder.name)
    _cache.update(key=key, runs=runs)
    return runs


MAX_PLAY_S = 60.0


def _explicit_speed(value: object = None) -> Optional[float]:
    """The speed asked for (the request's ``replay_speed``, else ``BORG_REPLAY_SPEED``),
    clamped; None when neither is a positive number."""
    for raw in (value, os.getenv("BORG_REPLAY_SPEED")):
        try:
            speed = float(raw)
        except (TypeError, ValueError):
            continue
        if speed == speed and speed > 0:
            return min(max(speed, SPEED_MIN), SPEED_MAX)
    return None


def default_speed(doc: Optional[dict]) -> float:
    """Lead ruling (4.5, item 7): with no speed asked for, a run plays in at most
    ``MAX_PLAY_S`` seconds: max(1, recorded duration / 60)."""
    end_s = ((doc or {}).get("timeline", {}).get("end_ms") or 0) / 1000.0
    return max(1.0, end_s / MAX_PLAY_S)


def _speed(value: object = None, doc: Optional[dict] = None) -> float:
    explicit = _explicit_speed(value)
    return explicit if explicit is not None else default_speed(doc)


def start_session(run_id: str, speed: Optional[float] = None) -> str:
    """Start one playback of a recorded run; returns its own run id."""
    sid = str(uuid.uuid4())
    speed = _speed(speed, library().get(run_id))
    _sessions[sid] = {"run_id": run_id, "t0": _now(), "speed": speed, "wall0": _utcnow()}
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)
    return sid


def _lookup(run_id: str) -> tuple:
    """(export, elapsed recorded ms or None for a finished view, session) or 404."""
    session = _sessions.get(run_id)
    if session is not None:
        doc = library().get(session["run_id"])
        if doc is not None:
            elapsed = (_now() - session["t0"]) * 1000.0 * session["speed"]
            return doc, elapsed, dict(session, sid=run_id)
    doc = library().get(run_id)
    if doc is not None:
        return doc, None, None
    raise HTTPException(status_code=404, detail={"error": "BRT-API-021",
                                                 "message": f"Run '{run_id}' not found"})


# --- the run view at a moment ----------------------------------------------------------------

def _parse(value: object) -> Optional[datetime]:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _shifted(row: dict, keys: tuple, shift: timedelta) -> dict:
    out = dict(row)
    for k in keys:
        t = _parse(out.get(k))
        if t is not None:
            out[k] = (t + shift).strftime(_TS)
    return out


def _upto(t: object, elapsed: float) -> bool:
    return isinstance(t, (int, float)) and t <= elapsed


def _no_ratio(view: dict) -> dict:
    """Lead ruling (4.5, item 4): replay shows nothing that reads as a proof result, so the
    digest's round-1 benchmark ratio (the receipt's "between X and Y times this floor"
    sentence) is left out. Every total stays as recorded."""
    digest = view.get("digest")
    if isinstance(digest, dict):
        digest["direct_floor_ratio"] = None
    return view


def view_at(doc: dict, elapsed: Optional[float], session: Optional[dict] = None) -> dict:
    """The recorded ``GET /runs/{id}`` body as it stood ``elapsed`` recorded ms after the run
    started (None: finished, as recorded). Times are moved to the playback's start so durations
    stay as recorded; the run id is the playback's own."""
    src = doc["view"]
    if elapsed is None or session is None:
        return _no_ratio(copy.deepcopy(src))
    end = doc.get("timeline", {}).get("end_ms") or 0
    done = elapsed >= end
    start = _parse(src["run"].get("created_at")) or session["wall0"]
    shift = session["wall0"] - start
    sid, source_id = session["sid"], src["run"].get("run_id")

    def own(row: dict) -> dict:
        return dict(row, run_id=sid) if row.get("run_id") == source_id else row

    run = own(_shifted(src["run"], ("created_at", "completed_at"), shift))
    if not done:
        run.update(status="running", completed_at=None, failure_reason=None, answer_kind=None)

    def envelope(env: dict) -> Optional[dict]:
        if not (done or _upto(env.get("t_ms"), elapsed)):
            return None
        row = own(_shifted(env, ("created_at", "completed_at"), shift))
        if not (done or _upto(env.get("t_done_ms"), elapsed)):
            row.update(status="in_flight", completed_at=None, inbound_payload=None, dollars=None,
                       tokens_input=None, tokens_output=None, latency_ms=None)
        return row

    events = [own(_shifted(ev, ("created_at",), shift)) for ev in src.get("events", [])
              if done or _upto(ev.get("t_ms"), elapsed)]
    return {
        "run": run,
        "envelopes": [e for e in map(envelope, src.get("envelopes", [])) if e is not None],
        "child_envelopes": [e for e in map(envelope, src.get("child_envelopes", [])) if e is not None],
        "events": events,
        "digest": _no_ratio({"digest": copy.deepcopy(src.get("digest"))})["digest"] if done else None,
        "outbound": [own(_shifted(o, ("started_at", "finished_at"), shift))
                     for o in src.get("outbound", []) if done or _upto(o.get("t_ms"), elapsed)],
        "calls_left_machine": copy.deepcopy(src.get("calls_left_machine")) if done
        else {"count": None, "source": None},
    }


# --- what the pages read ----------------------------------------------------------------------

def _day(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return str(iso)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def notice(runs: dict) -> str:
    dates = sorted({str(d.get("recorded_on")) for d in runs.values() if d.get("recorded_on")})
    if not dates:
        return "Replay mode: no recorded run was found"
    return "Replay of a recorded run on " + " or ".join(_day(d) for d in dates)


def status_body() -> dict:
    runs = library()
    order = sorted(runs.values(), key=lambda d: (str(d.get("job") or "~"), str(d.get("run_id"))))
    return {
        "mode": "replay",
        "label": LABEL,
        "notice": notice(runs),
        "runs": [{"run_id": d["run_id"], "job": d.get("job"), "title": d.get("title"),
                  "label": d.get("label") or LABEL, "recorded_on": d.get("recorded_on"),
                  "lane_mode": d.get("lane_mode"), "prompt": d["prompt"],
                  "seconds": (d.get("timeline", {}).get("end_ms") or 0) / 1000.0,
                  "play_seconds": (d.get("timeline", {}).get("end_ms") or 0) / 1000.0
                  / _speed(None, d)} for d in order],
    }


live_router = APIRouter()


@live_router.get("/replay/status")
async def live_status() -> dict:
    """In live mode the pages show no replay notice."""
    return {"mode": "live"}


router = APIRouter()


@router.get("/replay/status")
async def replay_status() -> dict:
    return status_body()


@router.get("/runs")
async def replay_runs(limit: int = 50, include_bypass: bool = False) -> dict:
    rows = []
    for doc in library().values():
        run = dict(doc["view"]["run"], label=doc.get("label") or LABEL)
        rows.append(run)
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    counts = {"total": len(rows), "completed": sum(r.get("status") == "completed" for r in rows),
              "failed": sum(r.get("status") == "failed" for r in rows), "thumbs_up": 0, "thumbs_down": 0}
    return {"runs": rows[:max(0, limit)], "counts": counts}


@router.get("/runs/{run_id}")
async def replay_run(run_id: str) -> JSONResponse:
    doc, elapsed, session = _lookup(run_id)
    return JSONResponse(content=view_at(doc, elapsed, session))


@router.get("/runs/{run_id}/stream")
async def replay_run_stream(run_id: str) -> StreamingResponse:
    _lookup(run_id)

    async def events():
        seen: dict = {}
        deadline = _now() + STREAM_LIMIT_S
        while True:
            doc, elapsed, session = _lookup(run_id)
            view = view_at(doc, elapsed, session)
            for env in view["envelopes"] + view["child_envelopes"]:
                if seen.get(env["envelope_id"]) != env.get("status"):
                    seen[env["envelope_id"]] = env.get("status")
                    yield f"data: {json.dumps({'type': 'envelope_update', 'envelope': env})}\n\n"
            status = view["run"].get("status")
            if status in ("completed", "failed"):
                yield f"data: {json.dumps({'type': 'run_terminal', 'status': status, 'run_id': run_id})}\n\n"
                return
            if _now() >= deadline:
                return
            await asyncio.sleep(0.25)

    return StreamingResponse(events(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@router.get("/models/matrix")
async def replay_models() -> list:
    rows: dict = {}
    for doc in library().values():
        for m in doc.get("models") or []:
            if isinstance(m, dict) and m.get("model_id"):
                rows[m["model_id"]] = {"model_id": m["model_id"], "is_local": m.get("is_local") is True}
    return [rows[k] for k in sorted(rows)]


def _refuse(code: str, message: str) -> HTTPException:
    return HTTPException(status_code=403, detail={"error": code, "message": message})


@router.post("/v1/chat/completions")
async def replay_chat(request: Request):
    """Play the recorded run whose prompt this is; any other prompt is refused."""
    from src.api.chat_completions import _build_openai_response, _stream_response  # noqa: PLC0415

    try:
        body = await request.json()
    except ValueError:
        body = None
    messages = body.get("messages") if isinstance(body, dict) else None
    users = [m for m in messages or [] if isinstance(m, dict) and m.get("role") == "user"]
    text = users[-1].get("content") if users else None
    doc = next((d for d in library().values()
                if isinstance(text, str) and d["prompt"].strip() == text.strip()), None)
    if doc is None:
        raise _refuse("BRT-RPL-002", "replay mode plays recorded runs only: choose one of the "
                                     "recorded prompts on the Ask page")
    speed = _speed(body.get("replay_speed"), doc)
    sid = start_session(doc["run_id"], speed)
    model = str(body.get("model") or "borg_route")
    answer = doc.get("answer") or ""
    end_s = (doc.get("timeline", {}).get("end_ms") or 0) / 1000.0 / speed

    async def wait_until(t: float):
        while True:
            left = t - _now()
            if left <= 0:
                return
            await asyncio.sleep(min(left, HEARTBEAT_S))
            if t - _now() > 0:
                yield f": heartbeat {int(time.time())}\n\n"

    async def play():
        t0 = _now()
        for st in doc.get("timeline", {}).get("stages") or []:
            async for beat in wait_until(t0 + (st.get("t_ms") or 0) / 1000.0 / speed):
                yield beat
            payload = {"event": "stage", "stage": st.get("stage"), "detail": st.get("detail") or "",
                       "elapsed_ms": int(st.get("t_ms") or 0)}
            yield f"data: {json.dumps(payload)}\n\n"
        async for beat in wait_until(t0 + end_s):
            yield beat
        async for chunk in _stream_response(answer, model, sid):
            yield chunk

    if body.get("stream"):
        return StreamingResponse(play(), media_type="text/event-stream")
    await asyncio.sleep(end_s)
    return JSONResponse(content=_build_openai_response(sid, model, answer, include_usage=False))


replay_app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
replay_app.include_router(router)

_OPEN_PREFIXES = ("/ui/",)
_OPEN_PATHS = ("/", "/ui", "/health")


class ReplayMiddleware:
    """Pure ASGI. Live mode: passes everything through. Replay mode: the replay routes above,
    the pages and /health; 403 (BRT-RPL-001) for anything else."""

    def __init__(self, inner):
        self.inner = inner

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or demo_mode() is None:
            return await self.inner(scope, receive, send)
        for route in replay_app.router.routes:
            match, _ = route.matches(scope)
            if match == Match.FULL:
                return await replay_app(scope, receive, send)
        method, path = scope.get("method", "GET"), scope.get("path", "")
        if method in ("GET", "HEAD") and (path in _OPEN_PATHS or path.startswith(_OPEN_PREFIXES)):
            return await self.inner(scope, receive, send)
        response = JSONResponse(status_code=403, content={"detail": {
            "error": "BRT-RPL-001",
            "message": "not available in replay mode: this server only plays recorded demo runs"}})
        await response(scope, receive, send)
