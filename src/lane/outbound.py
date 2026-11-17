"""send_outbound(): the one door every model and tool request goes through.

With the lane off (no lane context) it is a plain ``httpx`` POST, exactly as
before. With the lane on, before anything is sent:

1. **Call guard** (design note 6.2): the URL the request is really about to
   use is judged with the locality rule. A call that must stay local (a raw
   task, or any call in ``all_local``) headed for a non-local address raises
   BRT-LANE-003 and nothing is sent. Retries pass the guard again.
2. **Content backstop** (section 7): a body headed for a non-local address,
   in a run that has documents (``raw_local``/``all_local``; not in
   ``by_reference``), is checked against the document text
   (``src/lane/backstop.py``). A hit raises BRT-LANE-006; nothing is sent.
3. **Ledger** (section 9, P3): one ``outbound_requests`` row per HTTP
   request, local or not (``left_machine`` says which), with the exact JSON
   body. The row is written and the write awaited before the POST; a failed
   write blocks the request (BRT-LANE-007). After the POST the row gets its
   status, HTTP status and error. A retry is its own row (``attempt``).
   Ledger writes for one database go through one serialised writer, each
   connection waits for the lock (busy timeout, 5 s per try), and a
   "database is locked" is retried with backoff (5 tries); one write has a
   total budget of 30 s, then the send is blocked.
   Secret fields (``api_key``) are redacted in the stored copy only.
4. Local calls are built with ``trust_env=False``, so a proxy variable cannot
   carry them off the machine. Clients are shared per event loop, one per
   (trust_env, timeout) kind (work order 1.12, ``shared_client``).

Callers keep their own error mapping: this function returns the response or
lets the ``httpx`` exception through; only lane refusals raise ``LaneError``.

``call_scope`` tells the door which run and envelope a request belongs to
(set by the executor and the orchestrator around their calls), so a refusal
or a ledger row is filed against the right run.

Error codes:
- BRT-LANE-003: a call that must stay local was about to go to a non-local
  endpoint; blocked (indicates a defect in marking or routing)
- BRT-LANE-006: document text found in a request to a non-local endpoint; blocked
- BRT-LANE-007: the ledger row could not be written; request not sent
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Iterator, Optional
from uuid import UUID, uuid4

import httpx

from src.db.connect import connect
from src.registry.models import ModelInfo

from .backstop import find_leak
from .context import current_lane
from .errors import LaneError

_SECRET_KEYS = {"api_key", "apikey", "key", "authorization", "token", "password"}

_scope: contextvars.ContextVar[dict] = contextvars.ContextVar("borg_call_scope", default={})


@contextmanager
def call_scope(run_id: Optional[UUID] = None, envelope_id: Optional[UUID] = None) -> Iterator[None]:
    token = _scope.set({"run_id": run_id, "envelope_id": envelope_id})
    try:
        yield
    finally:
        _scope.reset(token)


def current_scope() -> dict:
    return dict(_scope.get())


async def log_refusal(code: str, message: str, url: str, destination: Optional[str]) -> None:
    """Write a lane refusal to the run's local trace. Never raises."""
    lane = current_lane()
    if lane is None:
        return
    scope = current_scope()
    try:
        from src.logging.writer import write_event  # noqa: PLC0415

        await write_event(
            "error",
            run_id=scope.get("run_id"),
            envelope_id=scope.get("envelope_id"),
            decision={"code": code, "msg": message, "url": url, "destination": destination},
            db_path=lane.db_path,
        )
    except Exception:
        pass


def _redacted(body: object) -> object:
    if isinstance(body, dict):
        return {
            k: ("[redacted]" if str(k).lower() in _SECRET_KEYS else _redacted(v))
            for k, v in body.items()
        }
    if isinstance(body, list):
        return [_redacted(v) for v in body]
    return body


# One serialised writer per database: concurrent sends (an apply_to_each of
# many parts) queue here instead of racing for SQLite's write lock. The lock
# is per event loop (an asyncio.Lock belongs to one loop) and per path. The
# locks are kept on the loop object itself, never in a module-level map: a
# used asyncio.Lock refers to its loop, so a map keyed by loop would keep
# every finished loop alive.
_LOOP_LOCKS_ATTR = "_borg_ledger_locks"
_RETRY_TRIES = 5            # attempts on "database is locked" before giving up
_RETRY_BASE_SECONDS = 0.05  # backoff: 0.05, 0.1, 0.2, 0.4 s between attempts
# One ledger write (insert or finish) has this total budget across the lock
# wait, every try and the backoff; then it fails (the send is blocked,
# BRT-LANE-007). Each try waits for SQLite's lock at most _TRY_TIMEOUT_SECONDS
# (its busy timeout), so 5 tries plus backoff (about 25.8 s) fit the budget.
_WRITE_BUDGET_SECONDS = 30.0
_TRY_TIMEOUT_SECONDS = 5.0

_try_timeout: contextvars.ContextVar[Optional[float]] = contextvars.ContextVar(
    "borg_ledger_try_timeout", default=None
)


def loop_store(name: str) -> dict:
    """A dict kept as attribute ``name`` on the running event loop: per-loop
    state that ends with the loop (no module-level reference to the loop)."""
    loop = asyncio.get_running_loop()
    store = getattr(loop, name, None)
    if store is None:
        store = {}
        setattr(loop, name, store)
    return store


def _writer_lock(db_path: str) -> asyncio.Lock:
    per_loop = loop_store(_LOOP_LOCKS_ATTR)
    key = os.path.normcase(os.path.abspath(db_path))
    lock = per_loop.get(key)
    if lock is None:
        lock = per_loop[key] = asyncio.Lock()
    return lock


def _is_locked(exc: BaseException) -> bool:
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower()
    )


def _budget_spent() -> sqlite3.OperationalError:
    return sqlite3.OperationalError(
        f"database is locked: ledger write budget ({_WRITE_BUDGET_SECONDS:g} s) spent"
    )


async def _serialised(db_path: str, write) -> None:
    """Run ``write()`` under the database's writer lock, retrying a
    "database is locked" up to ``_RETRY_TRIES`` times with backoff, all
    within ``_WRITE_BUDGET_SECONDS``. The last failure is raised."""
    deadline = time.monotonic() + _WRITE_BUDGET_SECONDS
    lock = _writer_lock(db_path)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=_WRITE_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        raise _budget_spent() from None
    try:
        for attempt in range(1, _RETRY_TRIES + 1):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise _budget_spent()
            token = _try_timeout.set(min(_TRY_TIMEOUT_SECONDS, remaining))
            try:
                await write()
                return
            except Exception as exc:
                if not _is_locked(exc) or attempt == _RETRY_TRIES:
                    raise
            finally:
                _try_timeout.reset(token)
            pause = _RETRY_BASE_SECONDS * (2 ** (attempt - 1))
            if time.monotonic() + pause >= deadline:
                raise _budget_spent()
            await asyncio.sleep(pause)
    finally:
        lock.release()


def _ledger_connect(db_path: str):
    """A connection whose busy timeout is this try's share of the budget."""
    seconds = _try_timeout.get()
    return connect(db_path, timeout_s=seconds if seconds is not None else _TRY_TIMEOUT_SECONDS)


async def _insert_once(db_path: str, row: dict) -> None:
    async with _ledger_connect(db_path) as db:
        await db.execute(
            """INSERT INTO outbound_requests(
                request_id, run_id, envelope_id, attempt, purpose, destination,
                url, left_machine, body, status
            ) VALUES (?,?,?,?,?,?,?,?,?,'pending')""",
            (
                row["request_id"], row["run_id"], row["envelope_id"], row["attempt"],
                row["purpose"], row["destination"], row["url"], row["left_machine"],
                row["body"],
            ),
        )
        await db.commit()


async def _insert_row(db_path: str, row: dict) -> None:
    """Write the ledger row; raises when it still cannot be written."""
    await _serialised(db_path, lambda: _insert_once(db_path, row))


async def _finish_once(
    db_path: str, request_id: str, status: str,
    http_status: Optional[int], error: Optional[str],
) -> None:
    async with _ledger_connect(db_path) as db:
        await db.execute(
            """UPDATE outbound_requests SET status=?, http_status=?, error=?,
               finished_at=CURRENT_TIMESTAMP WHERE request_id=?""",
            (status, http_status, error, request_id),
        )
        await db.commit()


async def _finish_row(
    db_path: str, request_id: str, status: str,
    http_status: Optional[int], error: Optional[str],
) -> None:
    try:
        await _serialised(
            db_path, lambda: _finish_once(db_path, request_id, status, http_status, error)
        )
    except Exception:
        pass  # the row already exists; only its outcome is missing


async def record_start(
    url: str, body: dict, *, left_machine: bool, purpose: str,
    destination: Optional[str], attempt: int = 1,
) -> Optional[str]:
    """Write the ledger row for a request about to be sent; returns its id.
    None when the lane is off.

    Raises:
        LaneError: BRT-LANE-007 when the row cannot be written.
    """
    lane = current_lane()
    if lane is None:
        return None
    scope = current_scope()
    request_id = str(uuid4())
    row = {
        "request_id": request_id,
        "run_id": str(scope["run_id"]) if scope.get("run_id") else None,
        "envelope_id": str(scope["envelope_id"]) if scope.get("envelope_id") else None,
        "attempt": attempt,
        "purpose": purpose,
        "destination": destination,
        "url": url,
        "left_machine": 1 if left_machine else 0,
        "body": json.dumps(_redacted(body), ensure_ascii=False),
    }
    try:
        await _insert_row(lane.db_path, row)
    except Exception as exc:
        message = (
            f"the ledger row for a request to {url} could not be written ({type(exc).__name__}); "
            "the request was not sent"
        )
        await log_refusal("BRT-LANE-007", message, url, destination)
        raise LaneError("BRT-LANE-007", message) from None
    return request_id


async def record_end(
    request_id: Optional[str], status: str,
    http_status: Optional[int] = None, error: Optional[str] = None,
) -> None:
    lane = current_lane()
    if lane is None or request_id is None:
        return
    await _finish_row(lane.db_path, request_id, status, http_status, error)


# -- shared HTTP clients (work order 1.12) ------------------------------------
#
# Building an httpx.AsyncClient loads the CA bundle, which blocks the event
# loop (about 1.5 s on the ThinkPad). Every model and tool send therefore
# reuses one client per kind, (trust_env, timeout), created lazily per event
# loop and kept on the loop object (no module-level reference to a loop).
# Lifecycle: the clients are closed when their loop ends, by an async
# generator the loop finalises in ``shutdown_asyncgens`` (asyncio.run, the
# asyncio.Runner of uvicorn and pytest-asyncio). ``close_shared_clients``
# closes them earlier on demand.

_LOOP_CLIENTS_ATTR = "_borg_http_clients"


async def _aclose_all(clients: dict) -> None:
    for client in list(clients.values()):
        aclose = getattr(client, "aclose", None)
        if aclose is None:
            continue
        try:
            await aclose()
        except Exception:
            pass
    clients.clear()


async def _close_at_loop_end(clients: dict):
    """Started once per loop; the loop closes it (and so the clients) when
    it shuts down its async generators."""
    try:
        yield
    finally:
        await _aclose_all(clients)


async def shared_client(trust_env: bool, timeout: float) -> httpx.AsyncClient:
    """The running loop's long-lived client of this kind; do not close it.

    ``trust_env=False`` builds ``AsyncClient(timeout=..., trust_env=False)``
    (proxy variables ignored); ``True`` builds today's
    ``AsyncClient(timeout=...)``. The key includes the class, so a test that
    patches ``httpx.AsyncClient`` gets its own entry.
    """
    loop = asyncio.get_running_loop()
    state = getattr(loop, _LOOP_CLIENTS_ATTR, None)
    if state is None:
        clients: dict = {}
        closer = _close_at_loop_end(clients)
        await closer.asend(None)   # runs to its yield; the loop now tracks it
        state = (clients, closer)
        setattr(loop, _LOOP_CLIENTS_ATTR, state)
    clients = state[0]
    cls = httpx.AsyncClient
    key = (cls, bool(trust_env), float(timeout))
    client = clients.get(key)
    if client is None or getattr(client, "is_closed", False) is True:
        client = cls(timeout=timeout) if trust_env else cls(timeout=timeout, trust_env=False)
        clients[key] = client
    return client


async def close_shared_clients() -> None:
    """Close the running loop's shared clients now (they are rebuilt on the
    next send)."""
    state = getattr(asyncio.get_running_loop(), _LOOP_CLIENTS_ATTR, None)
    if state is not None:
        await _aclose_all(state[0])


async def client_for(local: bool, timeout: float) -> httpx.AsyncClient:
    """THE client a lane call uses: the shared client of its kind.

    A local target gets ``trust_env=False``, so ``HTTP_PROXY``/``ALL_PROXY``
    cannot carry it off the machine (design note section 3). Used by
    ``send_outbound`` and by the streamed bypass relay. The client is shared:
    do not close it."""
    return await shared_client(not local, timeout)


def check_backstop(url: str, body: dict, destination: Optional[str]) -> Optional[str]:
    """The backstop's verdict for a body headed off the machine: None, or the
    refusal message (document id and rule, never the matched text).
    Never runs in ``by_reference`` (no privacy promise; the ledger still
    records every request)."""
    lane = current_lane()
    if lane is None or not lane.has_documents or not lane.keeps_raw_local:
        return None
    hit = find_leak(
        body, lane.indexes(),
        lane.settings.max_verbatim_chars, lane.settings.max_ngram_share,
    )
    if hit is None:
        return None
    doc_id, rule = hit
    return (
        f"text of document {doc_id} ({rule}) found in a request to {url} "
        f"({destination}); blocked, nothing was sent"
    )


async def send_outbound(
    url: str,
    body: dict,
    headers: Optional[dict] = None,
    *,
    timeout: float,
    model: Optional[ModelInfo] = None,
    must_stay_local: bool = False,
    purpose: str = "model",
    destination: Optional[str] = None,
    attempt: int = 1,
) -> httpx.Response:
    """POST ``body`` as JSON to ``url`` through the lane's checks.

    Args:
        model: the matrix row being called, when it is a model call; its
            locality (address, ``-cloud`` tag, allow-list) is judged with the
            real URL. Tool calls pass None and are judged by URL alone.
        must_stay_local: the call guard applies (raw task, or ``all_local``).
        purpose, destination, attempt: what the call is, for the record.

    Raises:
        LaneError: BRT-LANE-003 (call guard), BRT-LANE-006 (backstop),
            BRT-LANE-007 (ledger write failed).
        httpx.HTTPError: as the plain POST would.
    """
    lane = current_lane()
    if lane is None:
        client = await shared_client(True, timeout)
        return await client.post(url, json=body, headers=headers)

    local = lane.is_local(model, url) if model is not None else lane.is_local_url(url)
    dest = destination or (model.model_id if model is not None else url)
    if must_stay_local and not local:
        message = (
            f"a call that must stay local was about to go to {url} ({dest}); "
            "blocked, nothing was sent"
        )
        await log_refusal("BRT-LANE-003", message, url, dest)
        raise LaneError("BRT-LANE-003", message)

    if not local:
        refusal = check_backstop(url, body, dest)
        if refusal is not None:
            await log_refusal("BRT-LANE-006", refusal, url, dest)
            raise LaneError("BRT-LANE-006", refusal)

    request_id = await record_start(
        url, body, left_machine=not local, purpose=purpose,
        destination=dest, attempt=attempt,
    )
    try:
        client = await client_for(local, timeout)
        resp = await client.post(url, json=body, headers=headers)
    except BaseException as exc:
        await record_end(request_id, "failed", None, f"{type(exc).__name__}: {exc}"[:500])
        raise
    status_code = getattr(resp, "status_code", None)
    failed = isinstance(status_code, int) and status_code >= 400
    await record_end(request_id, "failed" if failed else "sent", status_code,
                     f"HTTP {status_code}" if failed else None)
    return resp
