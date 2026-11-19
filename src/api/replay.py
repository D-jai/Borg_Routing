"""Replay mode (work order 4.5): ``BORG_DEMO_MODE=replay`` plays recorded runs and nothing else.

The recorded runs are the export files ``scripts/export_run.py`` writes (``demo/runs/*.json``,
or the folder in ``BORG_REPLAY_DIR``). In replay mode ``ReplayMiddleware`` (added in main.py)
answers every request before the app's own routes see it:

- the pages (``/``, ``/ui/...``) and ``/health`` are served as usual;
- ``POST /v1/chat/completions`` with a recorded prompt starts a playback and streams the
  recorded stage events at recorded speed (times divided by a ``speed`` factor:
  ``replay_speed`` in the body, else ``BORG_REPLAY_SPEED``, else ``default_speed``: a run plays
  in at most 60 seconds), with no single wait between two stages longer than ``GAP_CAP_S``
  (each stage carries its recorded duration, ``recorded_ms``, so a shortened wait is shown,
  not hidden), then the recorded answer,
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
from datetime import date, datetime, timedelta, timezone
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
GAP_CAP_S = 4.0     # planner, under ruling 38 (c): no recorded wait plays longer than this


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


def _marks(doc: Optional[dict]) -> list:
    """The recorded moments (ms) where playback may change pace: the start, every stage, the end."""
    tl = (doc or {}).get("timeline") or {}
    marks = {0.0, float(tl.get("end_ms") or 0)}
    for st in tl.get("stages") or []:
        t = st.get("t_ms") if isinstance(st, dict) else None
        if isinstance(t, (int, float)) and t >= 0:
            marks.add(float(t))
    return sorted(marks)


def play_map(doc: Optional[dict], speed: float) -> list:
    """Knots ``[(recorded ms, play seconds)]``: each recorded wait between two marks plays at
    ``speed`` but never longer than ``GAP_CAP_S`` (planner, under ruling 38 (c)); the 60 s rule
    for the whole run comes from the speed and still holds, since capping only shortens."""
    marks = _marks(doc)
    knots, play = [(marks[0], 0.0)], 0.0
    for a, b in zip(marks, marks[1:]):
        play += min((b - a) / 1000.0 / speed, GAP_CAP_S)
        knots.append((b, play))
    return knots


def play_seconds_at(knots: list, rec_ms: float) -> float:
    """Play seconds at which recorded moment ``rec_ms`` is shown."""
    prev = knots[0]
    for k in knots[1:]:
        if rec_ms <= k[0]:
            span = k[0] - prev[0]
            return prev[1] + (k[1] - prev[1]) * ((rec_ms - prev[0]) / span if span > 0 else 1.0)
        prev = k
    return knots[-1][1]


def recorded_ms_at(knots: list, play_s: float) -> float:
    """The recorded moment (ms) shown ``play_s`` seconds into a playback; past the end it
    keeps growing, so a finished playback reads as finished."""
    prev = knots[0]
    for k in knots[1:]:
        if play_s <= k[1]:
            span = k[1] - prev[1]
            return prev[0] + (k[0] - prev[0]) * ((play_s - prev[1]) / span if span > 0 else 1.0)
        prev = k
    return knots[-1][0] + (play_s - knots[-1][1]) * 1000.0 + 1.0


def start_session(run_id: str, speed: Optional[float] = None) -> str:
    """Start one playback of a recorded run; returns its own run id."""
    sid = str(uuid.uuid4())
    doc = library().get(run_id)
    speed = _speed(speed, doc)
    _sessions[sid] = {"run_id": run_id, "t0": _now(), "speed": speed, "wall0": _utcnow(),
                      "knots": play_map(doc, speed)}
    while len(_sessions) > MAX_SESSIONS:
        _sessions.popitem(last=False)
    return sid


def _lookup(run_id: str) -> tuple:
    """(export, elapsed recorded ms or None for a finished view, session) or 404."""
    session = _sessions.get(run_id)
    if session is not None:
        doc = library().get(session["run_id"])
        if doc is not None:
            elapsed = recorded_ms_at(session["knots"], _now() - session["t0"])
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

def _nth_sunday(year: int, month: int, n: int) -> date:
    first = date(year, month, 1)
    return first + timedelta(days=(6 - first.weekday()) % 7 + 7 * (n - 1))


def toronto_date(utc: datetime) -> date:
    """The calendar date in Toronto (America/Toronto) of a naive UTC time. Uses the zone
    database when there is one, else the Eastern rule in force since 2007 (EDT, UTC-4, from
    02:00 on the second Sunday of March to 02:00 on the first Sunday of November; else EST,
    UTC-5): Windows has no zone database without the tzdata package."""
    try:
        from zoneinfo import ZoneInfo  # noqa: PLC0415

        return utc.replace(tzinfo=timezone.utc).astimezone(ZoneInfo("America/Toronto")).date()
    except Exception:
        pass
    y = utc.year
    dst_start = datetime.combine(_nth_sunday(y, 3, 2), datetime.min.time()) + timedelta(hours=7)
    dst_end = datetime.combine(_nth_sunday(y, 11, 1), datetime.min.time()) + timedelta(hours=6)
    offset = 4 if dst_start <= utc < dst_end else 5
    return (utc - timedelta(hours=offset)).date()


def _recorded_day(doc: dict) -> Optional[str]:
    """The Toronto date the run started, from its stored UTC time; else the export's own."""
    started = _parse((doc.get("view") or {}).get("run", {}).get("created_at"))
    if started is not None:
        return toronto_date(started).isoformat()
    return doc.get("recorded_on")


def _day(iso: str) -> str:
    try:
        d = datetime.strptime(iso, "%Y-%m-%d")
    except (TypeError, ValueError):
        return str(iso)
    return f"{d.day:02d}-{_MONTHS[d.month - 1]}-{d.year}"


def notice(runs: dict) -> str:
    dates = sorted({day for day in map(_recorded_day, runs.values()) if day})
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
                  "play_seconds": play_map(d, _speed(None, d))[-1][1]} for d in order],
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
    knots = _sessions[sid]["knots"]
    end_ms = float(doc.get("timeline", {}).get("end_ms") or 0)
    end_s = play_seconds_at(knots, end_ms)
    stages = [st for st in doc.get("timeline", {}).get("stages") or [] if isinstance(st, dict)]

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
        for i, st in enumerate(stages):
            t_ms = float(st.get("t_ms") or 0)
            async for beat in wait_until(t0 + play_seconds_at(knots, t_ms)):
                yield beat
            nxt = float(stages[i + 1].get("t_ms") or 0) if i + 1 < len(stages) else end_ms
            payload = {"event": "stage", "stage": st.get("stage"), "detail": st.get("detail") or "",
                       "elapsed_ms": int(t_ms), "recorded_ms": int(max(0.0, nxt - t_ms))}
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
