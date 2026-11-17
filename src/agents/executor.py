"""Stateless agent executor — dispatches a WrappedTask to the right handler.

For type=answerer: calls the model via httpx, validates the inbound envelope,
  applies cleanup directives, logs the model_call event.
For other types: dispatched to their respective modules (imported lazily to
  avoid circular imports).

Error codes:
- BRT-ANS-001: model endpoint not configured for answerer task
- BRT-ANS-002: HTTP error calling model endpoint
- BRT-ANS-003: model response could not be parsed as InboundEnvelope JSON
- BRT-ANS-004: model response body contained no valid JSON object
- BRT-ANS-005: inbound envelope failed contract validation
- BRT-ANS-006: model call timed out
- BRT-ANS-007: governance load failed
- BRT-ANS-008: unexpected executor error
- BRT-ANS-009: model output truncated (finish_reason=length), after the
               doubled-cap retry or with no retry when the first cap is
               already at or above the 16000 ceiling — caller must raise
               ANSWERER_MAX_TOKENS / envelope.max_output_tokens or split
               the task.
- BRT-ANS-011: connection pool exhausted (too many calls at once; not a model timeout)
- BRT-ANS-010: the model declined or reported failure (shown to the planner
               in place of the code its own failure_reason text may start
               with; set where the planner's stage_results are built)
- BRT-TOOL-001: tool unavailable (caught via ToolInvocationResult, not raised)
- BRT-TOOL-002: tool runtime failure (BRT-WEB-01)
- BRT-LANE-003: a call that must stay local was headed off the machine (raised
               by send_outbound; the call guard)
- BRT-LANE-004: a raw step's output was withheld by the output guard
- BRT-LANE-005: tool_requests refused in lane_mode all_local

On-premise lane: a raw answerer (``task.data_scope == "raw"``) has its
document text loaded here, from ids, right before its own request is built;
its every failure leaves as a code plus document ids, and its output passes
the output guard (``src/lane/context.py::guard_envelope``).
"""
import json
import os
import re
import time
from typing import Optional
from uuid import UUID

import httpx

from src.db.scratch import apply_cleanup_directives
from src.db.state import create_run, get_config, lane_mode_of, sum_run_tree, update_run_status
from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.envelopes.validator import validate_inbound
from src.lane.context import (
    current_lane, failed_envelope, failure_of, guard_envelope, mark_model_reason, raw_failure_code,
    raw_step_error,
)
from src.lane.errors import LaneError
from src.lane.outbound import call_scope, send_outbound
from src.logging.writer import write_event
from src.prompts.answerer import ANSWERER_SYSTEM_PROMPT
from src.prompts.base import build_system_prompt, load_governance
from src.registry.cost import price_with_baseline
from src.registry.model_call import resolve_model_call
from src.registry.models import ModelInfo

_MODEL_CALL_TIMEOUT_SEC = 120   # default; BORG_ANSWERER_CALL_TIMEOUT overrides it per call
# A "{slot}" in the answerer stage prompt (JSON examples there never match).
_STAGE_SLOT = re.compile(r"\{(\w+)\}")

# BRT-ANS-009 — absolute ceiling for the doubled-cap retry. Bounds runaway
# token spend if a model is genuinely producing a much larger answer than
# the cap suggests. 16k matches the practical envelope of "complete spec +
# code + tests" while staying well under any provider's hard context
# window.
_TRUNCATION_RETRY_CEILING = 16000


def _extract_json(text: str) -> dict:
    """Extract the first JSON object from a string.

    Raises:
        ValueError: BRT-ANS-004 if no valid JSON object found.
    """
    # Try direct parse first
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass

    # Try to find JSON block inside ```json ... ``` fences
    fence_match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_match:
        try:
            return json.loads(fence_match.group(1))
        except json.JSONDecodeError:
            pass

    # Try to find any {...} block
    brace_match = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_match:
        try:
            return json.loads(brace_match.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError("BRT-ANS-004: no valid JSON object found in model response")


def _infer_source(model_id: str) -> str:
    """Infer provider source from a model_id prefix."""
    if model_id.startswith("openrouter/"):
        return "openrouter"
    if model_id.startswith("ollama/"):
        return "ollama"
    if model_id.startswith("custom_"):
        return "custom_key"
    return "lmstudio"


def _resolve_call_model(task: WrappedTask) -> ModelInfo:
    """Build a ModelInfo for resolve_model_call() from a WrappedTask.

    Prefers the live matrix row if present (it has canonical endpoint/source);
    otherwise synthesises one from the task's ``model_name`` + ``model_endpoint``.
    """
    model_id = task.model_name or ""
    # Late import keeps tests that mock the matrix happy.
    try:
        from src.orchestrator.engine import _get_matrix  # noqa: PLC0415
        matrix = _get_matrix()
        row = matrix.get(model_id)
        if row is not None:
            # If the task was dispatched with a different endpoint (e.g. pinned
            # config override) honour that but keep the row's source.
            if task.model_endpoint and task.model_endpoint != (row.endpoint or ""):
                return row.model_copy(update={"endpoint": task.model_endpoint})
            return row
    except Exception:
        pass
    return ModelInfo(
        model_id=model_id or "lmstudio/default",
        source=_infer_source(model_id),
        endpoint=task.model_endpoint,
        context_length=4096,
    )


async def _post_chat_completion(
    url: str,
    headers: dict,
    payload: dict,
    model: Optional[ModelInfo] = None,
    must_stay_local: bool = False,
    attempt: int = 1,
) -> tuple[str, int, int, str]:
    """Single POST to /v1/chat/completions. Returns
    (content, tokens_in, tokens_out, finish_reason).

    Goes through ``send_outbound`` (the lane's call guard; a plain POST when
    the lane is off).

    Raises:
        ValueError: BRT-ANS-006 on timeout, BRT-ANS-002 on HTTP/transport error.
        LaneError: BRT-LANE-003 when a call that must stay local is not local.
    """
    from src.orchestrator.engine import call_timeout_from_env  # noqa: PLC0415  (engine imports this module)

    try:
        resp = await send_outbound(
            url, payload, headers,
            timeout=call_timeout_from_env("BORG_ANSWERER_CALL_TIMEOUT", _MODEL_CALL_TIMEOUT_SEC),
            model=model,
            must_stay_local=must_stay_local,
            purpose="answerer",
            attempt=attempt,
        )
        resp.raise_for_status()
    except httpx.PoolTimeout as exc:
        raise ValueError(
            f"BRT-ANS-011: local connection pool exhausted ({url}); "
            "too many calls at once, not a model timeout"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ValueError(f"BRT-ANS-006: model call timed out ({url})") from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"BRT-ANS-002: HTTP {exc.response.status_code} from {url}: {exc.response.text[:200]}"
        ) from exc
    except httpx.RequestError as exc:
        raise ValueError(f"BRT-ANS-002: request error calling {url}: {exc}") from exc

    try:
        body = resp.json()
        choice = body["choices"][0]
        content = choice["message"]["content"]
        finish_reason = (choice.get("finish_reason") or "").strip()
        usage = body.get("usage", {})
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        # An HTTP 200 whose body is an error object or otherwise has no choices.
        raise ValueError(
            f"BRT-ANS-002: malformed response from {url} (no usable choices): {exc!r}"
        ) from exc
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    return content, tokens_in, tokens_out, finish_reason


def _answerer_caps(
    model: ModelInfo, requested_max_tokens: Optional[int] = None
) -> tuple[int, Optional[int]]:
    """Return (first_cap, retry_cap) for an answerer call.

    ``first_cap`` is the env cap (4x, at least 8000, for reasoning-family
    models) with the envelope's ``max_output_tokens`` as a floor.
    ``retry_cap`` is the doubled cap (ceiling ``_TRUNCATION_RETRY_CEILING``)
    used once on ``finish_reason == "length"``, or None when the first cap
    is already at or above the ceiling, so no retry can happen.
    """
    from src.orchestrator.engine import _resolve_max_tokens_for_model  # noqa: PLC0415

    base_max = _resolve_max_tokens_for_model(
        model, "ANSWERER_MAX_TOKENS", default=2000
    )
    effective_max = max(base_max, int(requested_max_tokens or 0))
    if effective_max >= _TRUNCATION_RETRY_CEILING:
        return effective_max, None
    return effective_max, min(effective_max * 2, _TRUNCATION_RETRY_CEILING)


async def _call_model(
    model: ModelInfo,
    system_prompt: str,
    user_msg: str,
    requested_max_tokens: Optional[int] = None,
    must_stay_local: bool = False,
) -> tuple[str, int, int, str]:
    """Call model via OpenAI-compat /v1/chat/completions with auto-retry
    on `finish_reason == "length"` (BRT-ANS-009).

    ``must_stay_local`` (a raw task, or any call in ``all_local``) makes
    every attempt, the retry included, pass the lane's call guard.

    The envelope-level ``requested_max_tokens`` (i.e. ``task.max_output_tokens``)
    acts as a FLOOR over the env-driven cap so plan nodes that legitimately
    need more headroom can declare it. Reasoning-family models additionally
    get the 4x bump from ``_resolve_max_tokens_for_model``.

    On the first attempt, if the provider reports the response was cut off
    by the cap (``finish_reason == "length"``), make ONE more call with the
    cap doubled (bounded by ``_TRUNCATION_RETRY_CEILING``). The caller is
    responsible for surfacing BRT-ANS-009 if the retry still truncates —
    we can't decide here whether downstream JSON parsed cleanly anyway.

    Returns:
        (content_text, tokens_in_total, tokens_out_total, final_finish_reason)

    Raises:
        ValueError: BRT-ANS-006 on timeout, BRT-ANS-002 on HTTP error.
    """
    url, headers, body_model = resolve_model_call(model)
    # BRT-ORC-RM — reasoning-family answerers (DeepSeek-R1, Qwen-QwQ,
    # o1/o3/o4, thinking-prefixed snapshots) burn thousands of hidden
    # chain-of-thought tokens before emitting visible output; a 2000-token
    # cap truncates them mid-thought. _resolve_max_tokens_for_model bumps
    # the cap to >= 8000 for detected reasoning models. The envelope's
    # requested cap acts as a floor (the planner may ask for more on a node).
    effective_max, retry_cap = _answerer_caps(model, requested_max_tokens)

    payload = {
        "model": body_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": effective_max,
    }

    lane_kwargs = (
        {"model": model, "must_stay_local": must_stay_local}
        if current_lane() is not None else {}
    )
    content, tokens_in, tokens_out, finish_reason = await _post_chat_completion(
        url, headers, payload, **lane_kwargs
    )

    # BRT-ANS-009 — silent-truncation cascade. When a
    # provider stops because it hit the cap, the visible text is almost
    # always a partial JSON object and BRT-ANS-004 then fires on the
    # unparseable tail. Retry ONCE with the cap doubled so the natural
    # fix happens automatically instead of failing the run.
    if finish_reason == "length" and retry_cap is not None:
        retry_payload = {**payload, "max_tokens": retry_cap}
        try:
            (
                content_retry,
                retry_in,
                retry_out,
                finish_reason,
            ) = await _post_chat_completion(
                url, headers, retry_payload,
                **({**lane_kwargs, "attempt": 2} if lane_kwargs else {}),
            )
        except ValueError as exc:
            # The first attempt was billed even though the retry failed.
            # Hand its tokens to the caller so the paid call is still logged.
            exc.paid_tokens = (tokens_in, tokens_out)  # type: ignore[attr-defined]
            raise
        content = content_retry
        tokens_in += retry_in
        tokens_out += retry_out

    return content, tokens_in, tokens_out, finish_reason


async def _execute_answerer(task: WrappedTask, db_path: str) -> InboundEnvelope:
    """Dispatch an answerer-type task to the model.

    Raises:
        ValueError: BRT-ANS-001..008 on various failure modes.
    """
    if not task.model_endpoint:
        raise ValueError(
            f"BRT-ANS-001: model_endpoint not configured for envelope {task.envelope_id}"
        )

    # Load governance
    try:
        governance = await load_governance(lambda key: get_config(key, db_path=db_path))
    except Exception as exc:
        raise ValueError(f"BRT-ANS-007: governance load failed: {exc}") from exc

    lane = current_lane()
    # On-premise lane: in all_local nothing may leave the machine, so a
    # step's tools are refused before any of them runs (design note 6.4).
    if lane is not None and lane.all_local and task.tool_requests:
        await write_event(
            "error",
            run_id=task.run_id,
            envelope_id=task.envelope_id,
            decision={
                "code": "BRT-LANE-005",
                "msg": "tool_requests are refused in lane_mode all_local; nothing was sent",
                "tools": [r.tool for r in task.tool_requests],
            },
            db_path=db_path,
        )
        return failed_envelope(task, "BRT-LANE-005")

    # BRT-WEB-01 — run tool_requests BEFORE the LLM call. Results land in
    # dependency_input['tool_results'] so the prompt can read them.
    if task.tool_requests:
        from src.tools import run_tool_requests  # noqa: PLC0415

        try:
            with call_scope(task.run_id, task.envelope_id):
                tool_outcomes = await run_tool_requests(task.tool_requests)
        except Exception as exc:
            return InboundEnvelope(
                envelope_id=task.envelope_id,
                subtask_id=task.subtask_id,
                status="failed",
                output={},
                failure_reason=f"BRT-TOOL-002: tool dispatcher crashed: {exc}",
            )

        public = {
            name: [r.to_public_dict() for r in results]
            for name, results in tool_outcomes.items()
        }
        # Inline tool_results into the context string so the answerer prompt
        # (which does NOT read dependency_input directly) can see them. The
        # key name "tool_results" is stamped verbatim so downstream prompts
        # (and tests) can grep for it.
        inlined_block = (
            "\n\ntool_results (pre-fetched by the executor before this call):\n"
            + json.dumps(public, indent=2, default=str)
        )
        task = task.model_copy(
            update={
                "context": (task.context or "") + inlined_block,
                "dependency_input": {
                    **task.dependency_input,
                    "tool_results": public,
                },
            }
        )

        # Log one model_call event per tool invocation (model_id="tool:<name>"
        # so the run trace can filter tool calls from real model calls).
        for name, results in tool_outcomes.items():
            for r in results:
                await write_event(
                    "model_call",
                    run_id=task.run_id,
                    envelope_id=task.envelope_id,
                    model_id=f"tool:{name}",
                    # No tool carries a price today; a priced tool would set
                    # its dollars here. Unpriced means 0.0, never None.
                    dollars=0.0,
                    latency_ms=r.latency_ms,
                    payload_in=json.dumps({"tool": name, "args": r.args})[:2000],
                    payload_out=(r.error or "")[:2000] if r.status == "failed"
                                else json.dumps(r.result)[:2000],
                    db_path=db_path,
                )

        # If every tool request failed, surface a BRT-TOOL-002 envelope
        # instead of calling the model against an empty tool_results.
        all_results = [r for rs in tool_outcomes.values() for r in rs]
        if all_results and all(r.status == "failed" for r in all_results):
            first_err = next(
                (r.error for r in all_results if r.error), "BRT-TOOL-002: tools failed"
            )
            return InboundEnvelope(
                envelope_id=task.envelope_id,
                subtask_id=task.subtask_id,
                status="failed",
                output={},
                failure_reason=first_err,
            )

    # Build system prompt — use safe replacement (prompt contains JSON {} examples)
    state_query_result = task.dependency_input.get("state_query_result", "(none)")
    substitutions = {
        "run_id": str(task.run_id),
        "envelope_id": str(task.envelope_id),
        "subtask_id": str(task.subtask_id),
        "level_path": task.level_path,
        "instruction": task.instruction,
        "context": task.context or "(none)",
        "state_query_result": str(state_query_result),
        "output_schema": json.dumps(task.output_schema, indent=2),
        "side_effects_list": ", ".join(task.side_effects) if task.side_effects else "(none)",
    }
    # One pass over the template: a "{output_schema}" inside an item, the
    # instruction or the context is data, never a slot filled by a later step.
    stage_prompt = _STAGE_SLOT.sub(
        lambda m: substitutions.get(m.group(1), m.group(0)), ANSWERER_SYSTEM_PROMPT
    )
    system_prompt = build_system_prompt(
        stage_prompt=stage_prompt,
        global_rules=governance["global_rules"],
        admin_rules_ledger=governance["admin_rules_ledger"],
        # Law 1 — governance shows the operator's verbatim instruction. The
        # step's own text is the task, carried by {instruction} above.
        user_instruction=task.governance_instruction or task.instruction,
    )

    # Build a ModelInfo for the call. Prefer the matrix row (captures source
    # + endpoint canonically); fall back to inferring source from the model_id
    # prefix and using the endpoint stamped on the task.
    model_for_call = _resolve_call_model(task)

    # Call model. _call_model honours task.max_output_tokens as a floor
    # over ANSWERER_MAX_TOKENS and auto-retries once at doubled cap on
    # finish_reason=="length" (BRT-ANS-009).
    t0 = time.monotonic()
    call_kwargs = {}
    if lane is not None:
        # The call guard: a raw task, or any call in all_local, must stay
        # local. In by_reference a raw task may go to any checked model.
        call_kwargs["must_stay_local"] = lane.all_local or (
            task.data_scope == "raw" and lane.keeps_raw_local
        )
    try:
        with call_scope(task.run_id, task.envelope_id):
            content, tokens_in, tokens_out, finish_reason = await _call_model(
                model_for_call, system_prompt, "begin", task.max_output_tokens,
                **call_kwargs,
            )
    except ValueError as exc:
        paid = getattr(exc, "paid_tokens", None)
        if paid:
            # Paid-then-failed: the first attempt was billed. Log it (existing
            # event type) before re-raising so the digest counts it.
            paid_dollars, paid_baseline = await price_with_baseline(
                model_for_call, paid[0], paid[1], db_path
            )
            await write_event(
                "model_call",
                run_id=task.run_id,
                envelope_id=task.envelope_id,
                model_id=task.model_name,
                tokens_input=paid[0],
                tokens_output=paid[1],
                dollars=paid_dollars,
                baseline_dollars=paid_baseline,
                latency_ms=int((time.monotonic() - t0) * 1000),
                decision={
                    "code": "BRT-ANS-009",
                    "msg": "first attempt billed (finish_reason=length); retry failed",
                    "retry_failed": True,
                    "error": str(exc)[:300],
                },
                db_path=db_path,
            )
        raise
    except Exception as exc:
        raise ValueError(f"BRT-ANS-008: unexpected error calling model: {exc}") from exc

    latency_ms = int((time.monotonic() - t0) * 1000)

    # Work order 1.6 — dollars from the serving model's prices (0.0 when it
    # has none) and the same tokens at the baseline model's prices. Tokens
    # already include both attempts of a truncation retry.
    dollars, baseline_dollars = await price_with_baseline(
        model_for_call, tokens_in, tokens_out, db_path
    )

    # BRT-ANS-009 — even with the doubled-cap retry, the response still
    # got cut off by the cap. Surface a distinct failure code so operators
    # see "the cap is too small for this task" instead of the misleading
    # downstream BRT-ANS-004 (unparseable JSON, which is the symptom not
    # the cause).
    if finish_reason == "length":
        first_cap, retry_cap = _answerer_caps(model_for_call, task.max_output_tokens)
        if retry_cap is not None:
            cut_detail = (
                f"even after one retry at double the cap ({first_cap} -> {retry_cap})"
            )
            msg = "max_tokens cap exhausted after retry"
        else:
            cut_detail = (
                f"at a cap of {first_cap}; no retry, the cap is already at or "
                f"above the {_TRUNCATION_RETRY_CEILING} retry ceiling"
            )
            msg = "max_tokens cap exhausted; no retry (cap at or above ceiling)"
        env = InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status="failed",
            output={},
            failure_reason=(
                f"BRT-ANS-009: model output truncated (finish_reason=length) "
                f"{cut_detail}. Raise ANSWERER_MAX_TOKENS or split the task "
                "into smaller nodes."
            ),
            tokens_used={"input": tokens_in, "output": tokens_out},
            dollars=dollars,
            baseline_dollars=baseline_dollars,
        )
        await write_event(
            "model_call",
            run_id=task.run_id,
            envelope_id=task.envelope_id,
            model_id=task.model_name,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            dollars=dollars,
            baseline_dollars=baseline_dollars,
            latency_ms=latency_ms,
            payload_out=content[:2000],
            decision={
                "code": "BRT-ANS-009",
                "msg": msg,
                "retried": retry_cap is not None,
                "first_max_tokens": first_cap,
                "retry_max_tokens": retry_cap,
                "envelope_max_output_tokens": task.max_output_tokens,
            },
            db_path=db_path,
        )
        return env

    # Parse response
    try:
        raw = _extract_json(content)
    except ValueError:
        # Return a failed envelope rather than crashing
        env = InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status="failed",
            output={},
            failure_reason=f"BRT-ANS-004: could not parse model response as JSON. Raw: {content[:200]}",
            tokens_used={"input": tokens_in, "output": tokens_out},
            dollars=dollars,
            baseline_dollars=baseline_dollars,
        )
        await write_event(
            "model_call",
            run_id=task.run_id,
            envelope_id=task.envelope_id,
            model_id=task.model_name,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            dollars=dollars,
            baseline_dollars=baseline_dollars,
            latency_ms=latency_ms,
            payload_out=content[:2000],
            db_path=db_path,
        )
        return env

    # BRT-WEB-02 — grab any tool_results the executor injected earlier so
    # we can carry them back on the envelope. Compose mode will re-cite
    # from these raw citations even if the answerer's summary dropped them.
    carried_tool_results = dict(task.dependency_input.get("tool_results") or {})

    # Build InboundEnvelope
    try:
        env = InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status=raw.get("status", "failed"),
            confidence=raw.get("confidence"),
            output=raw.get("output", {}),
            failure_reason=raw.get("failure_reason"),
            tokens_used={"input": tokens_in, "output": tokens_out},
            scratch_writes=raw.get("scratch_writes", []),
            scratch_cleanup=raw.get("scratch_cleanup", []),
            tool_results=carried_tool_results,
        )
    except Exception as exc:
        raise ValueError(
            f"BRT-ANS-003: could not build InboundEnvelope from model response: {exc}. Raw: {raw}"
        ) from exc
    # Its failure_reason (if any) is the model's own text, not a runtime code.
    mark_model_reason(env)

    # Validate contract
    try:
        validate_inbound(env, task)
    except ValueError as exc:
        env = InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status="failed",
            output={},
            failure_reason=f"BRT-ANS-005: {exc}",
            tokens_used={"input": tokens_in, "output": tokens_out},
        )

    # Apply cleanup directives (Law 2 — child self-cleans)
    if env.scratch_cleanup:
        await apply_cleanup_directives(task.run_id, env.scratch_cleanup, db_path=db_path)

    env.dollars = dollars
    env.baseline_dollars = baseline_dollars

    # Log model_call event (fire-and-forget)
    await write_event(
        "model_call",
        run_id=task.run_id,
        envelope_id=task.envelope_id,
        model_id=task.model_name,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        dollars=dollars,
        baseline_dollars=baseline_dollars,
        latency_ms=latency_ms,
        payload_in=system_prompt[:2000],
        payload_out=content[:2000],
        db_path=db_path,
    )

    return env


async def _execute_raw_answerer(task: WrappedTask, db_path: str, lane) -> InboundEnvelope:
    """A raw answerer: load its documents, call (locally), guard the output.

    The document text is added to this task's own context here, after the
    envelope was saved with ids only. A failure leaves as a code plus the
    document ids; the full error stays in the local trace.

    Raises:
        LaneError: BRT-LANE-003 from the call guard (the message holds no
            document text), or the failure's own code with ids only.
        RawStepError: ``by_reference``: the failure's own code, the
            document ids and the first 200 characters of its message.
    """
    block = lane.text_block(list(task.documents))
    loaded = (
        task.model_copy(update={"context": f"{task.context}\n\n{block}" if task.context else block})
        if block else task
    )
    try:
        env = await _execute_answerer(loaded, db_path)
    except Exception as exc:
        if not lane.keeps_raw_local:
            # by_reference: no privacy promise. A failure raised while the
            # step ran (a LaneError such as BRT-LANE-007, or a runtime error)
            # goes up as its own code + the document ids + the first 200
            # characters, never as BRT-ORC-004 / BRT-APE-002.
            raise raw_step_error(exc, list(task.documents), "BRT-ANS-008") from exc
        if isinstance(exc, LaneError):
            raise
        code = raw_failure_code(exc, "BRT-ANS-008")
        failure = failure_of(code, list(task.documents))
        try:
            await write_event(
                "error",
                run_id=task.run_id,
                envelope_id=task.envelope_id,
                decision={
                    "code": code,
                    "msg": "a raw step failed; the full error stays in this local trace",
                    "error": str(exc)[:2000],
                    "doc": failure["doc"],
                },
                db_path=db_path,
            )
        except Exception:
            pass
        raise LaneError(code, "a raw step failed; see the local trace", doc=failure["doc"]) from None
    return guard_envelope(env, task, lane)


async def _run_answerer(task: WrappedTask, db_path: str) -> InboundEnvelope:
    lane = current_lane()
    if lane is not None and task.data_scope == "raw":
        return await _execute_raw_answerer(task, db_path, lane)
    return await _execute_answerer(task, db_path)


async def execute(task: WrappedTask, db_path: str = "data/borg_route.db") -> InboundEnvelope:
    """Dispatch a WrappedTask to the appropriate handler.

    Raises:
        ValueError: BRT-ANS-* on answerer failures.
        NotImplementedError: for types not yet implemented in this slice.
    """
    if task.type == "answerer":
        return await _run_answerer(task, db_path)
    elif task.type == "state_op":
        from src.orchestrator.state_ops import dispatch_state_op
        return await dispatch_state_op(task, db_path=db_path)
    elif task.type == "apply_to_each":
        from src.orchestrator.apply_to_each import expand_and_run
        return await expand_and_run(task, db_path=db_path)
    elif task.type == "orchestrate":
        from src.orchestrator.engine import orchestrate_run

        # Depth enforcement — BRT-ORC-020: demote at cap
        if task.current_depth >= task.max_depth:
            await write_event(
                "error",
                run_id=task.run_id,
                envelope_id=task.envelope_id,
                decision={
                    "error": (
                        f"BRT-ORC-020: max recursion depth {task.max_depth} reached "
                        f"at level {task.level_path} — demoting orchestrate node to answerer"
                    ),
                },
                db_path=db_path,
            )
            demoted = task.model_copy(update={"type": "answerer"})
            return await _run_answerer(demoted, db_path)

        # Spawn child run linked to parent — BRT-ORC-021
        try:
            child_run_id = await create_run(
                task.instruction,
                parent_run_id=task.run_id,
                db_path=db_path,
                lane_mode=lane_mode_of(current_lane()),
            )
        except Exception as exc:
            raise ValueError(f"BRT-ORC-021: child run creation failed: {exc}") from exc

        try:
            await update_run_status(child_run_id, "running", db_path=db_path)
            result_text = await orchestrate_run(
                run_id=child_run_id,
                user_instruction=task.instruction,
                db_path=db_path,
                current_depth=task.current_depth + 1,
                # Law 1 — the child plans its own step, but is governed by
                # what the operator asked at the root.
                root_instruction=task.governance_instruction or task.instruction,
            )
            await update_run_status(child_run_id, "completed", db_path=db_path)
        except Exception as exc:
            try:
                await update_run_status(child_run_id, "failed", db_path=db_path)
            except Exception:
                pass
            raise ValueError(f"BRT-ORC-022: child orchestration failed: {exc}") from exc

        # The child run's calls are logged under its own run id. Give this
        # node the child tree's total so the parent envelope shows its cost.
        from src.logging.writer import flush_pending  # noqa: PLC0415

        await flush_pending()
        child_total = await sum_run_tree(child_run_id, db_path=db_path)
        return InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status="resolved",
            output={"response": result_text, "child_run_id": str(child_run_id)},
            tokens_used={
                "input": child_total["tokens_input"],
                "output": child_total["tokens_output"],
            },
            dollars=child_total["dollars"],
            baseline_dollars=child_total["baseline_dollars"],
        )
    else:
        raise ValueError(f"BRT-ANS-008: unknown task type '{task.type}'")
