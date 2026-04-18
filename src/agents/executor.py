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
"""
import json
import re
import time
from typing import Optional
from uuid import UUID

import httpx

from src.db.scratch import apply_cleanup_directives
from src.db.state import get_config
from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.envelopes.validator import validate_inbound
from src.logging.writer import write_event
from src.prompts.answerer import ANSWERER_SYSTEM_PROMPT
from src.prompts.base import build_system_prompt, load_governance

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


async def _call_model(endpoint: str, system_prompt: str, user_msg: str) -> tuple[str, int, int]:
    """Call model via OpenAI-compat /v1/chat/completions.

    Returns:
        (content_text, input_tokens, output_tokens)

    Raises:
        ValueError: BRT-ANS-006 on timeout.
        ValueError: BRT-ANS-002 on HTTP error.
    """
    payload = {
        "model": "default",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_msg},
        ],
        "temperature": 0.2,
    }
    try:
        async with httpx.AsyncClient(timeout=_MODEL_CALL_TIMEOUT_SEC) as client:
            resp = await client.post(f"{endpoint}/v1/chat/completions", json=payload)
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ValueError(f"BRT-ANS-006: model call timed out ({endpoint})") from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"BRT-ANS-002: HTTP {exc.response.status_code} from {endpoint}: {exc.response.text[:200]}"
        ) from exc
    except httpx.RequestError as exc:
        raise ValueError(f"BRT-ANS-002: request error calling {endpoint}: {exc}") from exc

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

    # Call model
    t0 = time.monotonic()
    try:
        content, tokens_in, tokens_out = await _call_model(
            task.model_endpoint, system_prompt, "begin"
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
        # Child orchestration — returns a response string wrapped in an envelope
        run_id = task.run_id
        result_text = await orchestrate_run(
            run_id=run_id,
            user_instruction=task.instruction,
            db_path=db_path,
        )
        return InboundEnvelope(
            envelope_id=task.envelope_id,
            subtask_id=task.subtask_id,
            status="resolved",
            output={"response": result_text},
        )
    else:
        raise ValueError(f"BRT-ANS-008: unknown task type '{task.type}'")
