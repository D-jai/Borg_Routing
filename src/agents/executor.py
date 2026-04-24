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
- BRT-TOOL-001: tool unavailable (caught via ToolInvocationResult, not raised)
- BRT-TOOL-002: tool runtime failure (BRT-WEB-01)
"""
import json
import os
import re
import time
from typing import Optional
from uuid import UUID

import httpx

from src.db.scratch import apply_cleanup_directives
from src.db.state import create_run, get_config, update_run_status
from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.envelopes.validator import validate_inbound
from src.logging.writer import write_event
from src.prompts.answerer import ANSWERER_SYSTEM_PROMPT
from src.prompts.base import build_system_prompt, load_governance
from src.registry.model_call import resolve_model_call
from src.registry.models import ModelInfo

_MODEL_CALL_TIMEOUT_SEC = 120


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


async def _call_model(
    model: ModelInfo, system_prompt: str, user_msg: str
) -> tuple[str, int, int]:
    """Call model via OpenAI-compat /v1/chat/completions.

    Uses ``resolve_model_call`` so OpenRouter rows get auth headers and the
    right body model id. Always sends ``max_tokens`` (configurable via
    ``ANSWERER_MAX_TOKENS``) to avoid OpenRouter 402 ``tokens=0`` rejections.

    Returns:
        (content_text, input_tokens, output_tokens)

    Raises:
        ValueError: BRT-ANS-006 on timeout.
        ValueError: BRT-ANS-002 on HTTP error.
    """
    url, headers, body_model = resolve_model_call(model)
    max_tokens = int(os.getenv("ANSWERER_MAX_TOKENS", "2000"))
    payload = {
        "model": body_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
        "max_tokens": max_tokens,
    }
    try:
        async with httpx.AsyncClient(timeout=_MODEL_CALL_TIMEOUT_SEC) as client:
            resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ValueError(f"BRT-ANS-006: model call timed out ({url})") from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"BRT-ANS-002: HTTP {exc.response.status_code} from {url}: {exc.response.text[:200]}"
        ) from exc
    except httpx.RequestError as exc:
        raise ValueError(f"BRT-ANS-002: request error calling {url}: {exc}") from exc

    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)
    return content, tokens_in, tokens_out


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

    # BRT-WEB-01 — run tool_requests BEFORE the LLM call. Results land in
    # dependency_input['tool_results'] so the prompt can read them.
    if task.tool_requests:
        from src.tools import run_tool_requests  # noqa: PLC0415

        try:
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
    stage_prompt = ANSWERER_SYSTEM_PROMPT
    for placeholder, value in substitutions.items():
        stage_prompt = stage_prompt.replace("{" + placeholder + "}", value)
    system_prompt = build_system_prompt(
        stage_prompt=stage_prompt,
        global_rules=governance["global_rules"],
        admin_rules_ledger=governance["admin_rules_ledger"],
        user_instruction=task.instruction,
    )

    # Build a ModelInfo for the call. Prefer the matrix row (captures source
    # + endpoint canonically); fall back to inferring source from the model_id
    # prefix and using the endpoint stamped on the task.
    model_for_call = _resolve_call_model(task)

    # Call model
    t0 = time.monotonic()
    try:
        content, tokens_in, tokens_out = await _call_model(
            model_for_call, system_prompt, "begin"
        )
    except ValueError:
        raise
    except Exception as exc:
        raise ValueError(f"BRT-ANS-008: unexpected error calling model: {exc}") from exc

    latency_ms = int((time.monotonic() - t0) * 1000)

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
        )
        await write_event(
            "model_call",
            run_id=task.run_id,
            envelope_id=task.envelope_id,
            model_id=task.model_name,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency_ms,
            payload_out=content[:2000],
            db_path=db_path,
        )
        return env

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
        )
    except Exception as exc:
        raise ValueError(
            f"BRT-ANS-003: could not build InboundEnvelope from model response: {exc}. Raw: {raw}"
        ) from exc

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

    # Compute cost
    dollars: Optional[float] = None
    # (Cost computed by pipeline runner once model pricing is resolved)

    # Log model_call event (fire-and-forget)
    await write_event(
        "model_call",
        run_id=task.run_id,
        envelope_id=task.envelope_id,
        model_id=task.model_name,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        dollars=dollars,
        latency_ms=latency_ms,
        payload_in=system_prompt[:2000],
        payload_out=content[:2000],
        db_path=db_path,
    )

    return env


async def execute(task: WrappedTask, db_path: str = "data/borg_route.db") -> InboundEnvelope:
    """Dispatch a WrappedTask to the appropriate handler.

    Raises:
        ValueError: BRT-ANS-* on answerer failures.
        NotImplementedError: for types not yet implemented in this slice.
    """
    if task.type == "answerer":
        return await _execute_answerer(task, db_path)
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
            return await _execute_answerer(demoted, db_path)

        # Spawn child run linked to parent — BRT-ORC-021
        try:
            child_run_id = await create_run(
                task.instruction,
                parent_run_id=task.run_id,
                db_path=db_path,
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
            )
            await update_run_status(child_run_id, "completed", db_path=db_path)
        except Exception as exc:
            try:
                await update_run_status(child_run_id, "failed", db_path=db_path)
            except Exception:
                pass
            raise ValueError(f"BRT-ORC-022: child orchestration failed: {exc}") from exc

        return InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status="resolved",
            output={"response": result_text, "child_run_id": str(child_run_id)},
        )
    else:
        raise ValueError(f"BRT-ANS-008: unknown task type '{task.type}'")
