"""Scripted stand-in for every model server the engine talks to.

Installs over ``httpx.AsyncClient.post`` so the real engine, executor and
router run unchanged while no request leaves the process. It answers by the
kind of call (plan, re_plan, compose, answerer) and records every request,
so a test can assert which model served which step and what each was told.

Test-only. Nothing in ``src/`` imports this module.
"""
from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

from src.registry.capability_matrix import CapabilityMatrix
from src.registry.models import ModelInfo, Pricing

_MODE_RE = re.compile(r"You are currently in \*\*(\w+)\*\* mode")
_INSTRUCTION_RE = re.compile(
    r"## Original User Instruction \(verbatim, do not rephrase or compress\)\n(.*?)\n\n---\n",
    re.DOTALL,
)
# Ruling 23: a packed call's frame, and one item block (start line, the item
# as one JSON string, end line).
PACKED_MARK = "# Packed Task"
_ITEM_BLOCK_RE = re.compile(r"^<<<item (\w+)>>>\n(.*)\n<<<end item \1>>>$", re.MULTILINE)

TASK_TYPES = ("reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction")


@dataclass
class StubRequest:
    url: str
    model: str          # the model id sent in the request body
    kind: str           # plan | re_plan | compose | answerer | packed (ruling 23)
    system: str
    user: str
    max_tokens: Optional[int]
    body: dict = field(default_factory=dict)   # the exact JSON body received
    seq: int = 0                 # order in which requests arrived
    in_flight_at_start: int = 0  # requests in flight when this one arrived (itself included)
    finished_seq: Optional[int] = None  # how many requests had finished before this one ended
    finished_before_start: int = 0      # how many requests had finished when this one arrived

    @property
    def pack_items(self) -> dict:
        """A packed request's items, ``{id: item text}``, in prompt order."""
        return {m.group(1): json.loads(m.group(2)) for m in _ITEM_BLOCK_RE.finditer(self.system)}

    @property
    def pack_ids(self) -> list:
        return list(self.pack_items)

    @property
    def is_local(self) -> bool:
        """True when the request went to the stand-in local server."""
        return self.url.startswith("http://127.0.0.1")

    @property
    def governance_instruction(self) -> Optional[str]:
        """The "Original User Instruction" block of the system prompt."""
        m = _INSTRUCTION_RE.search(self.system)
        return m.group(1) if m else None


class _StubResponse:
    status_code = 200

    def __init__(self, body: dict) -> None:
        self._body = body
        self.text = json.dumps(body)

    def json(self) -> dict:
        return self._body

    def raise_for_status(self) -> None:
        return None


@dataclass
class StubModels:
    """Answers model calls from a script and records them.

    plan_nodes:    the nodes the planner returns in plan mode.
    compose_text:  the final answer returned in compose mode.
    plan_for:      optional ``fn(request) -> nodes``; overrides plan_nodes, so a
                   child run can be given a different plan from its parent.
    replan_for:    optional ``fn(request) -> dict``; the re_plan answer (default
                   is ``{"action": "compose"}``), so a test can send a second wave.
    answer_for:    optional ``fn(request) -> dict`` giving an answerer's
                   ``output``; defaults to ``{"result": "done by <model>"}``.
    completion_tokens: reported for every call (prompt tokens are derived
                   from the prompt length), so costs are computable.
    compose_for:   optional ``fn(request)`` scripting each compose call (the
                   first, a re-ask, a retry). It returns the reply: a dict
                   (sent as JSON), a str (sent as is, e.g. cut-off JSON), a
                   ``(reply, finish_reason)`` tuple, or an Exception (raised).
    pack_for:      optional ``fn(request)`` scripting a packed call (ruling
                   23), with the same return forms as ``compose_for``. By
                   default every item id is answered with
                   ``pack_item_for(request, item_text)`` (default
                   ``{"result": "done by <model>"}``).
    packed_tokens_per_item: when set, a packed call reports this many
                   completion tokens per item, and is cut at its ``max_tokens``
                   (``finish_reason: "length"``, the reply's first half).
    delay_for:     optional ``fn(request) -> seconds``, awaited before the
                   reply, so concurrency can be observed.
    """
    plan_nodes: list[dict]
    compose_text: str = "Final answer."
    answer_for: Optional[Callable[[StubRequest], dict]] = None
    plan_for: Optional[Callable[[StubRequest], list[dict]]] = None
    replan_for: Optional[Callable[[StubRequest], dict]] = None
    completion_tokens: int = 50
    plan_content_for: Optional[Callable[[StubRequest], dict]] = None
    error_for: Optional[Callable[[StubRequest], Optional[Exception]]] = None
    finish_for: Optional[Callable[[StubRequest], str]] = None
    compose_for: Optional[Callable[[StubRequest], Any]] = None
    pack_for: Optional[Callable[[StubRequest], Any]] = None
    pack_item_for: Optional[Callable[[StubRequest, Any], dict]] = None
    packed_tokens_per_item: Optional[int] = None
    delay_for: Optional[Callable[[StubRequest], float]] = None
    requests: list[StubRequest] = field(default_factory=list)
    in_flight: int = 0
    max_in_flight: int = 0
    finished: int = 0

    def install(self, monkeypatch) -> "StubModels":
        stub = self

        async def fake_post(client, url, json=None, headers=None, **kwargs):  # noqa: A002
            stub.in_flight += 1
            stub.max_in_flight = max(stub.max_in_flight, stub.in_flight)
            req = None
            try:
                if type(stub)._answer is not StubModels._answer:
                    # A subclass that edits replies in ``_answer`` keeps working.
                    resp = stub._answer(str(url), json or {})
                    req = stub.requests[-1] if stub.requests else None
                    return resp
                req = stub._record(str(url), json or {})
                req.in_flight_at_start = stub.in_flight
                if stub.delay_for is not None:
                    delay = stub.delay_for(req)
                    if delay:
                        await asyncio.sleep(delay)
                return stub._reply(req)
            finally:
                stub.in_flight -= 1
                stub.finished += 1
                if req is not None:
                    req.finished_seq = stub.finished

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return self

    # -- request handling -------------------------------------------------

    def _answer(self, url: str, payload: dict) -> _StubResponse:
        return self._reply(self._record(url, payload))

    def _record(self, url: str, payload: dict) -> StubRequest:
        messages = payload.get("messages") or []
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if "tavily" in url:
            kind = "tool"
        elif "response_format" in payload:
            m = _MODE_RE.search(system)
            kind = m.group(1) if m else "plan"
        elif PACKED_MARK in system:
            kind = "packed"
        else:
            kind = "answerer"
        req = StubRequest(
            url=url,
            model=str(payload.get("model", "")),
            kind=kind,
            system=system,
            user=user,
            max_tokens=payload.get("max_tokens"),
            body=json.loads(json.dumps(payload)),
            seq=len(self.requests),
            finished_before_start=self.finished,
        )
        self.requests.append(req)
        return req

    def _packed_content(self, req: StubRequest) -> Any:
        if self.pack_for is not None:
            return self.pack_for(req)
        out = {}
        for iid, text in req.pack_items.items():
            value = (
                self.pack_item_for(req, text) if self.pack_item_for
                else {"result": f"done by {req.model}"}
            )
            out[iid] = {"status": "resolved", "confidence": 0.9, "output": value}
        return out

    def _reply(self, req: StubRequest) -> _StubResponse:
        kind = req.kind
        system = req.system
        completion_tokens = self.completion_tokens

        if self.error_for is not None:
            exc = self.error_for(req)
            if exc is not None:
                raise exc

        if kind == "tool":
            return _StubResponse(
                {"results": [{"title": "stub", "url": "https://example.test", "content": "stub"}]}
            )
        if kind == "plan" and self.plan_content_for is not None:
            content: Any = self.plan_content_for(req)
        elif kind == "plan":
            nodes = self.plan_for(req) if self.plan_for else self.plan_nodes
            content = {"plan": {"nodes": nodes}, "rationale": "scripted plan"}
        elif kind == "re_plan":
            content = (
                self.replan_for(req) if self.replan_for
                else {"action": "compose", "rationale": "all steps done"}
            )
        elif kind == "compose" and self.compose_for is not None:
            content = self.compose_for(req)
        elif kind == "compose":
            content = {"response": self.compose_text, "rationale": "scripted compose"}
        elif kind == "packed":
            content = self._packed_content(req)
            if self.packed_tokens_per_item is not None:
                completion_tokens = self.packed_tokens_per_item * len(req.pack_ids)
                if req.max_tokens is not None and completion_tokens > req.max_tokens:
                    text = json.dumps(content)
                    content = (text[: len(text) // 2], "length")
                    completion_tokens = req.max_tokens
        else:
            output = (
                self.answer_for(req) if self.answer_for else {"result": f"done by {req.model}"}
            )
            content = {"status": "resolved", "confidence": 0.9, "output": output}

        finish = self.finish_for(req) if self.finish_for else "stop"
        if isinstance(content, Exception):
            raise content
        if isinstance(content, tuple):
            content, finish = content
        text = content if isinstance(content, str) else json.dumps(content)
        return _StubResponse(
            {
                "choices": [
                    {"message": {"content": text}, "finish_reason": finish}
                ],
                "usage": {
                    "prompt_tokens": max(1, len(system) // 4),
                    "completion_tokens": completion_tokens,
                },
            }
        )

    # -- reading the record -----------------------------------------------

    def of_kind(self, kind: str) -> list[StubRequest]:
        return [r for r in self.requests if r.kind == kind]

    def step_request(self, instruction_fragment: str) -> StubRequest:
        """The answerer request whose prompt carries this step instruction."""
        hits = [r for r in self.of_kind("answerer") if instruction_fragment in r.system]
        assert len(hits) == 1, (
            f"expected exactly one step request containing {instruction_fragment!r}, "
            f"found {len(hits)}"
        )
        return hits[0]


def stub_model(
    name: str,
    score: float,
    input_per_token: float,
    output_per_token: float,
    source: str = "openrouter",
    checked: bool = True,
) -> ModelInfo:
    """A matrix row with the same score on every task type."""
    prefix = "openrouter/" if source == "openrouter" else f"{source}/"
    endpoint = "https://openrouter.ai/api" if source == "openrouter" else "http://127.0.0.1:11434"
    return ModelInfo(
        model_id=f"{prefix}stub/{name}",
        source=source,
        endpoint=endpoint,
        context_length=200_000,
        pricing=(
            Pricing(input_per_token=input_per_token, output_per_token=output_per_token)
            if source == "openrouter"
            else None
        ),
        task_scores={t: score for t in TASK_TYPES},
        checked=checked,
    )


def lane_matrix(local_score: float = 7.0) -> CapabilityMatrix:
    """One local model (at 127.0.0.1) and three cloud models, all checked.

    The local model's default score (7.0) clears the light floor (6.0) but
    not the mid floor (7.5), so a derived mid step still goes to the cloud.
    """
    matrix = three_tier_matrix()
    matrix.upsert(stub_model("local", local_score, 0.0, 0.0, source="ollama"))
    return matrix


def three_tier_matrix() -> CapabilityMatrix:
    """One top, one mid and one cheap cloud model, all checked."""
    matrix = CapabilityMatrix()
    matrix.upsert(stub_model("top", 9.5, 5e-6, 2.5e-5))
    matrix.upsert(stub_model("mid", 8.0, 1e-6, 3e-6))
    matrix.upsert(stub_model("cheap", 6.5, 1e-7, 2e-7))
    return matrix


async def drain_log_writes(rounds: int = 5) -> None:
    """Let fire-and-forget log writes (``write_event``) reach the database."""
    for _ in range(rounds):
        pending = [t for t in asyncio.all_tasks() if t is not asyncio.current_task()]
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)
