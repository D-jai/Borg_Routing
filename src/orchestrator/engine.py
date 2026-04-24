"""Main orchestration loop — the brain of BORG_ROUTE.

orchestrate_run() is the entry point. It drives the plan → dispatch →
re_plan → compose cycle using the orchestrator model.

Error codes:
- BRT-ORC-001: orchestrator model call failed
- BRT-ORC-002: orchestrator returned unparseable JSON
- BRT-ORC-003: plan is empty or malformed
- BRT-ORC-004: node dispatch failed (individual; the run continues)
- BRT-ORC-005: maximum loop count exceeded (safety ceiling)
- BRT-ORC-006: orchestrator returned action=fail
- BRT-ORC-007: no orchestrator model available
- BRT-ORC-007-DEGRADED: quality bar relaxed below the default because no model qualified
- BRT-ORC-008: node builder failed for a plan node
- BRT-ORC-009: compose mode returned no response
- BRT-ORC-015: default answerer selection failed; fell back to orchestrator
               model (warning; run continues)
- BRT-ORC-016: envelope persistence (save/update_inbound) failed (error;
               run continues, trace shows the DB hiccup)
- BRT-ORC-017: depends_on id not found in results (warning; node dispatches
               with whatever upstream outputs could be resolved)

Optimisation hooks (no error code):
- BRT-ZERO-01: for unambiguously conversational short prompts, skip
               plan/re_plan and go straight to compose (saves ~2-3
               orchestrator calls). Disable with BORG_RULE_ZERO_SHORTCUT=false
               or the rule_zero_shortcut config key.
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional
from uuid import UUID, uuid4

import httpx

logger = logging.getLogger(__name__)

from src.agents.executor import execute
from src.db.scratch import count_scratch_for_level
from src.db.state import (
    create_run,
    get_config,
    save_envelope,
    update_envelope_inbound,
    update_run_status,
)
from src.envelopes.schema import InboundEnvelope, ToolRequest, WrappedTask
from src.logging.writer import write_event
from src.orchestrator.fold_in import fold_in
from src.prompts.base import build_system_prompt, load_governance
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.model_call import resolve_model_call
from src.registry.models import ModelInfo
from src.registry.router import select_model
from src.skills.library import SkillLibrary

MAX_LOOPS = 10
MAX_DEPTH = 5
_MODEL_CALL_TIMEOUT = 180
_MATRIX_PATH = "data/capability_matrix.yaml"
_SKILLS_PATH = "data/skills.yaml"

_shared_matrix: Optional[CapabilityMatrix] = None
_shared_skill_lib: Optional[SkillLibrary] = None


def _get_matrix() -> CapabilityMatrix:
    global _shared_matrix
    if _shared_matrix is None:
        m = CapabilityMatrix()
        try:
            m.load(_MATRIX_PATH)
        except Exception:
            pass
        _shared_matrix = m
    return _shared_matrix


def _get_skill_lib() -> SkillLibrary:
    global _shared_skill_lib
    if _shared_skill_lib is None:
        sl = SkillLibrary()
        try:
            sl.load(_SKILLS_PATH)
        except Exception:
            pass
        _shared_skill_lib = sl
    return _shared_skill_lib


_LIST_MARKER_RE = re.compile(r"(?m)^\s*(?:[-*\u2022]|\d+[.)])\s+")

_BULK_PHRASES = (
    "for each",
    "list of",
    "classify these",
    "analyse these",
    "analyze these",
    "summarise each",
    "summarize each",
    "process each",
    "compare these",
    "compare all",
    "for every",
)

# BRT-ZERO-01b — live-data tokens. Prompts containing any of these cannot
# be answered from the model's pretraining alone; they need web_search or
# another runtime tool. Matching any of them forces the full plan path so
# the orchestrator can emit a tool_requests node. Matched as whole-ish
# substrings (case-insensitive); kept conservative to avoid false
# negatives on short conversational turns like "today is my birthday".
_LIVE_DATA_TOKENS = (
    "latest",
    "current",
    "today's",
    "this week",
    "this month",
    "this year",
    "news",
    "price of",
    "stock",
    "weather",
    "score",
    "who won",
    "breaking",
    "right now",
    "as of",
)


def _is_trivial_conversational(text: str) -> bool:
    """BRT-ZERO-01 — cheap rule-based pre-classifier.

    Returns True for short, non-list, non-bulk, no-live-data conversational
    prompts that should shortcut plan/re_plan and go straight to compose.
    Deterministic, no model call. Conservative by design — if any heuristic
    is uncertain, returns False and we take the normal full path.
    """
    if not text:
        return False
    t = text.strip()
    if len(t) >= 500:
        return False
    if _LIST_MARKER_RE.search(t):
        return False
    lower = t.lower()
    for phrase in _BULK_PHRASES:
        if phrase in lower:
            return False
    for token in _LIVE_DATA_TOKENS:
        if token in lower:
            return False
    return True


_REASONING_TAG_RE = re.compile(
    r"<(?:thinking|think|reasoning|reason|analysis|scratchpad)\b[^>]*>.*?"
    r"</(?:thinking|think|reasoning|reason|analysis|scratchpad)>",
    re.DOTALL | re.IGNORECASE,
)

# Model-id patterns that identify reasoning/thinking-family models. These
# models burn 3-8k tokens on private chain-of-thought *before* any visible
# output appears; running them with `max_tokens=2000` will hit the cap
# mid-thought and return an empty / truncated response. When we detect
# one, we reserve a larger budget (see _resolve_max_tokens_for_model).
_REASONING_MODEL_PATTERNS = (
    # OpenAI reasoning family (o1, o3, o4, and thinking-prefixed snapshots)
    re.compile(r"(^|/)(?:openai/)?o[134](?:-|$)", re.IGNORECASE),
    re.compile(r"thinking", re.IGNORECASE),
    # DeepSeek-R1 reasoning family
    re.compile(r"deepseek[-_/]?r1", re.IGNORECASE),
    # Qwen QwQ reasoning family
    re.compile(r"qwen[-_/]?qwq", re.IGNORECASE),
    re.compile(r"qwq[-_]?\d", re.IGNORECASE),
    # Gemini / Claude reasoning snapshots (non-exhaustive — caller can
    # also set specialty_tags=["reasoning"] to force the larger budget).
    re.compile(r"gemini.*(?:thinking|reasoning)", re.IGNORECASE),
    re.compile(r"claude.*(?:thinking|reasoning)", re.IGNORECASE),
    # Grok reasoning
    re.compile(r"grok[-_]?(?:\d[-_])?reasoning", re.IGNORECASE),
    # Kimi reasoning
    re.compile(r"kimi.*k[12].*thinking", re.IGNORECASE),
)


def _is_reasoning_model(model: ModelInfo) -> bool:
    """Return True if the model is in the reasoning/thinking family.

    Detection is conservative: any of the pattern matches on model_id OR an
    explicit `reasoning` tag in specialty_tags flips this on. A False
    negative just means the model runs with the standard token budget (the
    old behaviour); a False positive costs a few thousand extra output
    tokens on a call that didn't need them — cheaper than a run-breaking
    truncation.
    """
    if "reasoning" in {t.lower() for t in (model.specialty_tags or [])}:
        return True
    mid = model.model_id or ""
    return any(pat.search(mid) for pat in _REASONING_MODEL_PATTERNS)


def _resolve_max_tokens_for_model(
    model: ModelInfo,
    base_env_var: str,
    default: int = 2000,
) -> int:
    """Compute the per-call `max_tokens` for the given model.

    Non-reasoning models get the env-configured default (typically 2000).
    Reasoning models get max(default * 4, 8000) so their private
    chain-of-thought has headroom before the visible answer is emitted.
    Operators who want even more headroom can raise `base_env_var`.
    """
    base = int(os.getenv(base_env_var, str(default)))
    if _is_reasoning_model(model):
        return max(base * 4, 8000)
    return base


def _strip_reasoning_wrappers(text: str) -> str:
    """Remove <thinking>...</thinking> / <think>...</think> / similar blocks.

    Reasoning-family models (DeepSeek-R1, Qwen-QwQ, Gemini-thinking, some
    OpenAI reasoning snapshots) sometimes leak their private chain-of-thought
    as XML-ish wrappers even when `response_format=json_object` is set.
    Strip them before JSON extraction.
    """
    return _REASONING_TAG_RE.sub("", text).strip()


def _extract_json(text: str) -> dict:
    """Extract first JSON object from ``text`` — tolerant of reasoning-model prose.

    Order of attempts:
      1. Raw `json.loads` on the stripped text (happy path).
      2. Strip `<thinking>` / `<think>` / `<reasoning>` wrappers, retry.
      3. Fenced `json` / generic ``` code block.
      4. Greedy `{...}` match on the cleaned text (last resort).

    Raises:
        ValueError: BRT-ORC-002 if none of the strategies produce valid JSON.
    """
    raw = text.strip() if text else ""
    if not raw:
        raise ValueError("BRT-ORC-002: empty orchestrator response")

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass

    cleaned = _strip_reasoning_wrappers(raw)
    if cleaned and cleaned != raw:
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            pass

    fence_m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", cleaned or raw, re.DOTALL)
    if fence_m:
        try:
            return json.loads(fence_m.group(1))
        except json.JSONDecodeError:
            pass

    brace_m = re.search(r"\{.*\}", cleaned or raw, re.DOTALL)
    if brace_m:
        try:
            return json.loads(brace_m.group(0))
        except json.JSONDecodeError:
            pass

    raise ValueError("BRT-ORC-002: no valid JSON in orchestrator response")


def _safe_sub(template: str, subs: dict) -> str:
    """Safe placeholder substitution — avoids KeyError from JSON {} in prompt."""
    result = template
    for key, value in subs.items():
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value)
        else:
            value_str = str(value)
        result = result.replace("{" + key + "}", value_str)
    return result


def _pinned_model(endpoint: str, name: str) -> ModelInfo:
    """Wrap a pinned-config endpoint/name in a synthetic ModelInfo so the
    resolve_model_call helper can handle it uniformly with matrix rows."""
    return ModelInfo(
        model_id=f"lmstudio/{name}" if "/" not in name else name,
        source="lmstudio",
        endpoint=endpoint,
        context_length=4096,
    )


async def _select_orchestrator_model(
    task_type: str,
    db_path: str,
    quality_requirement: float = 9.0,
    time_cost_per_sec: Optional[float] = None,
    run_id: Optional[UUID] = None,
) -> ModelInfo:
    """Select the orchestrator ModelInfo row.

    Resolution order:
      1. Pinned config (`orchestrator_endpoint` set in DB)
      2. Capability matrix at the requested quality_requirement (default 9.0)
      3. Degraded fallback at 5.1 — emits BRT-ORC-NO-FRONTIER trace event
         (when `run_id` is provided) so the dashboard can flag the run.
      4. Raises BRT-ORC-007.

    Returns: ModelInfo

    Raises:
        ValueError: BRT-ORC-007 if no model available.
    """
    # Check config for a pinned orchestrator endpoint
    pinned = await get_config("orchestrator_endpoint", db_path=db_path)
    if pinned:
        name = await get_config("orchestrator_model_name", db_path=db_path) or "orchestrator"
        return _pinned_model(pinned, name)

    # Try to select from capability matrix
    matrix = _get_matrix()
    select_kwargs: dict = {}
    if time_cost_per_sec is not None:
        select_kwargs["time_cost_per_sec"] = time_cost_per_sec
    try:
        return select_model(
            matrix=matrix,
            task_type=task_type,
            quality_requirement=quality_requirement,
            **select_kwargs,
        )
    except Exception:
        pass

    # Degraded fallback: if no checked model meets the quality bar, retry at
    # 5.1 — just above the default 5.0 unresearched score, so rows that have
    # genuinely been scored even weakly are preferred over flat defaults.
    try:
        model = select_model(
            matrix=matrix,
            task_type=task_type,
            quality_requirement=5.1,
            **select_kwargs,
        )
        logger.warning(
            "BRT-ORC-NO-FRONTIER: no model met quality_requirement=%s for "
            "task_type='%s'; falling back to '%s' at its current score. "
            "Run Get Model Info to refresh scores or check on a frontier "
            "model in the Models page.",
            quality_requirement, task_type, model.model_id,
        )
        # Emit a trace event so the dashboard / run timeline can flag this
        # run as 'ran without a qualifying brain'. Uses event_type='error'
        # (the only existing severity bucket that fits — see VALID_EVENT_TYPES
        # in src/logging/writer.py); the BRT-ORC-NO-FRONTIER code in the
        # decision payload distinguishes it from hard-fail errors. Fire-
        # and-forget; a logging hiccup must never shadow the real selection.
        if run_id is not None:
            try:
                await write_event(
                    "error",
                    run_id=run_id,
                    model_id=model.model_id,
                    decision={
                        "code": "BRT-ORC-NO-FRONTIER",
                        "severity": "warning",
                        "msg": (
                            f"No checked model met quality_requirement="
                            f"{quality_requirement} for task_type='{task_type}'. "
                            f"Degraded to '{model.model_id}' at quality_requirement=5.1. "
                            f"Re-score via Get Model Info or check on a frontier "
                            f"model on the Models page."
                        ),
                        "requested_quality": quality_requirement,
                        "fallback_quality": 5.1,
                        "task_type": task_type,
                    },
                    db_path=db_path,
                )
            except Exception:
                pass
        return model
    except Exception:
        pass

    raise ValueError(
        "BRT-ORC-007: no orchestrator model available. "
        "Check that at least one model is checked on the Models page, "
        "or set orchestrator_endpoint in config."
    )


async def _invoke_orchestrator_model(
    mode: str,
    run_id: UUID,
    user_instruction: str,
    current_depth: int,
    max_depth: int,
    thread_window: list[dict],
    rolling_summary: str,
    stage_results: dict,
    child_scratch_digest: dict,
    skills_catalogue: str,
    governance: dict,
    available_models_brief: str,
    orchestrator_model: ModelInfo,
    db_path: str,
) -> dict:
    """One call to the orchestrator model. Returns parsed JSON dict.

    Raises:
        ValueError: BRT-ORC-001 on HTTP error.
        ValueError: BRT-ORC-002 on JSON parse failure.
    """
    stage_results_str = json.dumps(
        {k: v.output for k, v in stage_results.items()}, indent=2
    ) if stage_results else "(none)"

    # BRT-WEB-02 — surface raw tool_results (web_search citations, etc.)
    # to compose/re_plan modes so the composer can re-cite from primary
    # sources even if an answerer summary dropped or mangled them. Only
    # include in compose/re_plan; plan mode hasn't seen any tools yet.
    if mode in ("compose", "re_plan") and stage_results:
        aggregated_tools: dict = {}
        for node_id, env in stage_results.items():
            env_tools = getattr(env, "tool_results", None) or {}
            for tool_name, results in env_tools.items():
                aggregated_tools.setdefault(tool_name, []).append({
                    "source_node": node_id,
                    "results": results,
                })
        tool_results_str = (
            json.dumps(aggregated_tools, indent=2, default=str)
            if aggregated_tools else "(none)"
        )
    else:
        tool_results_str = "(none)"

    subs = {
        "current_mode": mode,
        "run_id": str(run_id),
        "current_depth": str(current_depth),
        "max_depth": str(max_depth),
        "available_models_brief": available_models_brief,
        "thread_window": json.dumps(thread_window, indent=2),
        "rolling_summary": rolling_summary or "(none)",
        "stage_results": stage_results_str,
        "tool_results": tool_results_str,
        "child_scratch_digest": json.dumps(child_scratch_digest) if child_scratch_digest else "(none)",
        "context_budget_hint": "(unknown — use cascading distillation if context feels dense)",
        "skills_catalogue": skills_catalogue,
    }

    stage_prompt = _safe_sub(ORCHESTRATOR_SYSTEM_PROMPT, subs)
    system_prompt = build_system_prompt(
        stage_prompt=stage_prompt,
        global_rules=governance["global_rules"],
        admin_rules_ledger=governance["admin_rules_ledger"],
        user_instruction=user_instruction,
    )

    url, headers, body_model = resolve_model_call(orchestrator_model)
    # BRT-ORC-RM — reasoning-family orchestrators burn thousands of hidden
    # chain-of-thought tokens before the first visible byte; giving them
    # only 2000 max_tokens truncates them mid-thought and returns empty
    # JSON. _resolve_max_tokens_for_model bumps the cap to >= 8000 for
    # detected reasoning models; non-reasoning models keep the default.
    max_tokens = _resolve_max_tokens_for_model(
        orchestrator_model, "ORCHESTRATOR_MAX_TOKENS", default=2000
    )

    # BRT-ORC-02b — ask LM Studio / OpenRouter for JSON-only output at the
    # API level so the orchestrator can't wander into prose. Both backends
    # honour `response_format: {"type":"json_object"}` via the
    # OpenAI-compatible surface. Safe to include unconditionally — backends
    # that don't recognise the key ignore it.
    payload = {
        "model": body_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_instruction if mode == "plan" else "continue"},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    async def _post_and_collect() -> tuple[str, dict, int]:
        """POST once; return (content, usage, latency_ms). Raises BRT-ORC-001."""
        t_start = time.monotonic()
        try:
            async with httpx.AsyncClient(timeout=_MODEL_CALL_TIMEOUT) as client:
                resp = await client.post(url, json=payload, headers=headers)
            resp.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ValueError(f"BRT-ORC-001: orchestrator call timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"BRT-ORC-001: HTTP {exc.response.status_code} from orchestrator"
            ) from exc
        except httpx.RequestError as exc:
            raise ValueError(f"BRT-ORC-001: request error calling orchestrator: {exc}") from exc
        latency = int((time.monotonic() - t_start) * 1000)
        body_resp = resp.json()
        content_resp = body_resp["choices"][0]["message"]["content"]
        usage_resp = body_resp.get("usage", {})
        return content_resp, usage_resp, latency

    content, usage, latency_ms = await _post_and_collect()
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)

    # BRT-ORC-02b — ALWAYS log the raw payload_out before attempting to
    # parse it. Previously _extract_json raised ValueError before
    # write_event ran, so the raw prose that caused the failure was
    # lost. Parsing happens AFTER the decision event is scheduled.
    parsed: Optional[dict] = None
    parse_error: Optional[Exception] = None
    try:
        parsed = _extract_json(content)
    except ValueError as exc:
        parse_error = exc

    try:
        await write_event(
            "orchestrator_decision",
            run_id=run_id,
            model_id=orchestrator_model.model_id,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            latency_ms=latency_ms,
            decision={
                "mode": mode,
                "parsed": parsed,
                "rationale": (parsed or {}).get("rationale", ""),
                "parse_error": str(parse_error) if parse_error else None,
            },
            payload_in=system_prompt[:2000],
            payload_out=content[:2000],
            db_path=db_path,
        )
    except Exception:
        # Logging must never mask the real failure; fall through.
        pass

    if parsed is not None:
        return parsed

    # BRT-ORC-02b — one retry with a "JSON ONLY" reinforcement. Works for
    # the ~5 % of cases where response_format was ignored or the model
    # leaked prose despite it. Second failure propagates the original
    # BRT-ORC-002 so the run fails with a real reason (now captured by
    # BUG-002/BRT-PIP-010).
    retry_system = system_prompt + (
        "\n\nCRITICAL: Your entire reply must be valid JSON only. "
        "Do not emit prose, markdown, or commentary. Return a single JSON "
        "object matching the schema described above."
    )
    payload["messages"][0]["content"] = retry_system
    content_retry, usage_retry, latency_retry = await _post_and_collect()
    tokens_in_retry = usage_retry.get("prompt_tokens", 0)
    tokens_out_retry = usage_retry.get("completion_tokens", 0)

    parsed_retry: Optional[dict] = None
    retry_error: Optional[Exception] = None
    try:
        parsed_retry = _extract_json(content_retry)
    except ValueError as exc:
        retry_error = exc

    try:
        await write_event(
            "orchestrator_decision",
            run_id=run_id,
            model_id=orchestrator_model.model_id,
            tokens_input=tokens_in_retry,
            tokens_output=tokens_out_retry,
            latency_ms=latency_retry,
            decision={
                "mode": mode,
                "parsed": parsed_retry,
                "rationale": (parsed_retry or {}).get("rationale", ""),
                "retry": True,
                "parse_error": str(retry_error) if retry_error else None,
            },
            payload_in=retry_system[:2000],
            payload_out=content_retry[:2000],
            db_path=db_path,
        )
    except Exception:
        pass

    if parsed_retry is not None:
        return parsed_retry

    raise ValueError(
        f"BRT-ORC-002: orchestrator returned unparseable JSON after retry "
        f"(mode={mode}); first_error={parse_error}; retry_error={retry_error}"
    )


def _build_task_from_node(
    node: dict,
    run_id: UUID,
    current_depth: int,
    max_depth: int,
    level_path: str,
    model_endpoint: str,
    model_name: str,
    user_instruction: str,
    results: Optional[dict] = None,
) -> WrappedTask:
    """Convert a plan node dict into a WrappedTask.

    Handles skill-based nodes (has skill_id) and custom nodes.
    For state_op nodes, packages the op/params/source into dependency_input.
    For apply_to_each nodes: if ``inputs_ref`` is a string and matches a
    prior node id in ``results``, the upstream envelope's output is walked
    for an ``items`` / ``results`` list and stamped into
    ``dependency_input['items']`` — BRT-APE-01.

    Raises:
        ValueError: BRT-ORC-008 on build failure.
    """
    results = results or {}
    node_type = node.get("type", "answerer")

    # If skill node
    if node.get("skill_id"):
        try:
            lib = _get_skill_lib()
            return lib.render(
                skill_id=node["skill_id"],
                version=node.get("skill_version", 1),
                args=node.get("skill_args", {}),
                run_id=run_id,
                current_depth=current_depth,
                max_depth=max_depth,
                level_path=level_path,
                trigger_reason=node.get("trigger_reason", "initial_plan"),
            )
        except Exception as exc:
            raise ValueError(f"BRT-ORC-008: skill render failed: {exc}") from exc

    # state_op node
    if node_type == "state_op":
        dep_input = {
            "op": node.get("op", "count"),
            "params": node.get("params", {}),
            "items": node.get("items", []),
        }
        source = node.get("source")
        if source:
            dep_input["source_ref"] = source

        return WrappedTask(
            envelope_id=uuid4(),
            run_id=run_id,
            subtask_id=uuid4(),
            type="state_op",
            current_depth=current_depth,
            max_depth=max_depth,
            level_path=level_path,
            instruction=f"Apply state_op: {node.get('op', 'count')}",
            output_schema={"result": "any"},
            dependency_input=dep_input,
            trigger_reason=node.get("trigger_reason", "initial_plan"),
        )

    # apply_to_each node
    if node_type == "apply_to_each":
        # BRT-APE-01 — if inputs_ref is a string that matches a prior node id,
        # resolve the upstream envelope output to a concrete list of items
        # so the expander sees dependency_input['items'] (not an unresolved
        # symbolic reference). Falls back to whatever the orchestrator wrote
        # directly into node['items'] if available.
        inputs_ref_raw = node.get("inputs_ref", "")
        items_from_node = node.get("items", [])
        resolved_items = items_from_node
        if (
            isinstance(inputs_ref_raw, str)
            and inputs_ref_raw
            and inputs_ref_raw in results
            and not items_from_node
        ):
            upstream_env = results[inputs_ref_raw]
            upstream_out = getattr(upstream_env, "output", {}) or {}
            for key in ("items", "results", "list"):
                candidate = upstream_out.get(key)
                if isinstance(candidate, list):
                    resolved_items = candidate
                    break

        dep_input = {
            "inputs_ref": inputs_ref_raw,
            "items": resolved_items,
            "concurrency_limit": node.get("concurrency_limit", 20),
        }
        return WrappedTask(
            envelope_id=uuid4(),
            run_id=run_id,
            subtask_id=uuid4(),
            type="apply_to_each",
            current_depth=current_depth,
            max_depth=max_depth,
            level_path=level_path,
            model_endpoint=model_endpoint,
            model_name=model_name,
            instruction=node.get("instruction", ""),
            context=node.get("context", ""),
            output_schema=node.get("output_schema", {}),
            side_effects=node.get("side_effects", []),
            dependency_input=dep_input,
            trigger_reason=node.get("trigger_reason", "initial_plan"),
        )

    # answerer or orchestrate
    # BRT-PR-01a — when the node declares depends_on and matching upstream
    # envelopes are in results, inject their outputs into the child's
    # dependency_input AND append a short "Upstream outputs:" block to the
    # context string so the model can read them without needing to know the
    # dependency_input schema.
    depends_on = node.get("depends_on") or []
    base_context = node.get("context", "")
    base_dep_input = dict(node.get("dependency_input", {}))
    if isinstance(depends_on, list) and depends_on:
        upstream_results: dict = {}
        missing: list[str] = []
        for upstream_id in depends_on:
            if upstream_id in results:
                env = results[upstream_id]
                upstream_results[upstream_id] = getattr(env, "output", {}) or {}
            else:
                missing.append(upstream_id)
        if upstream_results:
            base_dep_input["upstream_results"] = upstream_results
            block = "\n\nUpstream stage outputs (from depends_on):\n" + json.dumps(
                upstream_results, indent=2
            )
            base_context = (base_context or "") + block
        if missing:
            # Fire-and-forget warning; dispatch continues with whatever we
            # could resolve. BRT-ORC-017 is logged from the dispatch loop
            # because we don't have db_path here.
            base_dep_input["_depends_on_missing"] = missing

    # BRT-WEB-01 — propagate tool_requests from the plan JSON into the task.
    # The executor will run them before the LLM call and inject results.
    raw_tool_requests = node.get("tool_requests") or []
    tool_requests: list[ToolRequest] = []
    for req in raw_tool_requests:
        if isinstance(req, dict) and req.get("tool"):
            tool_requests.append(
                ToolRequest(tool=str(req["tool"]), args=req.get("args") or {})
            )

    return WrappedTask(
        envelope_id=uuid4(),
        run_id=run_id,
        subtask_id=uuid4(),
        type=node_type if node_type == "orchestrate" else "answerer",
        current_depth=current_depth,
        max_depth=max_depth,
        level_path=level_path,
        model_endpoint=model_endpoint,
        model_name=model_name,
        instruction=node.get("instruction", user_instruction),
        context=base_context,
        state_query=node.get("state_query"),
        output_schema=node.get("output_schema", {}),
        side_effects=node.get("side_effects", []),
        tool_requests=tool_requests,
        dependency_input=base_dep_input,
        trigger_reason=node.get("trigger_reason", "initial_plan"),
    )


async def orchestrate_run(
    run_id: UUID,
    user_instruction: str,
    thread_window: list[dict] = None,
    rolling_summary: str = "",
    db_path: str = "data/borg_route.db",
    current_depth: int = 0,
) -> str:
    """Main orchestration loop. Returns the final response text.

    Raises:
        ValueError: BRT-ORC-001..009 on fatal failures.
    """
    if thread_window is None:
        thread_window = []

    governance = await load_governance(lambda key: get_config(key, db_path=db_path))

    # Read routing knobs from config (falls back to env, then hardcoded defaults).
    # The orchestrator IS the brain of every run — a mid-tier brain produces
    # mid-tier plans. Default 9.0 forces a frontier-or-near-frontier model;
    # the matrix already seeds 6 OpenRouter rows that meet this bar, and
    # cold-start ships with Sonnet-4.6 + Kimi-K2-Thinking pre-checked.
    configured_quality = await get_config("orchestrator_quality_requirement", db_path=db_path)
    quality_req = float(
        configured_quality if configured_quality is not None
        else os.getenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    )
    configured_time_cost = await get_config("time_cost_per_sec", db_path=db_path)
    time_cost = float(
        configured_time_cost if configured_time_cost is not None
        else os.getenv("TIME_COST_PER_SEC", "0.0001")
    )

    # BRT-ORC-02 — no silent fallback. Previously a ValueError from
    # _select_orchestrator_model was swallowed and replaced with a synthetic
    # _pinned_model(endpoint=""), which then produced httpx.UnsupportedProtocol
    # deep in the call stack with no run trace. Let BRT-ORC-007 propagate so
    # the run row flips to failed with a real reason.
    # `run_id` is threaded so the degrade path can emit BRT-ORC-NO-FRONTIER
    # to the run trace.
    orch_model = await _select_orchestrator_model(
        "reasoning",
        db_path=db_path,
        quality_requirement=quality_req,
        time_cost_per_sec=time_cost,
        run_id=run_id,
    )

    skill_lib = _get_skill_lib()
    skills_catalogue = skill_lib.catalogue_for_prompt()

    matrix = _get_matrix()
    available_models_brief = ", ".join(
        m.model_id for m in matrix.all()[:5]
    ) or "(none discovered)"

    base_kwargs = dict(
        run_id=run_id,
        user_instruction=user_instruction,
        current_depth=current_depth,
        max_depth=MAX_DEPTH,
        thread_window=thread_window,
        rolling_summary=rolling_summary,
        skills_catalogue=skills_catalogue,
        governance=governance,
        available_models_brief=available_models_brief,
        orchestrator_model=orch_model,
        db_path=db_path,
    )

    # BRT-ZERO-01 — Rule Zero shortcut. For unambiguously conversational
    # short prompts, skip plan/re_plan and go straight to compose. Saves
    # ~2-3 orchestrator calls and ~$0.0035 per run on trivial inputs. The
    # shortcut can be disabled with BORG_RULE_ZERO_SHORTCUT=false or the
    # matching config key (useful when debugging the full plan path).
    shortcut_cfg = await get_config("rule_zero_shortcut", db_path=db_path)
    if shortcut_cfg is None:
        shortcut_enabled = os.getenv("BORG_RULE_ZERO_SHORTCUT", "true").strip().lower() != "false"
    else:
        shortcut_enabled = str(shortcut_cfg).strip().lower() != "false"
    if shortcut_enabled and _is_trivial_conversational(user_instruction):
        try:
            await write_event(
                "orchestrator_decision",
                run_id=run_id,
                decision={
                    "mode": "rule_zero_shortcut",
                    "reason": "trivial-conversational",
                    "len": len(user_instruction or ""),
                },
                db_path=db_path,
            )
        except Exception:
            pass
        compose_result = await _invoke_orchestrator_model(
            mode="compose",
            stage_results={},
            child_scratch_digest={},
            **base_kwargs,
        )
        shortcut_resp = compose_result.get("response")
        if shortcut_resp:
            return shortcut_resp
        # BRT-ZERO-01b — the shortcut compose came back empty OR smuggled
        # a plan node under `plan.nodes`. Either way we're about to redo
        # the work via the full plan path, so surface a shortcut_escalated
        # event. Cost-win telemetry must NOT count this run as a Rule Zero
        # save; it's actually a Rule Zero miss + full plan on top. The
        # event uses event_type='reorchestration' (allowed by the logs
        # CHECK constraint) with a code field for filtering.
        try:
            await write_event(
                "reorchestration",
                run_id=run_id,
                decision={
                    "code": "BRT-ZERO-01-ESCALATED",
                    "msg": "Rule Zero shortcut compose returned no direct response; "
                           "falling through to full plan path.",
                    "had_plan_nodes": bool(
                        (compose_result.get("plan") or {}).get("nodes")
                    ),
                },
                db_path=db_path,
            )
        except Exception:
            pass
        # Fall through to the normal plan path.

    # --- PLAN MODE ---
    plan_result = await _invoke_orchestrator_model(
        mode="plan",
        stage_results={},
        child_scratch_digest={},
        **base_kwargs,
    )

    if plan_result.get("direct_answer"):
        return plan_result["direct_answer"]

    plan = plan_result.get("plan")
    if not plan or not plan.get("nodes"):
        # Graceful fallback to compose
        compose_result = await _invoke_orchestrator_model(
            mode="compose",
            stage_results={},
            child_scratch_digest={},
            **base_kwargs,
        )
        return compose_result.get("response", "I was unable to generate a response.")

    current_nodes = plan["nodes"]
    results: dict[str, InboundEnvelope] = {}
    child_digests: dict = {}
    loop_count = 0

    # Select a default answerer model. BRT-ORC-02 — make the fallback visible:
    # if select_model raises (usually no model meets the quality bar), fall
    # back to the orchestrator model but log a warning event so the run
    # trace shows the demotion.
    try:
        default_model = select_model(
            matrix=matrix,
            task_type="reasoning",
            quality_requirement=quality_req,
            time_cost_per_sec=time_cost,
        )
        answerer_endpoint = default_model.endpoint or ""
        answerer_name = default_model.model_id
    except Exception as exc:
        answerer_endpoint = orch_model.endpoint or ""
        answerer_name = orch_model.model_id
        # Note: logs.event_type has a CHECK constraint; 'warning' is not a
        # permitted value, so BRT-ORC-015 rides on 'error' with its own code
        # in the decision payload. UI/trace filters on the code when they
        # need severity granularity.
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-ORC-015",
                    "severity": "warning",
                    "msg": (
                        "default answerer selection failed; falling back to "
                        "orchestrator model"
                    ),
                    "fallback_model": answerer_name,
                    "err": str(exc),
                },
                db_path=db_path,
            )
        except Exception:
            pass

    while loop_count < MAX_LOOPS:
        loop_count += 1

        # Find ready nodes. BRT-PR-01a — depends_on ids that reference nodes
        # not present in the plan at all (e.g. orchestrator hallucinated an
        # upstream id) are IGNORED by the ready-check — they would otherwise
        # block the node forever. The mismatch is logged as BRT-ORC-017 when
        # the task is built below.
        completed_ids = set(results.keys())
        known_ids = {n.get("id") for n in current_nodes if n.get("id")}

        def _deps_ready(node_deps: list) -> bool:
            for d in node_deps or []:
                if d in known_ids and d not in completed_ids:
                    return False
            return True

        ready = [
            n for n in current_nodes
            if n.get("id") not in completed_ids
            and _deps_ready(n.get("depends_on", []))
        ]

        if not ready:
            break

        # Dispatch ready nodes in parallel
        tasks_to_dispatch = []
        for i, node in enumerate(ready):
            node_id = node.get("id", f"n{i}")
            level = f"{current_depth+1}{chr(ord('a') + i % 26)}"
            try:
                task = _build_task_from_node(
                    node=node,
                    run_id=run_id,
                    current_depth=current_depth + 1,
                    max_depth=MAX_DEPTH,
                    level_path=level,
                    model_endpoint=answerer_endpoint,
                    model_name=answerer_name,
                    user_instruction=user_instruction,
                    results=results,
                )
                # BRT-PR-01a — log any depends_on ids that didn't resolve.
                missing_deps = (task.dependency_input or {}).pop(
                    "_depends_on_missing", None
                )
                if missing_deps:
                    try:
                        await write_event(
                            "error",
                            run_id=run_id,
                            envelope_id=task.envelope_id,
                            decision={
                                "code": "BRT-ORC-017",
                                "severity": "warning",
                                "msg": "depends_on ids not resolved",
                                "node_id": node_id,
                                "missing": missing_deps,
                            },
                            db_path=db_path,
                        )
                    except Exception:
                        pass
                tasks_to_dispatch.append((node_id, node, task))
            except Exception as exc:
                # Node build failed — record as failed result
                results[node_id] = InboundEnvelope(
                    envelope_id=uuid4(),
                    subtask_id=uuid4(),
                    status="failed",
                    output={},
                    failure_reason=str(exc),
                )

        # Save and execute in parallel. BRT-ORC-02 — envelope persistence
        # errors used to be swallowed silently (bare except: pass), so a
        # broken DB produced a run that appeared to complete but had no
        # envelopes/logs to show. Now we log BRT-ORC-016 and continue.
        async def _dispatch_one(node_id: str, node: dict, task: WrappedTask):
            try:
                await save_envelope(task, db_path=db_path)
            except Exception as exc:
                try:
                    await write_event(
                        "error",
                        run_id=run_id,
                        envelope_id=task.envelope_id,
                        decision={
                            "code": "BRT-ORC-016",
                            "msg": "envelope save failed",
                            "err": str(exc),
                        },
                        db_path=db_path,
                    )
                except Exception:
                    pass
            try:
                env = await execute(task, db_path=db_path)
            except Exception as exc:
                env = InboundEnvelope(
                    envelope_id=task.envelope_id,
                    subtask_id=task.subtask_id,
                    status="failed",
                    output={},
                    failure_reason=f"BRT-ORC-004: {exc}",
                )
            try:
                await update_envelope_inbound(task.envelope_id, env, db_path=db_path)
            except Exception as exc:
                try:
                    await write_event(
                        "error",
                        run_id=run_id,
                        envelope_id=task.envelope_id,
                        decision={
                            "code": "BRT-ORC-016",
                            "msg": "envelope inbound update failed",
                            "err": str(exc),
                        },
                        db_path=db_path,
                    )
                except Exception:
                    pass

            # Fold-in for orchestrate nodes (Law 2)
            if node.get("type") == "orchestrate":
                digest = await fold_in(
                    run_id=run_id,
                    child_level_path=task.level_path,
                    child_model_id=task.model_name,
                    db_path=db_path,
                )
                if digest:
                    child_digests[node_id] = digest

            return node_id, env

        dispatched = await asyncio.gather(
            *[_dispatch_one(nid, n, t) for nid, n, t in tasks_to_dispatch]
        )
        for nid, env in dispatched:
            results[nid] = env

        # Check if all nodes dispatched
        all_node_ids = {n.get("id", f"n{i}") for i, n in enumerate(current_nodes)}
        if all_node_ids.issubset(set(results.keys())):
            # All nodes done — move to re_plan
            pass

        # --- RE_PLAN MODE ---
        replan_result = await _invoke_orchestrator_model(
            mode="re_plan",
            stage_results=results,
            child_scratch_digest=child_digests,
            **base_kwargs,
        )

        action = replan_result.get("action", "compose")

        if action == "compose":
            break
        elif action in ("continue", "cascade"):
            next_nodes = replan_result.get("next_nodes", [])
            if not next_nodes:
                break
            current_nodes = next_nodes
        elif action == "fail":
            rationale = replan_result.get("rationale", "No rationale provided.")
            raise ValueError(f"BRT-ORC-006: orchestrator failed the run: {rationale}")
        else:
            # Unknown action — compose anyway
            break

    if loop_count >= MAX_LOOPS:
        await write_event(
            "error",
            run_id=run_id,
            decision={"error": "BRT-ORC-005: max loop count exceeded"},
            db_path=db_path,
        )

    # --- COMPOSE MODE ---
    compose_result = await _invoke_orchestrator_model(
        mode="compose",
        stage_results=results,
        child_scratch_digest=child_digests,
        **base_kwargs,
    )

    response = compose_result.get("response")
    if not response:
        raise ValueError("BRT-ORC-009: compose mode returned no response")

    return response
