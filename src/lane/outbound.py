"""send_outbound(): the one door every model and tool request goes through.

With the lane off (no lane context) it is a plain ``httpx`` POST, exactly as
before. With the lane on, before anything is sent:

1. **Call guard** (design note 6.2): the URL the request is really about to
   use is judged with the locality rule. A call that must stay local (a raw
   task, or any call in ``all_local``) headed for a non-local address raises
   BRT-LANE-003 and nothing is sent. Retries pass the guard again.
2. **Content backstop** (section 7): a body headed for a non-local address,
   in a run that has documents, is checked against the document text
   (``src/lane/backstop.py``). A hit raises BRT-LANE-006; nothing is sent.
3. **Ledger** (section 9, P3): one ``outbound_requests`` row per HTTP
   request, local or not (``left_machine`` says which), with the exact JSON
   body. The row is written and the write awaited before the POST; a failed
   write blocks the request (BRT-LANE-007). After the POST the row gets its
   status, HTTP status and error. A retry is its own row (``attempt``).
   Secret fields (``api_key``) are redacted in the stored copy only.
4. Local calls are built with ``trust_env=False``, so a proxy variable cannot
   carry them off the machine.

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

import contextvars
import json
from contextlib import contextmanager
from typing import Iterator, Optional
from uuid import UUID, uuid4

import aiosqlite
import httpx

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


async def _insert_row(db_path: str, row: dict) -> None:
    async with aiosqlite.connect(db_path) as db:
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


async def _finish_row(
    db_path: str, request_id: str, status: str,
    http_status: Optional[int], error: Optional[str],
) -> None:
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """UPDATE outbound_requests SET status=?, http_status=?, error=?,
                   finished_at=CURRENT_TIMESTAMP WHERE request_id=?""",
                (status, http_status, error, request_id),
            )
            await db.commit()
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


def check_backstop(url: str, body: dict, destination: Optional[str]) -> Optional[str]:
    """The backstop's verdict for a body headed off the machine: None, or the
    refusal message (document id and rule, never the matched text)."""
    lane = current_lane()
    if lane is None or not lane.has_documents:
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
        async with httpx.AsyncClient(timeout=timeout) as client:
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
    client_kwargs: dict = {"timeout": timeout}
    if local:
        client_kwargs["trust_env"] = False
    try:
        async with httpx.AsyncClient(**client_kwargs) as client:
            resp = await client.post(url, json=body, headers=headers)
    except BaseException as exc:
        await record_end(request_id, "failed", None, f"{type(exc).__name__}: {exc}"[:500])
        raise
    status_code = getattr(resp, "status_code", None)
    failed = isinstance(status_code, int) and status_code >= 400
    await record_end(request_id, "failed" if failed else "sent", status_code,
                     f"HTTP {status_code}" if failed else None)
    return resp
