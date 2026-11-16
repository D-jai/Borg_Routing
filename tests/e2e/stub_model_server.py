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

TASK_TYPES = ("reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction")


@dataclass
class StubRequest:
    url: str
    model: str          # the model id sent in the request body
    kind: str           # plan | re_plan | compose | answerer
    system: str
    user: str
    max_tokens: Optional[int]
    body: dict = field(default_factory=dict)   # the exact JSON body received

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
    requests: list[StubRequest] = field(default_factory=list)

    def install(self, monkeypatch) -> "StubModels":
        stub = self

        async def fake_post(client, url, json=None, headers=None, **kwargs):  # noqa: A002
            return stub._answer(str(url), json or {})

        monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
        return self

    # -- request handling -------------------------------------------------

    def _answer(self, url: str, payload: dict) -> _StubResponse:
        messages = payload.get("messages") or []
        system = next((m["content"] for m in messages if m.get("role") == "system"), "")
        user = next((m["content"] for m in messages if m.get("role") == "user"), "")
        if "tavily" in url:
            kind = "tool"
        elif "response_format" in payload:
            m = _MODE_RE.search(system)
            kind = m.group(1) if m else "plan"
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
        )
        self.requests.append(req)

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
        elif kind == "compose":
            content = {"response": self.compose_text, "rationale": "scripted compose"}
        else:
            output = (
                self.answer_for(req) if self.answer_for else {"result": f"done by {req.model}"}
            )
            content = {"status": "resolved", "confidence": 0.9, "output": output}

        finish = self.finish_for(req) if self.finish_for else "stop"
        return _StubResponse(
            {
                "choices": [
                    {"message": {"content": json.dumps(content)}, "finish_reason": finish}
                ],
                "usage": {
                    "prompt_tokens": max(1, len(system) // 4),
                    "completion_tokens": self.completion_tokens,
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
