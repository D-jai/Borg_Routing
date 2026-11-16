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
- BRT-ORC-018: orchestrator response truncated by max_tokens; auto-retried
               once at doubled cap, ceiling 16000, only when the first cap
               is below 16000 (info; recorded as "length_retry": true and
               "code": "BRT-ORC-018" on that call's orchestrator_decision
               event, whose tokens are the sum of both attempts)
- BRT-ORC-023: skill lookup failed while routing a step (warning; the step
               is routed without the skill's tier and specialty)
- BRT-ORC-019: no checked model cleared the quality floor for a step's tier
               and kind of work; the step ran on the run's default model
               (warning; run continues)

Optimisation hooks (no error code):
- BRT-ZERO-01: for unambiguously conversational short prompts, skip
               plan/re_plan and go straight to compose (saves ~2-3
               orchestrator calls). Disable with BORG_RULE_ZERO_SHORTCUT=false
               or the rule_zero_shortcut config key.
"""
import asyncio
import json
import logging
import math
import os
import re
import time
from datetime import datetime, timezone
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
from src.registry.node_router import (
    DEFAULT_FLOORS,
    fallback_decision,
    select_model_for_node,
)
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


def reload_matrix() -> None:
    """Invalidate the orchestrator's in-memory matrix cache.

    BUG-023 — operators flipping the `checked` flag via `PATCH /models/.../checked`
    were seeing the change in `GET /models/matrix` immediately, but the
    orchestrator engine kept routing to the old set until uvicorn restart
    because `_shared_matrix` was loaded once at first call and never
    re-read. Calling this from the PATCH handler forces the next router
    request to reload from `data/capability_matrix.yaml` (the source of
    truth that PATCH writes to). Cheap because the file is tiny.
    """
    global _shared_matrix
    _shared_matrix = None


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


def _undouble_braces(template: str) -> str:
    """Turn a template's escaped braces (``{{`` and ``}}``) into single ones.

    The orchestrator prompt writes its JSON examples with doubled braces, as
    str.format would need, but placeholders are filled by plain replacement
    (``_safe_sub``), so without this the model reads ``{{"type": ...}}``.

    Run this on the template BEFORE substitution, never after: substituted
    JSON legitimately ends in ``}}``. The prompt also holds plain JSON whose
    nested close is a real ``}}`` (the web_search example), so a ``}}`` is
    treated as an escape only when it closes an escaped ``{{``.
    """
    out: list[str] = []
    opens: list[bool] = []          # True for an escaped "{{", False for a plain "{"
    i, n = 0, len(template)
    while i < n:
        ch = template[i]
        doubled = i + 1 < n and template[i + 1] == ch
        if ch == "{":
            opens.append(doubled)
            out.append("{")
            i += 2 if doubled else 1
        elif ch == "}":
            if doubled and opens and opens[-1]:
                opens.pop()
                i += 2
            else:
                if opens:
                    opens.pop()
                i += 1
            out.append("}")
        else:
            out.append(ch)
            i += 1
    return "".join(out)


# The prompt as the model should read it; computed once, the template is constant.
_ORCHESTRATOR_TEMPLATE = _undouble_braces(ORCHESTRATOR_SYSTEM_PROMPT)

def _per_million(per_token: float) -> str:
    """A per-token price as dollars per million tokens, readable at any size.

    $1 and above: two decimals ("$5.00"). Below $1: three significant
    figures ("$0.15", "$0.001"), so a cheap paid model never reads "$0.00".
    Exactly zero reads "$0".
    """
    value = float(per_token) * 1e6
    if value <= 0:
        return "$0"
    if value >= 1:
        return f"${value:.2f}"
    decimals = -math.floor(math.log10(value)) + 2
    text = f"{value:.{decimals}f}".rstrip("0").rstrip(".")
    return f"${text}"


def _available_models_brief(matrix: CapabilityMatrix) -> str:
    """The pool as the planner sees it: checked models only, most capable
    first, each with what it is best at and what it costs.

    Unchecked rows are left out: the planner must not recommend a model the
    router is not allowed to use.
    """
    rows = [m for m in matrix.all() if m.checked and m.endpoint]
    if not rows:
        return "(no models are checked in the pool)"
    rows.sort(key=lambda m: m.task_scores.get("reasoning", 0.0), reverse=True)

    lines = []
    # Every checked row is listed: the work order says "the checked rows",
    # so there is no cap (ruling 16-Nov-2026).
    for m in rows:
        strongest = sorted(m.task_scores.items(), key=lambda kv: kv[1], reverse=True)[:3]
        strengths = ", ".join(f"{kind} {score:g}" for kind, score in strongest) or "not yet scored"
        tags = f" [{', '.join(m.specialty_tags)}]" if m.specialty_tags else ""
        if m.pricing is not None and not (
            m.pricing.input_per_token == 0 and m.pricing.output_per_token == 0
        ):
            price = (
                f"{_per_million(m.pricing.input_per_token)} in / "
                f"{_per_million(m.pricing.output_per_token)} out per million tokens"
            )
        elif m.pricing is not None:
            price = "free, no token price"
        else:
            price = "runs on this machine, no token price"
        lines.append(f"- {m.model_id}{tags}: best at {strengths}; {price}")
    return "\n".join(lines)


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
    governance_instruction: Optional[str] = None,
) -> dict:
    """One call to the orchestrator model. Returns parsed JSON dict.

    ``user_instruction`` is what this run must deliver. ``governance_instruction``
    is the operator's verbatim instruction for the whole job (Law 1); it
    differs from ``user_instruction`` only inside a child run, and defaults
    to it.

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

    stage_prompt = _safe_sub(_ORCHESTRATOR_TEMPLATE, subs)

    # BUG-017 — inject the current wall-clock date so reasoning models
    # don't fall back to their training-cutoff year when synthesising
    # time-sensitive web_search queries (e.g. "latest space news today
    # 2024 NASA" issued in 2026). Kimi K2 Thinking and other models
    # with mid-2024 cutoffs were observed silently inserting outdated
    # year tokens into Tavily queries. The date block sits between
    # governance and the stage prompt so it is read every call without
    # editing the static prompt template.
    current_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    date_block = (
        f"# Wall-Clock Reality\n"
        f"Today's date is {current_date} (UTC). This is the actual current\n"
        f"date — NOT your training cutoff. When the user's prompt mentions\n"
        f"'latest', 'today', 'this week', 'this month', 'now', 'current', or\n"
        f"any other time-sensitive phrasing, you MUST use this date when\n"
        f"constructing web_search queries. Never inject a hardcoded year\n"
        f"that disagrees with this date. If the prompt requires information\n"
        f"after your training cutoff, trust the retrieved web results over\n"
        f"your priors.\n\n---\n\n"
    )
    stage_prompt = date_block + stage_prompt

    # Law 1 — in a child run the governance block shows the operator's
    # instruction, so the child's own assignment is stated here instead.
    # (Only plan mode sends it as the user message; re_plan and compose send
    # "continue".) A top-level run needs no such block: the two are the same.
    governing = governance_instruction or user_instruction
    if governing.strip() != (user_instruction or "").strip():
        assignment_block = (
            "# Your Assignment In This Run\n"
            "You are orchestrating one sub-task of the operator's request shown\n"
            "in the governance layer above. The operator's instruction governs\n"
            "how you work; the sub-task below is what this run must deliver.\n\n"
            f"{user_instruction}\n\n---\n\n"
        )
        stage_prompt = assignment_block + stage_prompt

    system_prompt = build_system_prompt(
        stage_prompt=stage_prompt,
        global_rules=governance["global_rules"],
        admin_rules_ledger=governance["admin_rules_ledger"],
        user_instruction=governing,
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

    async def _post_and_collect() -> tuple[str, dict, int, str]:
        """POST once; return (content, usage, latency_ms, finish_reason).

        Raises BRT-ORC-001.
        """
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
        choice_resp = body_resp["choices"][0]
        content_resp = choice_resp["message"]["content"]
        finish_resp = (choice_resp.get("finish_reason") or "").strip()
        usage_resp = body_resp.get("usage", {})
        return content_resp, usage_resp, latency, finish_resp

    content, usage, latency_ms, finish_reason = await _post_and_collect()
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)

    # BRT-ORC-018 — orchestrator response truncated by the cap (BUG-012).
    # Retry ONCE with the cap doubled (capped at 16k) before falling
    # through to the parse-failure retry path. Without this, a "length"
    # cut produces partial JSON that may or may not parse — and even if
    # it does, the plan it encodes is broken.
    length_retried = False
    if finish_reason == "length" and max_tokens < 16000:
        retry_max = min(max_tokens * 2, 16000)
        if retry_max > max_tokens:
            length_retried = True
            payload["max_tokens"] = retry_max
            content_t, usage_t, latency_t, finish_reason = await _post_and_collect()
            tokens_in += usage_t.get("prompt_tokens", 0)
            tokens_out += usage_t.get("completion_tokens", 0)
            latency_ms += latency_t
            content = content_t
            max_tokens = retry_max

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
                # BRT-ORC-018 — recorded on this same event (no new
                # event_type: logs.event_type has a CHECK constraint).
                # tokens_input/tokens_output above are the sum of both
                # attempts when this flag is set.
                **(
                    {
                        "length_retry": True,
                        "code": "BRT-ORC-018",
                        "retry_max_tokens": max_tokens,
                    }
                    if length_retried
                    else {}
                ),
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
    content_retry, usage_retry, latency_retry, _finish_retry = await _post_and_collect()
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


async def _load_leaf_floors(db_path: str) -> dict[str, float]:
    """Minimum score a model needs to serve a step of each tier.

    Config key ``leaf_quality_<tier>`` wins, then env ``LEAF_QUALITY_<TIER>``,
    then the built-in default. An unreadable value falls back to the default
    rather than failing the run.
    """
    floors: dict[str, float] = {}
    for tier, default in DEFAULT_FLOORS.items():
        configured = await get_config(f"leaf_quality_{tier}", db_path=db_path)
        raw = configured if configured is not None else os.getenv(f"LEAF_QUALITY_{tier.upper()}")
        try:
            floors[tier] = float(raw) if raw is not None else default
        except (TypeError, ValueError):
            floors[tier] = default
    return floors


async def _skill_for_node(
    node: dict, run_id: UUID, node_id: str, db_path: str
) -> Optional[dict]:
    """The skill record a plan node invokes, or None.

    A failed lookup is not swallowed silently: it logs BRT-ORC-023 (warning)
    and the step is routed without the skill's tier and specialty.
    """
    skill_id = node.get("skill_id")
    if not skill_id:
        return None
    try:
        return _get_skill_lib().get(skill_id, node.get("skill_version", 1))
    except Exception as exc:
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-ORC-023",
                    "severity": "warning",
                    "msg": (
                        "skill lookup failed while routing a step; it is "
                        "routed without the skill's tier and specialty"
                    ),
                    "node_id": node_id,
                    "skill_id": skill_id,
                    "skill_version": node.get("skill_version", 1),
                    "err": str(exc),
                },
                db_path=db_path,
            )
        except Exception:
            pass
        return None


async def _log_route(
    run_id: UUID,
    node_id: str,
    envelope_id: UUID,
    decision: dict,
    db_path: str,
) -> None:
    """Write why a step went to its model into the run trace."""
    chosen = decision.get("chosen")
    reason = decision.get("reason")
    try:
        await write_event(
            "orchestrator_decision",
            run_id=run_id,
            envelope_id=envelope_id,
            model_id=chosen,
            decision={
                "mode": "route",
                "node_id": node_id,
                "msg": f"{node_id} -> {chosen} ({reason})",
                **decision,
            },
            db_path=db_path,
        )
        if reason == "fallback_default":
            await write_event(
                "error",
                run_id=run_id,
                envelope_id=envelope_id,
                decision={
                    "code": "BRT-ORC-019",
                    "severity": "warning",
                    "msg": (
                        "no checked model cleared the quality floor for this "
                        "step; it ran on the run's default model"
                    ),
                    "node_id": node_id,
                    "task_type": decision.get("task_type"),
                    "tier": decision.get("tier"),
                    "floor": decision.get("floor"),
                    "fallback_model": chosen,
                    "err": decision.get("error"),
                },
                db_path=db_path,
            )
    except Exception:
        # Logging must never fail a step.
        pass


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
    governance_instruction: Optional[str] = None,
) -> WrappedTask:
    """Convert a plan node dict into a WrappedTask.

    ``user_instruction`` is this run's instruction (the fallback task text
    for a node with none). ``governance_instruction`` is the operator's
    verbatim instruction for the whole job and is stamped on every task
    (Law 1); it defaults to ``user_instruction``.

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
    governing = governance_instruction or user_instruction

    # If skill node
    if node.get("skill_id"):
        try:
            lib = _get_skill_lib()
            rendered = lib.render(
                skill_id=node["skill_id"],
                version=node.get("skill_version", 1),
                args=node.get("skill_args", {}),
                run_id=run_id,
                current_depth=current_depth,
                max_depth=max_depth,
                level_path=level_path,
                trigger_reason=node.get("trigger_reason", "initial_plan"),
            )
            # The skill template carries no model; the step's routed model
            # is stamped here, or the executor fails it with BRT-ANS-001.
            return rendered.model_copy(
                update={
                    "model_endpoint": model_endpoint,
                    "model_name": model_name,
                    "governance_instruction": governing,
                }
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
            governance_instruction=governing,
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
            governance_instruction=governing,
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

    # BRT-ANS-009 — honour planner-declared per-node token cap if present.
    # Schema default (4000) wins when the node didn't ask for one. The
    # executor treats this value as a FLOOR over ANSWERER_MAX_TOKENS.
    raw_node_cap = node.get("max_output_tokens")
    try:
        node_cap = int(raw_node_cap) if raw_node_cap is not None else None
    except (TypeError, ValueError):
        node_cap = None

    task_kwargs = dict(
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
        governance_instruction=governing,
        context=base_context,
        state_query=node.get("state_query"),
        output_schema=node.get("output_schema", {}),
        side_effects=node.get("side_effects", []),
        tool_requests=tool_requests,
        dependency_input=base_dep_input,
        trigger_reason=node.get("trigger_reason", "initial_plan"),
    )
    if node_cap and node_cap > 0:
        task_kwargs["max_output_tokens"] = node_cap

    return WrappedTask(**task_kwargs)


async def orchestrate_run(
    run_id: UUID,
    user_instruction: str,
    thread_window: list[dict] = None,
    rolling_summary: str = "",
    db_path: str = "data/borg_route.db",
    current_depth: int = 0,
    root_instruction: Optional[str] = None,
) -> str:
    """Main orchestration loop. Returns the final response text.

    ``root_instruction`` is the operator's verbatim instruction for the
    whole job. A child run is given it so every call it makes stays governed
    by what the operator asked (Law 1) while it plans ``user_instruction``,
    its own sub-task. A top-level run leaves it None.

    Raises:
        ValueError: BRT-ORC-001..009 on fatal failures.
    """
    if thread_window is None:
        thread_window = []

    governance_instruction = root_instruction or user_instruction

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
    # One model per line under the prompt's "Available models:" label.
    available_models_brief = "\n" + _available_models_brief(matrix)

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
        governance_instruction=governance_instruction,
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
        # Graceful fallback to compose. BUG-020 — pre-fix this path
        # also fell through to a vague "I was unable" string when the
        # model returned an action-shaped control signal. Now uses
        # the shared compose-result decoder so action=fail surfaces
        # the rationale and action=compose lifts from stage_results.
        compose_result = await _invoke_orchestrator_model(
            mode="compose",
            stage_results={},
            child_scratch_digest={},
            **base_kwargs,
        )
        try:
            return await _handle_compose_result(
                compose_result=compose_result,
                results={},
                run_id=run_id,
                db_path=db_path,
            )
        except ValueError:
            # _handle_compose_result raises BRT-ORC-009 when nothing is
            # usable. In the empty-plan branch we still want a graceful
            # user-facing string rather than a 500, so preserve the
            # historical default here.
            return "I was unable to generate a response."

    current_nodes = plan["nodes"]
    results: dict[str, InboundEnvelope] = {}
    child_digests: dict = {}
    loop_count = 0

    # Each step is routed to its own model (select_model_for_node, below).
    # The default model is only the fallback for a step that no checked
    # model can serve at its floor. BRT-ORC-02 — make that fallback visible:
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
    except Exception as exc:
        default_model = orch_model
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
                    "fallback_model": default_model.model_id,
                    "err": str(exc),
                },
                db_path=db_path,
            )
        except Exception:
            pass

    leaf_floors = await _load_leaf_floors(db_path)

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

            # Route this step to its own model. A state_op calls no model.
            step_model = default_model
            route_decision: Optional[dict] = None
            if node.get("type", "answerer") != "state_op":
                node_skill = await _skill_for_node(node, run_id, node_id, db_path)
                try:
                    step_model, route_decision = select_model_for_node(
                        node=node,
                        skill=node_skill,
                        matrix=matrix,
                        floors=leaf_floors,
                        time_cost_per_sec=time_cost,
                        default_model=default_model,
                    )
                except Exception as exc:
                    step_model = default_model
                    route_decision = fallback_decision(
                        node=node,
                        skill=node_skill,
                        floors=leaf_floors,
                        default_model=default_model,
                        error=f"routing failed: {exc}",
                    )

            try:
                task = _build_task_from_node(
                    node=node,
                    run_id=run_id,
                    current_depth=current_depth + 1,
                    max_depth=MAX_DEPTH,
                    level_path=level,
                    model_endpoint=step_model.endpoint or "",
                    model_name=step_model.model_id,
                    user_instruction=user_instruction,
                    results=results,
                    governance_instruction=governance_instruction,
                )
                if route_decision is not None:
                    await _log_route(
                        run_id, node_id, task.envelope_id, route_decision, db_path
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

    return await _handle_compose_result(
        compose_result=compose_result,
        results=results,
        run_id=run_id,
        db_path=db_path,
    )


async def _handle_compose_result(
    compose_result: dict,
    results: dict,
    run_id: UUID,
    db_path: str,
) -> str:
    """BUG-020 (regression of round-2 BUG-010) — compose-result decoder.

    Compose mode previously only recognised `{"response": "..."}` and
    `{"direct_answer": "..."}`; when the orchestrator returned a
    re_plan-shaped control signal in compose mode
    (`{"action": "fail|compose|continue"}`), every branch fell through
    and BRT-ORC-009 fired even when stage_results contained a perfectly
    usable answerer envelope. This decoder explicitly covers all three
    control-signal shapes plus the `direct_answer` alias, and only
    raises BRT-ORC-009 when there is genuinely no usable text anywhere
    in the run. Used by every site that calls compose-mode (the main
    plan→re_plan→compose loop AND the empty-plan graceful fallback).
    """
    response = (
        compose_result.get("response")
        or compose_result.get("direct_answer")
        or ""
    )
    if response and response.strip():
        return response

    action = (compose_result.get("action") or "").lower().strip()

    # action == "fail" — surface rationale instead of silently 500ing.
    # Run completes (not raises); user sees a coherent decline message.
    if action == "fail":
        rationale = (
            compose_result.get("rationale")
            or compose_result.get("msg")
            or "The orchestrator declined to compose a final answer."
        )
        try:
            await write_event(
                "orchestrator_decision",
                run_id=run_id,
                decision={
                    "code": "BUG-020-COMPOSE-DECLINED",
                    "msg": "compose returned action=fail; surfacing rationale to user",
                    "rationale": rationale,
                },
                db_path=db_path,
            )
        except Exception:
            pass
        return rationale

    # action == "compose" or "continue" or anything else WITHOUT a
    # `response` field — lift the most recent resolved answerer envelope
    # from stage_results. Pre-fix this path raised BRT-ORC-009 and
    # discarded all the upstream answerer work.
    salvaged = _salvage_answer_from_stage_results(results)
    if salvaged:
        try:
            await write_event(
                "orchestrator_decision",
                run_id=run_id,
                decision={
                    "code": "BUG-020-COMPOSE-USED-STAGE-RESULTS",
                    "msg": "compose returned no response; lifted answer "
                           "from a resolved answerer envelope to avoid "
                           "discarding upstream work",
                    "compose_action": action or "(missing)",
                },
                db_path=db_path,
            )
        except Exception:
            pass
        return salvaged

    # Genuinely nothing usable anywhere — keep the original error so
    # operators still see the failure mode when it's real.
    raise ValueError("BRT-ORC-009: compose mode returned no response")


def _salvage_answer_from_stage_results(results: dict) -> Optional[str]:
    """BUG-020 helper — extract the best textual answer from any resolved
    envelope in stage_results. Iterates in insertion order and prefers
    later resolved envelopes (closer to compose); falls back to earlier
    ones. Recognises `output.response`, `output.answer`, `output.text`,
    `output.content` plus the bare `output` string for legacy shapes.
    Returns None if nothing usable is found.
    """
    if not results:
        return None

    candidates: list[str] = []
    for _node_id, env in results.items():
        if env is None:
            continue
        status = getattr(env, "status", None)
        if status != "resolved":
            continue
        out = getattr(env, "output", None) or {}
        if isinstance(out, str) and out.strip():
            candidates.append(out.strip())
            continue
        if not isinstance(out, dict):
            continue
        for key in ("response", "answer", "text", "content", "summary"):
            val = out.get(key)
            if isinstance(val, str) and val.strip():
                candidates.append(val.strip())
                break

    if not candidates:
        return None
    # Prefer the last (most recent) candidate — matches the temporal
    # intent of "use what came right before compose".
    return candidates[-1]
