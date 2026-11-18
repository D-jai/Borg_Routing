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
- BRT-ORC-NO-FRONTIER: quality bar relaxed below the default because no model qualified (warning)
- BRT-ORC-008: node builder failed for a plan node
- BRT-ORC-009: compose mode returned no response (and no step finished)
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
- BRT-ORC-024: a step waits, through its refs or depends_on, on a cycle of
               steps of the same plan, so it can never run; it fails before
               re_plan with no model call and the message names the cycle
- BRT-ORC-025: a step's depends_on step failed (cycle-failed included); the
               step fails before any model call and the message names the
               failed step (an item ref to it keeps BRT-APE-005)
- BRT-ORC-026: a re_plan step reused the id of a step that already ran; it
               was renamed <id>_2 (_3 ...) and the batch's refs to it follow
               (warning; the step runs)
- BRT-ORC-027: non-object entries dropped from a plan / re_plan step list
               (warning; nothing valid left = no steps, never an empty plan)
- BRT-ORC-028: compose reply in the wrong shape; its final text was used
               (info; no second call)
- BRT-ORC-029: compose reply had no final text; compose asked once more
- BRT-ORC-030: compose gave no final text twice; the answer is the finished
               steps' results with a note, runs.answer_kind = "salvaged"
               (warning; the run completes)
- BRT-ORC-031: compose returned action=fail; its rationale is the answer (answer_kind "declined").
- BRT-ORC-032: compose used a row marker that names no list step; asked once
               more, then salvaged (warning; planner ruling 21a)
- BRT-LANE-009: a rows block placed by code copied a document (raw_local /
               all_local); the run fails closed (planner ruling 21b)

On-premise lane (docs/design/on_premise_lane.md; on only when the run has a
lane context, see src/lane/context.py):
- BRT-LANE-002: no local model for a step that must stay local, or for the
               planner in all_local (the step, or the run, fails; no fallback)
- BRT-LANE-005: documents on an orchestrate step outside all_local
- BRT-ORC-003 (documents): a planner direct_answer or an empty plan is
               rejected when the run has documents
Raw marking: a step is raw when its node names documents (``documents``,
``"inputs_ref": "documents"``, a skill's ``items_ref: documents``) or reads,
through ``depends_on``, ``inputs_ref``, ``source`` or any of a skill's
``*_ref`` args (``items_ref``, ``failed_items_ref``, ``evidence_ref``), a raw
step whose output did not pass the output guard. Raw steps are routed to local models only,
except in ``by_reference``, where any checked model may serve them.

Optimisation hooks (no error code):
- BRT-ZERO-01: for unambiguously conversational short prompts, skip
               plan/re_plan and go straight to compose (saves ~2-3
               orchestrator calls). Disable with BORG_RULE_ZERO_SHORTCUT=false
               or the rule_zero_shortcut config key.
"""
import asyncio
import contextvars
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
from src.lane.context import (
    RAW_FAILURE_DEFAULT, LaneContext, current_lane, doc_label, failure_of, prompt_json,
    raw_failure_code, raw_failures_hidden, raw_step_failed_envelope,
)
from src.lane.errors import LaneError, RawStepError
from src.lane.outbound import call_scope, send_outbound
from src.logging.writer import write_event
from src.orchestrator.apply_to_each import (
    _resolve_items,
    bad_ref_type_message,
    empty_ref_list_message,
    failed_ref_message,
)
from src.orchestrator.fold_in import fold_in
from src.prompts.base import build_system_prompt, load_governance
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.cost import call_dollars, price_with_baseline, resolve_baseline_with_note
from src.registry.model_call import resolve_model_call
from src.registry.models import ModelInfo
from src.registry.node_router import (
    DEFAULT_FLOORS,
    fallback_decision,
    local_view,
    select_model_for_node,
    tokens_needed,
)
from src.registry.router import select_model
from src.skills.library import SkillLibrary

MAX_LOOPS = 10
MAX_DEPTH = 5
_MODEL_CALL_TIMEOUT = 180   # default; BORG_ORCHESTRATOR_CALL_TIMEOUT overrides it per call
_MATRIX_PATH = "data/capability_matrix.yaml"
_SKILLS_PATH = "data/skills.yaml"

_shared_matrix: Optional[CapabilityMatrix] = None

# What the chat alias asked of this run (tier, default kind of work, local
# first), read when each step is routed. A context variable so child runs
# started from a step inherit it without threading it through every call.
_route_options: contextvars.ContextVar[Optional[dict]] = contextvars.ContextVar(
    "borg_route_options", default=None
)
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


def reload_matrix(matrix: Optional[CapabilityMatrix] = None) -> None:
    """Make the engine see the API's capability matrix (R2).

    The API (Discover, Get Model Info, the checkbox) and the engine must work
    on one matrix object, or a change made in the UI is not routed until a
    restart. ``main.py`` hands the engine its matrix at start-up; after each
    change the API calls this with its own object, so the two stay one even if
    something replaced the engine's copy. With no argument the shared object is
    re-read from its YAML file in place (it is never swapped for a second copy).
    """
    global _shared_matrix
    if matrix is not None:
        _shared_matrix = matrix
        return
    if _shared_matrix is not None:
        try:
            _shared_matrix.load()
        except Exception:
            pass


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


def call_timeout_from_env(env_var: str, default: float) -> float:
    """Seconds one model call may take: ``env_var`` when it is a positive finite number, else ``default``.

    Read at call time, so a changed environment applies to the next call. An invalid value (not a number,
    zero, negative, nan or inf) is never guessed at: the default is used and a warning is logged.
    Used for ``BORG_ORCHESTRATOR_CALL_TIMEOUT`` (default 180) and ``BORG_ANSWERER_CALL_TIMEOUT`` (default 120).
    """
    raw = os.getenv(env_var)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(raw)
    except ValueError:
        value = float("nan")
    if not math.isfinite(value) or value <= 0:
        logger.warning("%s=%r is not a positive number of seconds; using the default %s s", env_var, raw, default)
        return default
    return value


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
            value_str = prompt_json(value)
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
    local_filter=None,
) -> ModelInfo:
    """Select the orchestrator ModelInfo row.

    Resolution order:
      1. Pinned config (`orchestrator_endpoint` set in DB)
      2. Capability matrix at the requested quality_requirement (default 9.0)
      3. Degraded fallback at 5.1 — emits BRT-ORC-NO-FRONTIER trace event
         (when `run_id` is provided) so the dashboard can flag the run.
      4. Raises BRT-ORC-007.

    ``local_filter`` (lane_mode all_local): every step above, the pinned
    endpoint and the degraded path included, sees local models only, and a
    miss raises BRT-LANE-002 instead of BRT-ORC-007.

    Returns: ModelInfo

    Raises:
        ValueError: BRT-ORC-007 if no model available.
        LaneError: BRT-LANE-002 if ``local_filter`` is set and no local model qualifies.
    """
    # Check config for a pinned orchestrator endpoint
    pinned = await get_config("orchestrator_endpoint", db_path=db_path)
    if pinned:
        name = await get_config("orchestrator_model_name", db_path=db_path) or "orchestrator"
        pinned_model = _pinned_model(pinned, name)
        if local_filter is not None and not local_filter(pinned_model):
            raise LaneError(
                "BRT-LANE-002",
                f"the pinned orchestrator endpoint {pinned} is not local; "
                "lane_mode all_local needs a local planner",
            )
        return pinned_model

    # Try to select from capability matrix
    matrix = _get_matrix()
    if local_filter is not None:
        matrix = local_view(matrix, local_filter)
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
    # 5.1 — just above the scorer's flat 5.0 fallback (a model it could not
    # research), so rows that have genuinely been scored are preferred.
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

    if local_filter is not None:
        raise LaneError(
            "BRT-LANE-002",
            "no local model can plan this run (lane_mode all_local); tick a local "
            "model, or score one, so it clears the orchestrator bar or 5.1",
        )
    raise ValueError(
        "BRT-ORC-007: no orchestrator model available. "
        "Check that at least one model is checked on the Models page, "
        "or set orchestrator_endpoint in config."
    )


# The user message of a re_plan or compose call: one short line naming the
# mode (planner ruling 20, R2: re_plan and compose both sent "continue", and
# compose replies came back in the re_plan shape). The shared template in
# src/prompts/ is unchanged.
_MODE_USER_MESSAGES = {
    "re_plan": "Mode: re_plan. Reply with the next action.",
    "compose": "Mode: compose. Reply with the final answer.",
}
# The one compose re-ask after a reply with no final answer (R2 step 2).
_COMPOSE_REASK = (
    'Mode: compose. Your last reply had no final answer. Reply with '
    '{"response": "<the final answer>"} only.'
)

_FAILURE_MESSAGE_MAX = 200   # deliberate cap on a failure message sent to plan / re_plan
_FAILURE_CODE_FALLBACK = "BRT-ORC-004"
_MODEL_FAILURE_CODE = "BRT-ANS-010"   # the model itself declined or reported failure


def _string_leaves(value: object) -> list:
    """Every string (keys included) inside a step output, decoded: the
    quoted text a row-marker hit may repeat (ruling 21)."""
    found: list = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, dict):
        for k, v in value.items():
            if isinstance(k, str):
                found.append(k)
            found.extend(_string_leaves(v))
    elif isinstance(value, list):
        for v in value:
            found.extend(_string_leaves(v))
    return found


def _stage_output_for_prompt(env: InboundEnvelope, mode: str = "re_plan") -> dict:
    """One step's entry in the stage_results block of an orchestrator prompt.

    A passing step shows its output unchanged. A failed step also carries
    ``{"failure": {"code", "message"}}`` merged over whatever output exists:
    re_plan reads outputs, not failure_reason. The message is the first 200
    characters of the reason; in compose mode the failure is the code only.

    The code: a reason the model wrote (the envelope was built from its JSON)
    is BRT-ANS-010 and is never parsed; otherwise it is the code the
    runtime's reason starts with, else a fixed fallback.

    A raw step (decided by the ``data_scope`` of the task that produced the
    envelope, not by ``lane_guarded``) shows only ``{"code", "doc"}``: never
    its output or any message text. In ``by_reference`` (no output guard ran)
    only a failure the runtime built that way keeps that shape; any other raw
    failure is shown by the non-raw rules above.
    """
    if env.status != "failed":
        return env.output
    if env._data_scope == "raw":
        shown = (env.output or {}).get("failure")
        if env.lane_guarded is False and isinstance(shown, dict) and shown.get("code"):
            # A failure the runtime built (a lane refusal): code + doc ids.
            # by_reference: a failure raised while the step ran also carries
            # its first 200 characters (not shown to compose).
            failure = {"code": shown["code"], "doc": shown.get("doc")}
            if "message" in shown and mode != "compose":
                failure["message"] = str(shown["message"])[:_FAILURE_MESSAGE_MAX]
            return {"failure": failure}
    if env._data_scope == "raw" and raw_failures_hidden():
        code = (
            RAW_FAILURE_DEFAULT if env._reason_from_model
            else raw_failure_code(env.failure_reason)
        )
        return {"failure": failure_of(code, [env._doc_label] if env._doc_label else [])}
    reason = str(env.failure_reason or "")
    code = (
        _MODEL_FAILURE_CODE if env._reason_from_model
        else raw_failure_code(reason, _FAILURE_CODE_FALLBACK)
    )
    entry = dict(env.output or {})
    failure = {"code": code}
    if mode != "compose":
        failure["message"] = reason[:_FAILURE_MESSAGE_MAX]
    entry["failure"] = failure
    return entry


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
    user_message: Optional[str] = None,
    extra_user_lines: Optional[str] = None,
    pointer_lines: Optional[str] = None,
) -> dict:
    """One call to the orchestrator model. Returns parsed JSON dict.

    ``extra_user_lines`` (compose only, planner ruling 21) is the row-marker
    block added after the user message's first line; None leaves the
    request byte-identical. ``pointer_lines`` are the list steps' lines: with
    them, a compose reply cut at the cap is retried once, always, with the
    row-marker pointer as its user message (``row_markers.ROWS_POINTER``).

    The user message is the instruction in plan mode and one short line
    naming the mode otherwise (``_MODE_USER_MESSAGES``; planner ruling 20,
    R2). ``user_message`` replaces it (the compose re-ask).

    ``user_instruction`` is what this run must deliver. ``governance_instruction``
    is the operator's verbatim instruction for the whole job (Law 1); it
    differs from ``user_instruction`` only inside a child run, and defaults
    to it.

    Raises:
        ValueError: BRT-ORC-001 on HTTP error.
        ValueError: BRT-ORC-002 on JSON parse failure.
    """
    stage_results_str = prompt_json(
        {k: _stage_output_for_prompt(v, mode) for k, v in stage_results.items()}, indent=2
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
            prompt_json(aggregated_tools, indent=2, default=str)
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
        "thread_window": prompt_json(thread_window, indent=2),
        "rolling_summary": rolling_summary or "(none)",
        "stage_results": stage_results_str,
        "tool_results": tool_results_str,
        "child_scratch_digest": prompt_json(child_scratch_digest) if child_scratch_digest else "(none)",
        "context_budget_hint": "(unknown — use cascading distillation if context feels dense)",
        "skills_catalogue": skills_catalogue,
    }

    stage_prompt = _safe_sub(_ORCHESTRATOR_TEMPLATE, subs)

    # Inject the current wall-clock date so reasoning models
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

    # On-premise lane — the planner, re_plan and compose see a manifest of
    # the run's documents (ids, types, sizes, names unless hidden), never
    # their text. Added in code: the prompt file is protected.
    lane = current_lane()
    if lane is not None and lane.has_documents:
        stage_prompt = lane.manifest_block() + stage_prompt

    # Law 1 — in a child run the governance block shows the operator's
    # instruction, so the child's own assignment is stated here instead.
    # (Only plan mode sends it as the user message; re_plan and compose send
    # one line naming the mode.) A top-level run needs no such block: the two are the same.
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
            {"role": "user", "content": (
                user_message if user_message is not None
                else user_instruction if mode == "plan"
                else _MODE_USER_MESSAGES.get(mode, "continue")
            ) + (("\n" + extra_user_lines) if extra_user_lines else "")},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }

    attempts = [0]

    async def _post_and_collect() -> tuple[str, dict, int, str]:
        """POST once; return (content, usage, latency_ms, finish_reason).

        Every attempt goes through ``send_outbound`` (the lane's checks; a
        plain POST when the lane is off).

        Raises BRT-ORC-001; LaneError (BRT-LANE-*) when the lane refuses.
        """
        t_start = time.monotonic()
        attempts[0] += 1
        try:
            with call_scope(run_id):
                resp = await send_outbound(
                    url, payload, headers,
                    timeout=call_timeout_from_env("BORG_ORCHESTRATOR_CALL_TIMEOUT", _MODEL_CALL_TIMEOUT),
                    model=orchestrator_model,
                    must_stay_local=lane is not None and lane.all_local,
                    purpose=f"orchestrator:{mode}",
                    attempt=attempts[0],
                )
            resp.raise_for_status()
        except httpx.PoolTimeout as exc:
            raise ValueError(
                "BRT-ANS-011: local connection pool exhausted calling the "
                "orchestrator; too many calls at once, not a model timeout"
            ) from exc
        except httpx.TimeoutException as exc:
            raise ValueError(f"BRT-ORC-001: orchestrator call timed out") from exc
        except httpx.HTTPStatusError as exc:
            raise ValueError(
                f"BRT-ORC-001: HTTP {exc.response.status_code} from orchestrator"
            ) from exc
        except httpx.RequestError as exc:
            raise ValueError(f"BRT-ORC-001: request error calling orchestrator: {exc}") from exc
        latency = int((time.monotonic() - t_start) * 1000)
        try:
            body_resp = resp.json()
            choice_resp = body_resp["choices"][0]
            content_resp = choice_resp["message"]["content"]
            finish_resp = (choice_resp.get("finish_reason") or "").strip()
            usage_resp = body_resp.get("usage", {})
        except (KeyError, IndexError, TypeError, AttributeError) as exc:
            # An HTTP 200 whose body is an error object or has no choices.
            raise ValueError(
                f"BRT-ORC-001: malformed response from orchestrator (no usable choices): {exc!r}"
            ) from exc
        return content_resp, usage_resp, latency, finish_resp

    content, usage, latency_ms, finish_reason = await _post_and_collect()
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)

    # BRT-ORC-018 — orchestrator response truncated by the cap.
    # Retry ONCE with the cap doubled (capped at 16k) before falling
    # through to the parse-failure retry path. Without this, a "length"
    # cut produces partial JSON that may or may not parse — and even if
    # it does, the plan it encodes is broken.
    length_retried = False
    # Planner ruling 21: a compose reply cut while list steps exist is
    # retried once, always (also at the ceiling), pointing to the marker.
    rows_pointer = mode == "compose" and bool(pointer_lines)
    pointer_cut_again = False
    if finish_reason == "length" and (max_tokens < 16000 or rows_pointer):
        retry_max = max(max_tokens, min(max_tokens * 2, 16000))
        if retry_max > max_tokens or rows_pointer:
            length_retried = True
            payload["max_tokens"] = retry_max
            if rows_pointer:
                from src.orchestrator.row_markers import ROWS_POINTER  # noqa: PLC0415

                payload["messages"][1]["content"] = ROWS_POINTER + "\n" + pointer_lines
            try:
                content_t, usage_t, latency_t, finish_reason = await _post_and_collect()
            except ValueError as exc:
                # The first attempt was billed even though the retry failed:
                # log it (existing event type) before re-raising.
                try:
                    paid_dollars, paid_baseline = await price_with_baseline(
                        orchestrator_model, tokens_in, tokens_out, db_path
                    )
                    await write_event(
                        "orchestrator_decision",
                        run_id=run_id,
                        model_id=orchestrator_model.model_id,
                        tokens_input=tokens_in,
                        tokens_output=tokens_out,
                        dollars=paid_dollars,
                        baseline_dollars=paid_baseline,
                        latency_ms=latency_ms,
                        decision={
                            "mode": mode,
                            "parsed": None,
                            "length_retry_failed": True,
                            "finish_reason": "length",
                            "code": "BRT-ORC-018",
                            "error": str(exc)[:300],
                        },
                        payload_in=system_prompt[:2000],
                        payload_out=content[:2000],
                        db_path=db_path,
                    )
                except Exception:
                    pass
                raise
            tokens_in += usage_t.get("prompt_tokens", 0)
            tokens_out += usage_t.get("completion_tokens", 0)
            latency_ms += latency_t
            content = content_t
            max_tokens = retry_max
            pointer_cut_again = rows_pointer and finish_reason == "length"

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

    # Work order 1.6 — price the call(s) behind this event. tokens_in/out
    # already sum both attempts when a length retry happened.
    dollars, baseline_dollars = await price_with_baseline(
        orchestrator_model, tokens_in, tokens_out, db_path
    )

    try:
        await write_event(
            "orchestrator_decision",
            run_id=run_id,
            model_id=orchestrator_model.model_id,
            tokens_input=tokens_in,
            tokens_output=tokens_out,
            dollars=dollars,
            baseline_dollars=baseline_dollars,
            latency_ms=latency_ms,
            decision={
                "mode": mode,
                "parsed": parsed,
                "rationale": (parsed or {}).get("rationale", ""),
                "parse_error": str(parse_error) if parse_error else None,
                # Always logged (planner ruling 20): settles truncation
                # questions from the database. After a length retry it is
                # the retry's; the first attempt's was "length".
                "finish_reason": finish_reason,
                # BRT-ORC-018 — recorded on this same event (no new
                # event_type: logs.event_type has a CHECK constraint).
                # tokens_input/tokens_output above are the sum of both
                # attempts when this flag is set.
                **(
                    {
                        "length_retry": True,
                        "code": "BRT-ORC-018",
                        "retry_max_tokens": max_tokens,
                        "first_finish_reason": "length",
                        **({"retry_message": "rows_pointer"} if rows_pointer else {}),
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

    if pointer_cut_again:
        # Ruling 21: the pointer retry was cut too; no JSON-only retry. The
        # caller salvages (a compose call that raises).
        raise ValueError(
            "BRT-ORC-002: compose reply cut at the cap again after the row-marker "
            f"retry; not parseable: {parse_error}"
        )

    # BRT-ORC-02b — one retry with a "JSON ONLY" reinforcement. Works for
    # the ~5 % of cases where response_format was ignored or the model
    # leaked prose despite it. Second failure propagates the original
    # BRT-ORC-002 so the run fails with a real reason (now captured by
    # BRT-PIP-010).
    retry_system = system_prompt + (
        "\n\nCRITICAL: Your entire reply must be valid JSON only. "
        "Do not emit prose, markdown, or commentary. Return a single JSON "
        "object matching the schema described above."
    )
    payload["messages"][0]["content"] = retry_system
    content_retry, usage_retry, latency_retry, finish_retry = await _post_and_collect()
    tokens_in_retry = usage_retry.get("prompt_tokens", 0)
    tokens_out_retry = usage_retry.get("completion_tokens", 0)

    parsed_retry: Optional[dict] = None
    retry_error: Optional[Exception] = None
    try:
        parsed_retry = _extract_json(content_retry)
    except ValueError as exc:
        retry_error = exc

    dollars_retry, baseline_retry = await price_with_baseline(
        orchestrator_model, tokens_in_retry, tokens_out_retry, db_path
    )

    try:
        await write_event(
            "orchestrator_decision",
            run_id=run_id,
            model_id=orchestrator_model.model_id,
            tokens_input=tokens_in_retry,
            tokens_output=tokens_out_retry,
            dollars=dollars_retry,
            baseline_dollars=baseline_retry,
            latency_ms=latency_retry,
            decision={
                "mode": mode,
                "parsed": parsed_retry,
                "rationale": (parsed_retry or {}).get("rationale", ""),
                "retry": True,
                "finish_reason": finish_retry,
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
    node_type: str = "answerer",
    item_count: Optional[int] = None,
    demoted: bool = False,
) -> None:
    """Write why a step went to its model into the run trace.

    Every figure here is an estimate (token counts are guessed from text
    length). ``est_dollars`` and ``baseline_est_cost`` are dollars only and
    use the same token estimate, so they compare like for like; ``est_cost``
    adds the time cost and is what the router ranks by.
    """
    chosen = decision.get("chosen")
    reason = decision.get("reason")
    decision = dict(decision)
    try:
        # Work order 1.5 - what the same step would cost on the baseline model.
        baseline, baseline_note = await resolve_baseline_with_note(_get_matrix(), db_path)
        decision["baseline"] = baseline.model_id if baseline else None
        if baseline_note:
            decision["baseline_note"] = baseline_note
        est_in = decision.get("est_input_tokens")
        est_out = decision.get("est_output_tokens")
        decision["baseline_est_cost"] = (
            call_dollars(baseline, est_in, est_out)
            if baseline is not None and est_in is not None and est_out is not None
            else None
        )
    except Exception:
        decision.setdefault("baseline", None)
        decision.setdefault("baseline_est_cost", None)

    if node_type == "orchestrate" and demoted:
        # At max depth the executor runs this step as one answerer call on
        # ``chosen``: it is a normal single call, with real estimates.
        decision["note"] = "demoted at max depth"
    elif node_type == "orchestrate":
        # The child run picks its own models; the routed model is only the
        # fallback if the step is demoted at max depth, so no estimate of a
        # single call describes this step.
        decision["served_by"] = "child_run"
        decision["note"] = (
            "a child run plans this step and picks its own models; 'chosen' is "
            "only the fallback model if the step is demoted at max depth"
        )
        decision["est_dollars"] = None
        decision["baseline_est_cost"] = None
    elif node_type == "apply_to_each" and item_count:
        # The figures above cover one call; the step makes one per item.
        decision["items"] = item_count
        per_est = decision.get("est_dollars")
        per_base = decision.get("baseline_est_cost")
        decision["per_item_est_dollars"] = per_est
        decision["per_item_baseline_est_cost"] = per_base
        decision["est_dollars"] = None if per_est is None else per_est * item_count
        decision["baseline_est_cost"] = None if per_base is None else per_base * item_count
        if decision.get("est_cost") is not None:
            decision["est_cost_total"] = decision["est_cost"] * item_count
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


def _upstream_list(ref: object, results: dict) -> Optional[list]:
    """The list an earlier step produced, or None.

    ``ref`` is the id of a finished step (``inputs_ref`` of an apply_to_each
    step, ``source`` of a state_op). The step's output is read for an
    ``items``, ``results``, ``list`` or ``result`` key holding a list.
    """
    if not isinstance(ref, str) or not ref or ref not in results:
        return None
    out = getattr(results[ref], "output", {}) or {}
    for key in ("items", "results", "list", "result"):
        candidate = out.get(key)
        if isinstance(candidate, list):
            return candidate
    return None


def _upstream_groups(ref: object, results: dict) -> Optional[dict]:
    """The groups of rows a ``group_by`` step (without ``count``) produced, or None.

    Read from the step's ``result`` when it is an object whose every value is
    a list (ruling 25d). Counts per group are not rows, so they give None.
    """
    if not isinstance(ref, str) or not ref or ref not in results:
        return None
    out = getattr(results[ref], "output", {}) or {}
    groups = out.get("result") if isinstance(out, dict) else None
    if isinstance(groups, dict) and groups and all(isinstance(v, list) for v in groups.values()):
        return groups
    return None


def _state_op_source(node: dict) -> Optional[str]:
    """The step a state_op reads: ``source``, ``source_ref`` or ``inputs_ref``.

    ``inputs_ref`` is read as the source because the ready check and the
    failed-dependency check (``_node_refs``) already treat it as the step's
    input ref (review of 1071659): it is never silently ignored. A list is
    not a step id.
    """
    for key in ("source", "source_ref", "inputs_ref"):
        value = node.get(key)
        if isinstance(value, str) and value:
            return value
    return None


_DOCUMENTS_REF = "documents"


def _node_reads_documents(node: dict) -> bool:
    """True when a plan node names the run's documents (by id or as a set).

    ``"inputs_ref": "documents"`` is a reserved word for the document set,
    never a step id; a skill names it through any ``*_ref`` arg
    (``items_ref``, ``evidence_ref``, ``failed_items_ref`` ...) or
    ``inputs_ref``. A ``data_scope`` key written on the node is ignored: the
    runtime decides (design note 5.2).
    """
    if node.get("documents"):
        return True
    if node.get("inputs_ref") == _DOCUMENTS_REF:
        return True
    skill_args = node.get("skill_args") or {}
    return isinstance(skill_args, dict) and any(
        (str(key).endswith("_ref") or key == "inputs_ref") and value == _DOCUMENTS_REF
        for key, value in skill_args.items()
    )


def _node_refs(node: dict, documents_reserved: bool = True) -> list[str]:
    """Every earlier step a node reads: depends_on, inputs_ref, source, and
    a skill's ``*_ref`` args (``items_ref``, ``failed_items_ref``,
    ``evidence_ref``).

    Used by the ready check (a named step in the same plan is waited for, as
    if it were in ``depends_on``; planner ruling 1a) and by the lane's taint
    check. A ref given as a list is inline items, not a step id.
    ``"documents"`` names the run's documents, not a step, only while the
    lane is on (``documents_reserved``); with the lane off it is an ordinary
    step id.
    """
    def _is_step_ref(value: object) -> bool:
        return (
            isinstance(value, str)
            and bool(value)
            and not (documents_reserved and value == _DOCUMENTS_REF)
        )

    refs: list[str] = []
    deps = node.get("depends_on") or []
    if isinstance(deps, list):
        refs.extend(str(d) for d in deps)
    for key in ("inputs_ref", "source", "source_ref"):
        value = node.get(key)
        if _is_step_ref(value):
            refs.append(value)
    skill_args = node.get("skill_args") or {}
    if isinstance(skill_args, dict):
        for key, ref in skill_args.items():
            if str(key).endswith("_ref") and _is_step_ref(ref):
                refs.append(ref)
    return refs


def _inline_items(node: dict) -> Optional[list]:
    """Items a plan node carries itself, or None (planner ruling 1a).

    A list from the operator's message travels in the node: ``items`` on the
    node, ``items`` in a skill's args, or any of a skill's ``*_ref`` args
    (``items_ref``, ``failed_items_ref``) given as a list instead of a step
    id. A ``*_ref`` given as an empty list is returned as ``[]``, so the step
    fails with BRT-APE-005 instead of looking up a step named ``[]``.
    """
    if isinstance(node.get("items"), list) and node["items"]:
        return node["items"]
    skill_args = node.get("skill_args") or {}
    if isinstance(skill_args, dict):
        value = skill_args.get("items")
        if isinstance(value, list) and value:
            return value
        for key, value in skill_args.items():
            if str(key).endswith("_ref") and isinstance(value, list):
                return value
    return None


def _bad_ref_arg(skill_args: object) -> Optional[tuple[str, object]]:
    """The first ``*_ref`` skill arg that is neither a string nor a list."""
    if not isinstance(skill_args, dict):
        return None
    for key, value in skill_args.items():
        if str(key).endswith("_ref") and value is not None and not isinstance(value, (str, list)):
            return str(key), value
    return None


def _skill_reads_data(skill_def: dict) -> bool:
    """True for an answerer skill whose template names its data in a
    ``state_query`` (compare_and_rank, verify_claim). The query is never
    executed; the engine writes the data into the step's context instead."""
    template = skill_def.get("outbound_template") or {}
    return skill_def.get("envelope_type") == "answerer" and bool(template.get("state_query"))


def _numbered(label: str, entries: list) -> str:
    """Entries as a numbered list under a one-line heading."""
    lines = [f"{label} for this step ({len(entries)}; cite an entry by its number):"]
    for i, entry in enumerate(entries, start=1):
        text = entry if isinstance(entry, str) else json.dumps(entry, ensure_ascii=False)
        lines.append(f"{i}. {text}")
    return "\n".join(lines)


def _answerer_skill_data(
    node: dict,
    skill_def: dict,
    skill_args: object,
    inline_items: Optional[list],
    results: dict,
    lane: Optional[LaneContext],
) -> tuple[list[str], str]:
    """The data an answerer skill step reads: (document ids, context text).

    Inline items (``items`` or a ``*_ref`` given as a list) win; otherwise the
    ``*_ref`` names an earlier step of this run and its output is used (the
    taint check already reads that ref, so a raw step makes this step raw),
    or, with the lane on, ``documents`` names the run's documents by id.

    Raises:
        RuntimeError: BRT-SKL-008 when there is no data to give the step.
    """
    sid = skill_def.get("skill_id", "?")
    args = skill_args if isinstance(skill_args, dict) else {}
    refs = [str(a) for a in (skill_def.get("required_args") or {}) if str(a).endswith("_ref")]
    ref_key = refs[0] if refs else "items_ref"
    label = ref_key[: -len("_ref")].replace("_", " ").capitalize() or "Items"

    def _fail(why: str) -> RuntimeError:
        return RuntimeError(f"BRT-SKL-008: render failed — skill '{sid}': {why}")

    if inline_items is not None:
        if not inline_items:
            raise _fail(f"{ref_key} is an empty list; the step has no data")
        return [], _numbered(label, inline_items)
    ref = args.get(ref_key)
    if lane is not None and ref == _DOCUMENTS_REF:
        if not lane.has_documents:
            raise _fail(f"{ref_key} names the run's documents, but this run has none")
        return lane.all_part_ids(), ""
    if isinstance(ref, str) and ref:
        if ref not in results:
            raise _fail(f"{ref_key} '{ref}' names no finished step of this run and the "
                        f"step carries no items; no model call was made")
        if _failed_step(ref, results):
            raise _fail(f"{ref_key} '{ref}' names a step that failed; no model call was made")
        entries = _upstream_list(ref, results)
        if entries is None:
            out = getattr(results[ref], "output", {}) or {}
            if len(out) == 1 and isinstance(next(iter(out.values())), (str, int, float)):
                entries = [str(next(iter(out.values())))]
            else:
                entries = [out] if out else []
        if not entries:
            # The step ran and found nothing: that is the data (a claim with
            # no evidence is "insufficient"), not a planning error.
            return [], f"{label} for this step: none (step '{ref}' produced no entries)."
        return [], _numbered(label, entries)
    raise _fail(f"no data: give the items inline or set {ref_key} to a step id")


def _skill_data_size(
    node: dict,
    skill: Optional[dict],
    results: dict,
    lane: Optional[LaneContext],
) -> tuple[bool, int]:
    """(reads data, extra input tokens) for an answerer skill step whose data
    the engine writes into its context (compare_and_rank, verify_claim).

    The extra tokens are the context text built from an earlier step's output
    (``len // 4``, the router's own estimate); inline items are already in
    the step's ``skill_args``, which the router counts, so they add nothing
    here. Documents are counted by ``_doc_tokens``. A step whose data cannot
    be built adds nothing: the build fails it loudly (BRT-SKL-008).
    """
    if skill is None or not node.get("skill_id") or not _skill_reads_data(skill):
        return False, 0
    inline = _inline_items(node)
    try:
        docs, text = _answerer_skill_data(
            node, skill, node.get("skill_args") or {}, inline, results, lane
        )
    except Exception:
        return False, 0
    if inline is not None:
        return True, 0
    return bool(docs or text), len(text) // 4


def data_too_big_message(need: int, largest: int) -> str:
    """BRT-REG-020 failure text for a step whose data fits no checked model.

    Guidance first, so all of it fits the 200 characters re_plan is shown.
    """
    return (
        f"BRT-REG-020: this step's data needs ~{need} tokens with its answer; no "
        f"checked model's context holds it (largest {largest}). No model call was "
        f"made. Send fewer or shorter entries."
    )


def _failed_step(ref: object, results: dict) -> bool:
    """True when ``ref`` names a finished step of this run that failed."""
    return (
        isinstance(ref, str)
        and ref in results
        and getattr(results[ref], "status", None) == "failed"
    )


def _with_ids(nodes: list, taken: set) -> list:
    """The plan's nodes, each with an ``id``, so a step that ran is never
    taken for one still waiting.

    An explicit planner id is kept unchanged (a re_plan id that reuses a
    finished step's id was already renamed by ``_rename_reused_ids``). A
    node with none gets
    ``n<index>``, or ``n<index>_<k>`` when that is taken, so a generated id
    is unique across the whole run: never in ``taken`` (the run's results and
    every id given earlier in the run), never an id already in this plan.
    ``taken`` is updated with every id of this plan.
    """
    nodes = _normalised_ids(nodes)
    for n in nodes:
        if isinstance(n, dict) and n.get("id"):
            taken.add(str(n["id"]))
    out = []
    for i, n in enumerate(nodes):
        if isinstance(n, dict) and not n.get("id"):
            new_id, k = f"n{i}", 2
            while new_id in taken:
                new_id, k = f"n{i}_{k}", k + 1
            taken.add(new_id)
            n = {**n, "id": new_id}
        out.append(n)
    return out


_REF_KEYS = ("inputs_ref", "source", "source_ref")


def _id_key(value: object) -> object:
    """A step id in its one compared form (R1-R4 review): a number (not a
    bool) becomes its string, so ``1`` and ``"1"`` name the same step."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return value


def _normalised_ids(nodes: object) -> list:
    """The nodes with every ``id`` and ``depends_on`` entry in the compared
    form (``_id_key``), so results are keyed, and ids compared, the same way
    whatever type the planner wrote. Non-object entries pass through."""
    out = []
    for n in list(nodes or []):
        if isinstance(n, dict):
            node = dict(n)
            if "id" in node:
                node["id"] = _id_key(node["id"])
            deps = node.get("depends_on")
            if isinstance(deps, list):
                node["depends_on"] = [_id_key(d) for d in deps]
            elif deps is not None:
                node["depends_on"] = _id_key(deps)
            n = node
        out.append(n)
    return out


def _node_ref_values(node: dict) -> list:
    """Every string a node names another step by (depends_on, the
    ``_REF_KEYS``, skill ``*_ref`` args), as written."""
    refs: list = []
    deps = node.get("depends_on")
    if isinstance(deps, list):
        refs.extend(d for d in deps if isinstance(d, str))
    elif isinstance(deps, str):
        refs.append(deps)
    for key in _REF_KEYS:
        if isinstance(node.get(key), str):
            refs.append(node[key])
    args = node.get("skill_args")
    if isinstance(args, dict):
        refs.extend(v for k, v in args.items() if str(k).endswith("_ref") and isinstance(v, str))
    return refs


def _rename_reused_ids(nodes: list, done: set, taken: set) -> tuple[list, list[dict]]:
    """A re_plan batch whose steps reuse the id of a step that already ran,
    each such step given a fresh id (planner ruling 20, R1).

    A step whose ``id`` is in ``done`` (the run's results) is renamed
    ``<id>_2``, ``<id>_3`` ... (the first not in ``taken``, which holds every
    id the run has used, nor in this batch); ``taken`` is updated. References
    to a renamed id from OTHER steps of the same batch (``depends_on``,
    ``inputs_ref``, ``source``, ``source_ref``, any skill ``*_ref`` arg given
    as a string) are remapped to the new id; the renamed step's own refs to
    its old id, and every ref to an id not redefined in the batch, keep
    naming the earlier result, which stays under its original id.

    A remap that would close a cycle inside the batch (the renamed step
    waits, directly or through others, on the step whose ref would follow
    it) is not made: that ref keeps naming the earlier result (R1-R4
    review). Remaps are decided in batch order; refs that need no remap
    are counted first. Ids are compared in one form (``_id_key``).

    Returns (nodes, renames), ``renames`` being ``[{"from", "to"}]`` in
    batch order. Non-object entries are passed through untouched.
    """
    nodes = _normalised_ids(nodes)
    done = {str(_id_key(d)) for d in done}
    batch_ids = {str(n["id"]) for n in nodes if isinstance(n, dict) and n.get("id")}
    renamed_from: dict[int, str] = {}
    renamed_to: dict[int, str] = {}
    mapping: dict[str, str] = {}
    renames: list[dict] = []
    for i, n in enumerate(nodes):
        if not (isinstance(n, dict) and n.get("id") and str(n["id"]) in done):
            continue
        old = str(n["id"])
        k = 2
        while f"{old}_{k}" in taken or f"{old}_{k}" in batch_ids:
            k += 1
        new = f"{old}_{k}"
        taken.add(new)
        batch_ids.add(new)
        renamed_from[i] = old
        renamed_to[i] = new
        mapping.setdefault(old, new)
        renames.append({"from": old, "to": new})
    if not renames:
        return nodes, []

    def _remap(value: object, table: dict) -> object:
        return table.get(value, value) if isinstance(value, str) else value

    # Batch-local wait graph: index -> indexes it waits on. Refs that need
    # no remap first (a ref naming a batch id as written, not a reused one).
    index_of: dict[str, int] = {}
    for i, n in enumerate(nodes):
        if isinstance(n, dict) and n.get("id"):
            index_of.setdefault(renamed_to.get(i, str(n["id"])), i)
    waits: dict[int, set[int]] = {i: set() for i in range(len(nodes))}
    tables: dict[int, dict] = {}
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            continue
        tables[i] = {o: t for o, t in mapping.items() if o != renamed_from.get(i)}
        for ref in _node_ref_values(n):
            if ref not in tables[i] and ref in index_of and index_of[ref] != i:
                waits[i].add(index_of[ref])

    def _reaches(start: int, goal: int) -> bool:
        seen, stack = set(), [start]
        while stack:
            k = stack.pop()
            if k == goal:
                return True
            if k in seen:
                continue
            seen.add(k)
            stack.extend(waits[k])
        return False

    for i, n in enumerate(nodes):
        if i not in tables:
            continue
        refs = set(_node_ref_values(n))
        for old in list(tables[i]):
            if old not in refs:
                continue
            target = index_of.get(tables[i][old])
            if target is None:
                continue
            if _reaches(target, i):
                del tables[i][old]   # would close a cycle: keep the earlier result
            else:
                waits[i].add(target)

    out = []
    for i, n in enumerate(nodes):
        if not isinstance(n, dict):
            out.append(n)
            continue
        table = tables[i]
        node = dict(n)
        if i in renamed_to:
            node["id"] = renamed_to[i]
        if table:
            deps = node.get("depends_on")
            if isinstance(deps, list):
                node["depends_on"] = [_remap(d, table) for d in deps]
            elif isinstance(deps, str):
                node["depends_on"] = _remap(deps, table)
            for key in _REF_KEYS:
                if key in node:
                    node[key] = _remap(node[key], table)
            args = node.get("skill_args")
            if isinstance(args, dict):
                node["skill_args"] = {
                    k: (_remap(v, table) if str(k).endswith("_ref") else v)
                    for k, v in args.items()
                }
        out.append(node)
    return out, renames


async def _object_nodes(
    nodes: object, mode: str, run_id: UUID, db_path: str,
) -> list[dict]:
    """The plan's (or re_plan's) node list with every non-object entry
    dropped and logged (planner ruling 20, R3; BRT-ORC-027).

    Job C's re_plan ended ``next_nodes`` with the string ``"rationale: "``;
    the valid siblings still run. A ``nodes`` value that is not a list is
    dropped whole. An empty result is handled by the caller's existing path
    for a reply with no steps (never run as an empty plan).
    """
    if nodes is None or nodes == []:
        return []
    if isinstance(nodes, list):
        kept = [n for n in nodes if isinstance(n, dict)]
        dropped = [n for n in nodes if not isinstance(n, dict)]
    else:
        kept, dropped = [], [nodes]
    if dropped:
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-ORC-027",
                    "severity": "warning",
                    "msg": "non-object entries dropped from the step list",
                    "mode": mode,
                    "dropped": [repr(d)[:80] for d in dropped[:10]],
                    "dropped_count": len(dropped),
                    "kept_count": len(kept),
                },
                db_path=db_path,
            )
        except Exception:
            pass
    return kept


def failed_dependency_message(name: object) -> str:
    """BRT-ORC-025 failure text for a step whose ``depends_on`` step failed.

    Guidance first, the failed step's id last (cut to 40 characters), so the
    whole text fits the 200-character cap on what re_plan is shown.
    """
    return (
        "BRT-ORC-025: a step in this one's depends_on failed, so it was not run "
        "and made no model call; re-plan without it or fix that step first. "
        f"Failed step: '{str(name)[:40]}'"
    )


def _failed_dependency(node: dict, results: dict, documents_reserved: bool) -> Optional[str]:
    """The first ``depends_on`` step of ``node`` that failed, or None.

    A step the node also reads its items from (``inputs_ref``, ``source``, a
    skill's ``*_ref``) is left to the item path, which fails it with
    BRT-APE-005.
    """
    deps = node.get("depends_on") or []
    if not isinstance(deps, list):
        return None
    item_refs = set(_node_refs({k: v for k, v in node.items() if k != "depends_on"},
                               documents_reserved))
    for dep in deps:
        dep = str(dep)
        if dep not in item_refs and _failed_step(dep, results):
            return dep
    return None


def ref_cycle_message(node_id: str, cycle: Optional[list[str]]) -> str:
    """BRT-ORC-024 failure text for a step on a ref / depends_on cycle."""
    path = " -> ".join(cycle) if cycle else "steps that wait on each other"
    return (
        f"BRT-ORC-024: step '{node_id}' waits on a cycle of steps ({path}) "
        f"through inputs_ref, a skill's *_ref args, source or depends_on, so it "
        f"can never run; no model call was made. A step can only read steps "
        f"that do not read it back."
    )


async def _fail_ref_cycles(
    waiting: list[dict],
    edges: dict[str, list[str]],
    results: dict,
    raw_unstamped: set,
    run_id: UUID,
    lane: Optional[LaneContext],
    db_path: str,
) -> None:
    """Fail every waiting step that lies on a cycle (BRT-ORC-024).

    Called when steps are left but none is ready. A raw step's failure is a
    code plus document ids (the lane's shape); otherwise the message names
    the cycle. If no cycle is found (which the ready check rules out), every
    waiting step is failed so the loop always moves on.
    """
    cycles = {nid: _ref_cycle(nid, edges) for nid in edges}
    on_cycle = [n for n in waiting if cycles.get(n["id"])]
    for node in on_cycle or waiting:
        node_id = node["id"]
        message = ref_cycle_message(node_id, cycles.get(node_id))
        raw = lane is not None and (
            _node_reads_documents(node)
            or any(r in raw_unstamped for r in _node_refs(node))
        )
        if raw:
            part_ids: list[str] = []
            if lane.has_documents and _node_reads_documents(node):
                try:
                    part_ids = _node_document_ids(node, lane)
                except ValueError:
                    part_ids = []
            await _lane_refuse_node(
                results, raw_unstamped, run_id, node_id, "BRT-ORC-024", message,
                part_ids, db_path,
            )
            continue
        results[node_id] = InboundEnvelope(
            envelope_id=uuid4(),
            subtask_id=uuid4(),
            status="failed",
            output={},
            failure_reason=message,
        )
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={"code": "BRT-ORC-024", "msg": message, "node_id": node_id,
                          "cycle": cycles.get(node_id)},
                db_path=db_path,
            )
        except Exception:
            pass


def _ref_cycle(start: str, edges: dict[str, list[str]]) -> Optional[list[str]]:
    """A path ``start -> ... -> start`` through ``edges``, or None."""
    stack = [(start, [start])]
    seen: set[str] = set()
    while stack:
        cur, path = stack.pop()
        for nxt in edges.get(cur, []):
            if nxt == start:
                return path + [start]
            if nxt not in seen:
                seen.add(nxt)
                stack.append((nxt, path + [nxt]))
    return None


def _node_document_ids(node: dict, lane: LaneContext) -> list[str]:
    """The part ids a node reads. Raises BRT-ORC-008 for an unknown id."""
    if node.get("documents"):
        return lane.expand(node["documents"])
    return lane.all_part_ids()


def _doc_tokens(
    node: dict,
    part_ids: list[str],
    lane: Optional[LaneContext],
    skill: Optional[dict] = None,
) -> int:
    """Token estimate of the document text a raw step reads: the largest part
    for a per-item step, all of them for a step that reads them together (a
    node naming ``documents``, or an answerer skill such as verify_claim
    whose ``*_ref`` is ``documents``: one call sees every part)."""
    if lane is None or not part_ids:
        return 0
    if skill is not None:
        per_item = (
            skill.get("envelope_type") == "apply_to_each"
            or node.get("type") == "apply_to_each"
        )
    else:
        per_item = node.get("type") == "apply_to_each" or not node.get("documents")
    if per_item:
        return max(lane.chars_of([p]) for p in part_ids) // 4
    return lane.chars_of(part_ids) // 4


async def _lane_refuse_node(
    results: dict,
    raw_unstamped: Optional[set],
    run_id: UUID,
    node_id: str,
    code: str,
    message: str,
    part_ids: list[str],
    db_path: str,
) -> None:
    """Fail one step for a lane rule: nothing is sent. Upward the step shows a
    code and document ids only; the message goes to the local trace."""
    failure = failure_of(code, part_ids)
    results[node_id] = InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="failed",
        output={"failure": failure},
        failure_reason=json.dumps(failure),
        lane_guarded=False,
    )
    results[node_id]._data_scope = "raw"
    results[node_id]._doc_label = doc_label(part_ids)
    if raw_unstamped is not None:
        raw_unstamped.add(node_id)
    try:
        await write_event(
            "error",
            run_id=run_id,
            decision={"code": code, "msg": message, "node_id": node_id, "doc": failure["doc"]},
            db_path=db_path,
        )
    except Exception:
        pass


async def _fail_step_before_call(
    results: dict,
    raw_unstamped: Optional[set],
    run_id: UUID,
    node_id: str,
    code: str,
    message: str,
    part_ids: list[str],
    db_path: str,
    raw: bool,
) -> None:
    """Fail one step before routing or any model call.

    A raw step fails the lane's way (a code plus document ids upward, the
    message in the local trace); any other step carries the message, which
    re_plan sees.
    """
    if raw:
        await _lane_refuse_node(
            results, raw_unstamped, run_id, node_id, code, message, part_ids, db_path,
        )
        return
    results[node_id] = InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="failed",
        output={},
        failure_reason=message,
    )
    try:
        await write_event(
            "error",
            run_id=run_id,
            decision={"code": code, "msg": message, "node_id": node_id},
            db_path=db_path,
        )
    except Exception:
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
    data_scope: str = "derived",
    lane: Optional[LaneContext] = None,
) -> WrappedTask:
    """Convert a plan node dict into a WrappedTask.

    On-premise lane (``lane`` given): ``data_scope`` is stamped on the task,
    and a node that names documents gets their part ids, never their text:
    ``"inputs_ref": "documents"`` (or a skill's ``items_ref: documents``) on
    an apply_to_each makes one item per document part; ``documents`` on any
    node names the ids it reads. While the lane is on, ``"documents"`` is
    never looked up as a step id; with no documents loaded it fails the node
    (BRT-ORC-008). With the lane off it is an ordinary step id.

    ``user_instruction`` is this run's instruction (the fallback task text
    for a node with none). ``governance_instruction`` is the operator's
    verbatim instruction for the whole job and is stamped on every task
    (Law 1); it defaults to ``user_instruction``.

    Handles skill-based nodes (has skill_id) and custom nodes.
    For state_op nodes, packages the op/params/source into dependency_input.
    For apply_to_each nodes: if ``inputs_ref`` is a string and matches a
    prior node id in ``results``, the upstream envelope's output is walked
    for an ``items`` / ``results`` list and stamped into
    ``dependency_input['items']`` — BRT-APE-01. Items written in the node
    (``items``, or a skill's ``items`` / list-valued ``items_ref``) win. A
    ref naming no finished step, with no items of its own, is marked
    ``unresolved_ref`` and the expander fails it with BRT-APE-006 (ruling 1a).

    Raises:
        ValueError: BRT-ORC-008 on build failure.
    """
    results = results or {}
    node_type = node.get("type", "answerer")
    governing = governance_instruction or user_instruction

    def _all_parts() -> list[str]:
        if lane is None or not lane.has_documents:
            raise ValueError(
                "BRT-ORC-008: inputs_ref 'documents' names the run's documents, "
                "but this run has none"
            )
        return lane.all_part_ids()

    named_docs: list[str] = []
    if lane is not None and node.get("documents"):
        if not lane.has_documents:
            raise ValueError("BRT-ORC-008: the step names documents, but this run has none")
        named_docs = lane.expand(node["documents"])
    lane_fields = {"data_scope": data_scope} if lane is not None else {}

    # If skill node
    if node.get("skill_id"):
        skill_args = node.get("skill_args", {})
        inline_items = _inline_items(node)
        render_args = skill_args
        try:
            lib = _get_skill_lib()
            skill_def = lib.get(node["skill_id"], node.get("skill_version", 1))
            needs_data = _skill_reads_data(skill_def)
            if (
                (skill_def.get("envelope_type") == "apply_to_each" or needs_data)
                and isinstance(skill_args, dict)
            ):
                # Ruling 1a — inline items stand in for the skill's ref arg.
                # A *_ref arg given as a list renders as "" (the items travel
                # in dependency_input); a required *_ref arg left out is
                # filled with "" when the step carries items. Only for an
                # apply_to_each skill, or an answerer skill whose data the
                # engine writes into its context (skill render review): any
                # other skill still needs its ref and fails loudly
                # (BRT-SKL-005). The template is never changed.
                fill = {
                    k: "" for k, v in skill_args.items()
                    if str(k).endswith("_ref") and isinstance(v, list)
                }
                if inline_items is not None:
                    # A *_ref written as null beside inline items is the ref
                    # left out, as the plan prompt asks (demo run 19f56430).
                    for arg in skill_def.get("required_args", {}) or {}:
                        if str(arg).endswith("_ref") and skill_args.get(arg) is None:
                            fill[arg] = ""
                if fill:
                    render_args = {**skill_args, **fill}
            rendered = lib.render(
                skill_id=node["skill_id"],
                version=node.get("skill_version", 1),
                args=render_args,
                run_id=run_id,
                current_depth=current_depth,
                max_depth=max_depth,
                level_path=level_path,
                trigger_reason=node.get("trigger_reason", "initial_plan"),
            )
            # The skill template carries no model; the step's routed model
            # is stamped here, or the executor fails it with BRT-ANS-001.
            update = {
                "model_endpoint": model_endpoint,
                "model_name": model_name,
                "governance_instruction": governing,
                **lane_fields,
            }
            if render_args is not skill_args:
                update["skill_args"] = skill_args
            if rendered.type == "apply_to_each":
                # R14 — resolve the items from the earlier step the skill's
                # items_ref names, as a plan-written apply_to_each node does.
                dep = dict(rendered.dependency_input or {})
                if lane is not None and (named_docs or dep.get("inputs_ref") == _DOCUMENTS_REF):
                    # The run's documents, by part id (on-premise lane).
                    dep["items"] = named_docs or _all_parts()
                    update["dependency_input"] = dep
                    update["documents"] = dep["items"]
                elif inline_items is not None:
                    # Ruling 1a — items written in the step itself; an empty
                    # list fails with BRT-APE-005 in the expander.
                    dep["items"] = inline_items
                    if not inline_items:
                        empty_key = next(
                            (str(k) for k, v in skill_args.items()
                             if str(k).endswith("_ref") and v == []),
                            "items_ref",
                        ) if isinstance(skill_args, dict) else "items_ref"
                        dep["ref_failure"] = empty_ref_list_message(empty_key)
                    update["dependency_input"] = dep
                elif _bad_ref_arg(skill_args) is not None:
                    key, value = _bad_ref_arg(skill_args)
                    dep["ref_failure"] = bad_ref_type_message(key, value)
                    update["dependency_input"] = dep
                elif not dep.get("items"):
                    ref = dep.get("inputs_ref")
                    if _failed_step(ref, results):
                        # The step it reads failed: no items (BRT-APE-005).
                        dep["ref_failure"] = failed_ref_message(ref)
                        update["dependency_input"] = dep
                    elif (upstream := _upstream_list(ref, results)) is not None:
                        dep["items"] = upstream
                        update["dependency_input"] = dep
                    elif isinstance(ref, str) and ref and ref not in results:
                        # Names no step: the expander fails it (BRT-APE-006).
                        dep["unresolved_ref"] = ref
                        update["dependency_input"] = dep
            elif needs_data:
                # Skill render review: the template's state_query is never
                # executed, so the step's data goes into its context.
                docs, data_text = _answerer_skill_data(
                    node, skill_def, skill_args, inline_items, results, lane
                )
                if named_docs or docs:
                    update["documents"] = named_docs or docs
                if data_text:
                    update["context"] = "\n\n".join(
                        t for t in (rendered.context, data_text) if t
                    )
            elif named_docs:
                update["documents"] = named_docs
            return rendered.model_copy(update=update)
        except Exception as exc:
            raise ValueError(f"BRT-ORC-008: skill render failed: {exc}") from exc

    # state_op node
    if node_type == "state_op":
        dep_input = {
            "op": node.get("op", "count"),
            "params": node.get("params", {}),
        }
        source = _state_op_source(node)
        if source:
            dep_input["source_ref"] = source
        # R4 — a step named as the source supplies its list; items written in
        # the node win. A source that does not resolve to a list leaves no
        # items, so the operator fails loudly (BRT-SOP-003); a resolved empty
        # list is valid (a filter that matched nothing, then count -> 0).
        items = node.get("items")
        if not items and source:
            items = _upstream_list(source, results)
        if items is not None:
            dep_input["items"] = items
        elif source and (groups := _upstream_groups(source, results)) is not None:
            # Ruling 25d — a group_by step without count gives groups of
            # rows; sample takes n from each, the other ops read the rows.
            dep_input["groups"] = groups
        elif not source:
            dep_input["items"] = []

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
            **lane_fields,
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
        ref_failure: Optional[str] = None
        unresolved: Optional[str] = None
        if lane is not None and (named_docs or inputs_ref_raw == _DOCUMENTS_REF):
            # On-premise lane: one item per document part, by id. Handled
            # here explicitly: "documents" is never a step id while the lane
            # is on.
            resolved_items = named_docs or _all_parts()
            named_docs = resolved_items
        elif items_from_node:
            pass
        elif isinstance(inputs_ref_raw, list):
            # Ruling 1a review — inputs_ref given as a list is the items.
            resolved_items = inputs_ref_raw
            if not inputs_ref_raw:
                ref_failure = empty_ref_list_message("inputs_ref")
        elif isinstance(inputs_ref_raw, str) and inputs_ref_raw:
            if _failed_step(inputs_ref_raw, results):
                ref_failure = failed_ref_message(inputs_ref_raw)
            elif inputs_ref_raw in results:
                upstream = _upstream_list(inputs_ref_raw, results)
                if upstream is not None:
                    resolved_items = upstream
            else:
                # Ruling 1a — the ref names no step of this run (the ready
                # check has already waited for any step of this plan it
                # names). The expander fails it with BRT-APE-006.
                unresolved = inputs_ref_raw
        elif inputs_ref_raw not in (None, ""):
            ref_failure = bad_ref_type_message("inputs_ref", inputs_ref_raw)

        dep_input = {
            "inputs_ref": inputs_ref_raw,
            "items": resolved_items,
            "concurrency_limit": node.get("concurrency_limit", 20),
        }
        if ref_failure:
            dep_input["ref_failure"] = ref_failure
        if unresolved:
            dep_input["unresolved_ref"] = unresolved
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
            **lane_fields,
            **({"documents": named_docs} if named_docs else {}),
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
            block = "\n\nUpstream stage outputs (from depends_on):\n" + prompt_json(
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
        **lane_fields,
    )
    if named_docs:
        task_kwargs["documents"] = named_docs
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
    route_options: Optional[dict] = None,
    lane: Optional[LaneContext] = None,
    documents_step: bool = False,
) -> str:
    """Main orchestration loop. Returns the final response text.

    ``documents_step`` is True for a child run started by a step that reads
    the run's documents (an orchestrate step given documents, all_local):
    the child then answers to ruling 26b like a top-level run with documents.

    ``route_options`` carries a chat alias (``borg_route:light`` and so on)
    into step routing for this run and the child runs it starts.

    ``lane`` is the on-premise lane state of a top-level run (from
    ``src.lane.context.prepare_lane``); child runs inherit it. None (the
    lane off) leaves every path exactly as in Phase 1.

    ``root_instruction`` is the operator's verbatim instruction for the
    whole job. A child run is given it so every call it makes stays governed
    by what the operator asked (Law 1) while it plans ``user_instruction``,
    its own sub-task. A top-level run leaves it None.

    Raises:
        ValueError: BRT-ORC-001..009 on fatal failures.
    """
    # Set for this run only and reset on the way out, so the next run in the
    # same task never sees this run's alias. A child run (route_options=None)
    # inherits its parent's value untouched.
    token = _route_options.set(route_options) if route_options is not None else None
    try:
        if lane is not None:
            from src.lane.context import lane_scope  # noqa: PLC0415

            with lane_scope(lane):
                return await _orchestrate_run(
                    run_id, user_instruction, thread_window, rolling_summary, db_path,
                    current_depth, root_instruction, documents_step,
                )
        return await _orchestrate_run(
            run_id, user_instruction, thread_window, rolling_summary, db_path,
            current_depth, root_instruction, documents_step,
        )
    finally:
        if token is not None:
            _route_options.reset(token)


async def _orchestrate_run(
    run_id: UUID,
    user_instruction: str,
    thread_window: Optional[list[dict]],
    rolling_summary: str,
    db_path: str,
    current_depth: int,
    root_instruction: Optional[str],
    documents_step: bool = False,
) -> str:
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
    # On-premise lane: in all_local the planner, compose, the default model
    # and every step are local models only (design note 6.1).
    lane = current_lane()
    all_local = lane is not None and lane.all_local
    has_documents = lane is not None and lane.has_documents
    local_filter = lane.is_local if lane is not None else None
    orch_model = await _select_orchestrator_model(
        "reasoning",
        db_path=db_path,
        quality_requirement=quality_req,
        time_cost_per_sec=time_cost,
        run_id=run_id,
        **({"local_filter": local_filter} if all_local else {}),
    )

    skill_lib = _get_skill_lib()
    skills_catalogue = skill_lib.catalogue_for_prompt()

    matrix = _get_matrix()
    # One model per line under the prompt's "Available models:" label.
    available_models_brief = "\n" + _available_models_brief(
        local_view(matrix, local_filter) if all_local else matrix
    )

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
    # A run with documents never takes the shortcut: an answer about
    # documents must come from steps that read them (design note 5.5).
    if shortcut_enabled and not has_documents and _is_trivial_conversational(user_instruction):
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
            await _mark_answer_kind(run_id, "shortcut", db_path)
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

    plan = plan_result.get("plan")
    if not isinstance(plan, dict):
        plan = None
    else:
        # R3: a non-object node is dropped (BRT-ORC-027); a plan left with
        # none takes the no-plan path below, never runs empty.
        plan = {**plan, "nodes": await _object_nodes(plan.get("nodes"), "plan", run_id, db_path)}
    if has_documents and (plan_result.get("direct_answer") or not plan or not plan.get("nodes")):
        # Design note 5.5: no path skips the plan when documents are present.
        raise ValueError(
            "BRT-ORC-003: the planner answered without a plan, but this run has "
            "documents; an answer about documents must come from steps that read them"
        )

    if plan_result.get("direct_answer"):
        await _mark_answer_kind(run_id, "direct", db_path)
        return plan_result["direct_answer"]

    if not plan or not plan.get("nodes"):
        # Graceful fallback to compose. An action-shaped control signal
        # from the model no longer ends in a vague "I was unable" string:
        # this uses the shared compose-result decoder so action=fail surfaces
        # the rationale and action=compose lifts from stage_results.
        compose_result = await _invoke_orchestrator_model(
            mode="compose",
            stage_results={},
            child_scratch_digest={},
            **base_kwargs,
        )
        async def _reask_empty() -> dict:
            return await _invoke_orchestrator_model(
                mode="compose",
                stage_results={},
                child_scratch_digest={},
                user_message=_COMPOSE_REASK,
                **base_kwargs,
            )

        try:
            return await _handle_compose_result(
                compose_result=compose_result,
                results={},
                run_id=run_id,
                db_path=db_path,
                reask=_reask_empty,
            )
        except ValueError:
            # _handle_compose_result raises BRT-ORC-009 when nothing is
            # usable. In the empty-plan branch we still want a graceful
            # user-facing string rather than a 500, so preserve the
            # historical default here. No step ran and nothing was written
            # up: the run declined to answer (answer_kind "declined").
            await _mark_answer_kind(run_id, "declined", db_path)
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
            matrix=local_view(matrix, local_filter) if all_local else matrix,
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
    # Raw steps whose output did not pass the output guard: a step that reads
    # one of them is raw too (taint follows the data, design note 5.2).
    raw_unstamped: set[str] = set()

    # Every step id this run has used: a generated id never repeats one.
    run_ids: set = set(results)
    current_nodes = _with_ids(current_nodes, run_ids)
    # "documents" names the run's documents only while the lane is on; with
    # the lane off it is an ordinary step id and is waited for like any other.
    documents_reserved = lane is not None
    # True once a wave ran (or a step was failed) since the last re_plan.
    ran_since_replan = False

    while loop_count < MAX_LOOPS:
        # Within one plan, waves run until no step is ready; only then is
        # re_plan called (review of 158c424, finding 1): a step held back by
        # a ref or depends_on is never dropped. BRT-PR-01a — a ref naming no
        # step of the plan is IGNORED by the ready check (it would block the
        # step forever); a depends_on mismatch is logged as BRT-ORC-017 when
        # the task is built, an unresolved item ref fails with BRT-APE-006.
        completed_ids = set(results.keys())
        waiting = [n for n in current_nodes if n.get("id") not in completed_ids]
        waiting_ids = {n.get("id") for n in waiting}

        def _waits_on(n: dict) -> list[str]:
            # Planner ruling 1a — an inputs_ref, a skill's *_ref arg or a
            # state_op source naming a step of this plan is a dependency too.
            # A step naming itself is not held back (it would wait forever);
            # its ref resolves to nothing and the step fails (BRT-APE-006).
            return [
                r for r in _node_refs(n, documents_reserved)
                if r != n.get("id") and r in waiting_ids
            ]

        ready = [n for n in waiting if not _waits_on(n)]

        if waiting and not ready:
            # Every step left waits on another step left: a cycle. Its
            # members can never run, so they fail loudly now (BRT-ORC-024);
            # a step that depends_on a member then fails with BRT-ORC-025.
            await _fail_ref_cycles(
                waiting, {n["id"]: _waits_on(n) for n in waiting},
                results, raw_unstamped, run_id, lane, db_path,
            )
            ran_since_replan = True
            continue

        if not ready:
            if not ran_since_replan:
                # re_plan sent nothing new to run (e.g. only steps that
                # already ran): compose.
                break
            ran_since_replan = False
            loop_count += 1

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
                # R3: non-object entries are dropped (BRT-ORC-027); nothing
                # valid left means no next steps, so the run composes.
                next_nodes = await _object_nodes(
                    replan_result.get("next_nodes"), "re_plan", run_id, db_path
                )
                if not next_nodes:
                    break
                # Nothing is held at this point (every step either ran or
                # failed), so the next wave is re_plan's steps.
                run_ids.update(results)
                # R1: a step reusing a finished step's id is renamed (and
                # the batch's refs to it follow), so it runs (BRT-ORC-026).
                next_nodes, renames = _rename_reused_ids(next_nodes, set(results), run_ids)
                if renames:
                    try:
                        await write_event(
                            "error",
                            run_id=run_id,
                            decision={
                                "code": "BRT-ORC-026",
                                "severity": "warning",
                                "msg": "re_plan reused the id of a step that already "
                                       "ran; the new step was renamed",
                                "renamed": renames,
                            },
                            db_path=db_path,
                        )
                    except Exception:
                        pass
                current_nodes = _with_ids(next_nodes, run_ids)
                continue
            elif action == "fail":
                rationale = replan_result.get("rationale", "No rationale provided.")
                raise ValueError(f"BRT-ORC-006: orchestrator failed the run: {rationale}")
            else:
                # Unknown action — compose anyway
                break

        ran_since_replan = True

        # Dispatch ready nodes in parallel
        tasks_to_dispatch = []
        for i, node in enumerate(ready):
            node_id = node.get("id", f"n{i}")
            level = f"{current_depth+1}{chr(ord('a') + i % 26)}"

            # On-premise lane: is this step raw, and must it stay local?
            raw = False
            local_only = False
            node_docs: list[str] = []
            if lane is not None:
                reads_docs = _node_reads_documents(node)
                raw = reads_docs or any(ref in raw_unstamped for ref in _node_refs(node))
                # by_reference: a raw step is marked and handed its documents
                # the same way, but may be routed to any checked model.
                local_only = all_local or (raw and lane.keeps_raw_local)
                if reads_docs and has_documents:
                    try:
                        node_docs = _node_document_ids(node, lane)
                    except ValueError:
                        node_docs = []   # the build below fails it loudly (BRT-ORC-008)

            # A step whose depends_on step failed (cycle-failed included)
            # fails now, before routing or any model call (BRT-ORC-025): a
            # dead chain stops paying. Item refs keep BRT-APE-005.
            dead_dep = _failed_dependency(node, results, documents_reserved)
            if dead_dep is not None:
                message = failed_dependency_message(dead_dep)
                if raw:
                    await _lane_refuse_node(
                        results, raw_unstamped, run_id, node_id, "BRT-ORC-025",
                        message, node_docs, db_path,
                    )
                    continue
                results[node_id] = InboundEnvelope(
                    envelope_id=uuid4(),
                    subtask_id=uuid4(),
                    status="failed",
                    output={},
                    failure_reason=message,
                )
                try:
                    await write_event(
                        "error",
                        run_id=run_id,
                        decision={"code": "BRT-ORC-025", "msg": message,
                                  "node_id": node_id, "failed_step": dead_dep},
                        db_path=db_path,
                    )
                except Exception:
                    pass
                continue

            # A state_op whose source step failed has no items: it fails
            # here with BRT-APE-005 instead of running empty (BRT-SOP-003).
            if node.get("type") == "state_op" and not node.get("items"):
                source = _state_op_source(node)
                if _failed_step(source, results):
                    await _fail_step_before_call(
                        results, raw_unstamped if raw else None, run_id, node_id,
                        "BRT-APE-005", failed_ref_message(source), node_docs,
                        db_path, raw,
                    )
                    continue

            if lane is not None:
                if raw and node.get("type") == "orchestrate" and not all_local:
                    # Its child planner would read the content (design note 5.8).
                    await _lane_refuse_node(
                        results, raw_unstamped, run_id, node_id, "BRT-LANE-005",
                        "documents on an orchestrate step are refused outside all_local",
                        node_docs, db_path,
                    )
                    continue

            # Route this step to its own model. A state_op calls no model.
            step_model = default_model
            route_decision: Optional[dict] = None
            if node.get("type", "answerer") != "state_op":
                node_skill = await _skill_for_node(node, run_id, node_id, db_path)
                lane_kwargs: dict = {}
                extra_tokens = 0
                if local_only:
                    lane_kwargs["local_filter"] = local_filter
                if local_only or raw:
                    # The document text the step reads counts toward its size
                    # (by_reference too, where any checked model may serve it).
                    extra_tokens += _doc_tokens(node, node_docs, lane, node_skill)
                # An answerer skill's data (compare_and_rank, verify_claim)
                # goes into its context: its size counts before routing, and
                # a step no checked model can hold fails here, before any
                # call (BRT-REG-020); it is never truncated.
                reads_data, data_tokens = _skill_data_size(node, node_skill, results, lane)
                extra_tokens += data_tokens
                if extra_tokens:
                    lane_kwargs["extra_input_tokens"] = extra_tokens
                route_matrix = matrix
                need: Optional[int] = None
                if reads_data:
                    need = tokens_needed(node, extra_tokens)
                    checked = [m for m in matrix.all() if m.checked]
                    if not any((m.context_length or 0) >= need for m in checked):
                        largest = max((m.context_length or 0 for m in checked), default=0)
                        await _fail_step_before_call(
                            results, raw_unstamped if raw else None, run_id, node_id,
                            "BRT-REG-020", data_too_big_message(need, largest),
                            node_docs, db_path, raw,
                        )
                        continue
                    route_matrix = local_view(
                        matrix, lambda m, need=need: (m.context_length or 0) >= need
                    )
                try:
                    step_model, route_decision = select_model_for_node(
                        node=node,
                        skill=node_skill,
                        matrix=route_matrix,
                        floors=leaf_floors,
                        time_cost_per_sec=time_cost,
                        default_model=default_model,
                        run_options=_route_options.get(),
                        **lane_kwargs,
                    )
                except LaneError as exc:
                    # No local model: the step fails; it never falls back.
                    await _lane_refuse_node(
                        results, raw_unstamped if raw else None, run_id, node_id,
                        exc.code, str(exc), node_docs, db_path,
                    )
                    continue
                except Exception as exc:
                    if local_only:
                        await _lane_refuse_node(
                            results, raw_unstamped if raw else None, run_id, node_id,
                            raw_failure_code(exc, "BRT-LANE-002"), f"routing failed: {exc}",
                            node_docs, db_path,
                        )
                        continue
                    step_model = default_model
                    route_decision = fallback_decision(
                        node=node,
                        skill=node_skill,
                        floors=leaf_floors,
                        default_model=default_model,
                        error=f"routing failed: {exc}",
                        run_options=_route_options.get(),
                    )
                if need is not None and (step_model.context_length or 0) < need:
                    # The run's default model (no fitting model cleared the
                    # floor) cannot hold the data either: fail, never truncate.
                    largest = max((m.context_length or 0 for m in matrix.all() if m.checked),
                                  default=0)
                    await _fail_step_before_call(
                        results, raw_unstamped if raw else None, run_id, node_id,
                        "BRT-REG-020", data_too_big_message(need, largest),
                        node_docs, db_path, raw,
                    )
                    continue

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
                    **({"data_scope": "raw" if raw else "derived", "lane": lane}
                       if lane is not None else {}),
                )
                task.node_id = str(node_id)
                if route_decision is not None:
                    # The built task's type, not the plan node's: a skill
                    # step takes its type from the skill's envelope_type.
                    node_kind = task.type
                    item_count = None
                    if node_kind == "apply_to_each":
                        try:
                            item_count = len(_resolve_items(task))
                        except Exception:
                            item_count = None
                    await _log_route(
                        run_id, node_id, task.envelope_id, route_decision, db_path,
                        node_type=node_kind, item_count=item_count,
                        demoted=task.current_depth >= task.max_depth,
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
                if raw:
                    await _lane_refuse_node(
                        results, raw_unstamped, run_id, node_id, raw_failure_code(exc, "BRT-ORC-008"),
                        f"node build failed: {exc}", node_docs, db_path,
                    )
                    continue
                # Never silent: the failure is logged with its node id, and
                # re_plan sees the reason (demo run 19f56430).
                await _fail_step_before_call(
                    results, None, run_id, node_id, "BRT-ORC-008",
                    str(exc), node_docs, db_path, False,
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
            t_start = time.monotonic()
            try:
                env = await execute(task, db_path=db_path)
            except Exception as exc:
                if task.data_scope == "raw" and raw_failures_hidden():
                    # Upward a raw failure is a code plus document ids; the
                    # full error stays in the local trace (design note 5.4).
                    # by_reference: shown by the non-raw rules below.
                    from src.lane.context import failed_envelope  # noqa: PLC0415

                    code = raw_failure_code(exc, "BRT-ORC-004")
                    try:
                        await write_event(
                            "error",
                            run_id=run_id,
                            envelope_id=task.envelope_id,
                            decision={"code": code, "node_id": node_id,
                                      "msg": "raw step failed", "error": str(exc)[:2000]},
                            db_path=db_path,
                        )
                    except Exception:
                        pass
                    env = failed_envelope(task, code)
                elif task.data_scope == "raw" and isinstance(exc, (LaneError, RawStepError)):
                    # by_reference: a failure raised while a raw step ran
                    # reaches plan/re_plan as its own code + the document
                    # ids + the first 200 characters (not BRT-ORC-004).
                    env = raw_step_failed_envelope(task, exc, "BRT-ORC-004")
                else:
                    env = InboundEnvelope(
                        envelope_id=task.envelope_id,
                        subtask_id=task.subtask_id,
                        status="failed",
                        output={},
                        failure_reason=f"BRT-ORC-004: {exc}",
                    )
            # Latency is the step's own run time: stop the clock before any
            # pricing or database work.
            elapsed_ms = int((time.monotonic() - t_start) * 1000)
            try:
                # Work order 1.6 — token counts and dollars on the envelope row.
                tok_in = env.tokens_used.get("input") if env.tokens_used else None
                tok_out = env.tokens_used.get("output") if env.tokens_used else None
                env_dollars = env_baseline = None
                if env.dollars is not None:
                    # Set by the node itself: the serving model's price for an
                    # answerer, the sum over everything spawned for an
                    # orchestrate or apply_to_each node.
                    env_dollars, env_baseline = env.dollars, env.baseline_dollars
                elif tok_in is not None or tok_out is not None:
                    env_dollars, env_baseline = await price_with_baseline(
                        _get_matrix().get(task.model_name or ""),
                        tok_in, tok_out, db_path,
                    )
                await update_envelope_inbound(
                    task.envelope_id,
                    env,
                    tokens_input=tok_in,
                    tokens_output=tok_out,
                    dollars=env_dollars,
                    latency_ms=elapsed_ms,
                    baseline_dollars=env_baseline,
                    db_path=db_path,
                )
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

            return node_id, env, task

        dispatched = await asyncio.gather(
            *[_dispatch_one(nid, n, t) for nid, n, t in tasks_to_dispatch]
        )
        for nid, env, done_task in dispatched:
            results[nid] = env
            env._data_scope = done_task.data_scope
            env._node_type = done_task.type
            env._doc_label = doc_label(list(done_task.documents))
            if done_task.data_scope == "raw" and not env.lane_guarded:
                raw_unstamped.add(nid)
        # Back to the top: the next wave, or re_plan when no step is ready.

    if loop_count >= MAX_LOOPS:
        await write_event(
            "error",
            run_id=run_id,
            decision={"error": "BRT-ORC-005: max loop count exceeded"},
            db_path=db_path,
        )

    # Planner ruling 26b: with documents, an answer no document step stands
    # behind must not exist. The run fails before compose is called. A
    # child run inherits the lane but holds no documents of its own: it is
    # checked only when the step that started it reads documents (ruling
    # 26b edge), so its planner's text never stands in for a failed step.
    if current_depth == 0 or documents_step:
        await _fail_if_no_document_step(results, run_id, db_path)

    # --- COMPOSE MODE ---
    # Planner ruling 21: the list-holding steps compose may name with a row
    # marker. With none, every compose request is byte-identical to before.
    rows = _RowContext.for_run(results, current_lane(), user_instruction, governance_instruction)
    try:
        compose_result = await _invoke_orchestrator_model(
            mode="compose",
            stage_results=results,
            child_scratch_digest=child_digests,
            **rows.call_kwargs(),
            **base_kwargs,
        )
    except LaneError:
        raise
    except ValueError as exc:
        # The compose call itself failed (BRT-ORC-001/002, a transport
        # error): finished work is salvaged, never thrown away (ruling 20;
        # the lead's overrule in the R1-R4 review). Nothing finished: the
        # run fails with the call's error, as before.
        salvaged = _salvage_finished_results(results, current_lane())
        if not salvaged:
            raise
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-ORC-030",
                    "severity": "warning",
                    "msg": "the compose call failed; the answer is the finished "
                           "steps' results with a note",
                    "err": str(exc)[:300],
                    "steps": [k for k, v in results.items()
                              if getattr(v, "status", None) == "resolved"],
                },
                db_path=db_path,
            )
        except Exception:
            pass
        await _mark_answer_kind(run_id, "salvaged", db_path)
        return salvaged

    async def _reask(message: str = _COMPOSE_REASK) -> dict:
        return await _invoke_orchestrator_model(
            mode="compose",
            stage_results=results,
            child_scratch_digest=child_digests,
            user_message=message,
            **rows.call_kwargs(),
            **base_kwargs,
        )

    async def _reask_invalid() -> dict:
        from src.orchestrator.row_markers import INVALID_REASK  # noqa: PLC0415

        return await _reask(INVALID_REASK)

    return await _handle_compose_result(
        compose_result=compose_result,
        results=results,
        run_id=run_id,
        db_path=db_path,
        reask=_reask,
        rows=rows if rows.steps else None,
        reask_invalid=_reask_invalid if rows.steps else None,
    )


class _RowContext:
    """The run's list-holding steps for compose row markers (planner ruling
    21; docs/design/compose_row_markers.md)."""

    def __init__(self, steps: dict, quoted: list, lane: Optional[LaneContext]) -> None:
        self.steps = steps
        self.quoted = quoted
        self.lane = lane

    @classmethod
    def for_run(cls, results: dict, lane: Optional[LaneContext],
                user_instruction: str, governance_instruction: Optional[str]) -> "_RowContext":
        from src.orchestrator import row_markers  # noqa: PLC0415

        steps = row_markers.list_steps(results, lane)
        quoted: set = set()
        if steps:
            # The hits the operator or a step wrote (decoded string leaves,
            # never the JSON dump): a reply that repeats one is quoting data.
            texts = [user_instruction or "", governance_instruction or ""]
            for env in results.values():
                texts.extend(_string_leaves(getattr(env, "output", None)))
            quoted = row_markers.quoted_hits(texts)
        return cls(steps, quoted, lane)

    def call_kwargs(self) -> dict:
        from src.orchestrator import row_markers  # noqa: PLC0415

        if not self.steps:
            return {}
        return {
            "extra_user_lines": row_markers.block(self.steps),
            "pointer_lines": row_markers.step_lines(self.steps),
        }

    def guard(self):
        """The rows-block guard (ruling 21b), or None when it does not run."""
        lane = self.lane
        if lane is None or not lane.keeps_raw_local or not lane.has_documents:
            return None
        from src.lane.context import guard_rows_block  # noqa: PLC0415

        return lambda rows, block: guard_rows_block(rows, block, lane)


# Reply fields read as the final answer when compose put it somewhere other
# than "response" (R2 step 1). "rationale" is never an answer.
_ANSWER_FIELDS = (
    "response", "direct_answer", "final_answer", "final_response", "answer",
    "final", "text", "content", "output", "result", "message",
)
# The only answer fields of a reply that also carries ``action`` or
# ``next_nodes`` (a control reply): there "message", "output" and the like
# are the planner's notes, never the answer (R1-R4 review, R2).
_CONTROL_ANSWER_FIELDS = ("response", "direct_answer", "final_answer", "answer")


def _compose_text(compose_result: object) -> tuple[str, Optional[str], bool]:
    """(final text, field it came from, wrong shape) from a compose reply.

    The text is the first non-empty answer field (``_ANSWER_FIELDS``); a
    list or object there is written out as indented JSON. The shape is
    right only for a string ``response`` in a reply with no ``action``.
    When ``action`` or ``next_nodes`` is present, only
    ``_CONTROL_ANSWER_FIELDS`` count. ("", None, False) when the reply
    carries no final text.
    """
    if not isinstance(compose_result, dict):
        return "", None, False
    control = "action" in compose_result or "next_nodes" in compose_result
    for field in (_CONTROL_ANSWER_FIELDS if control else _ANSWER_FIELDS):
        value = compose_result.get(field)
        if isinstance(value, str) and value.strip():
            text = value
        elif isinstance(value, (list, dict)) and value:
            text = json.dumps(value, indent=2, ensure_ascii=False)
        else:
            continue
        wrong = (
            field != "response" or not isinstance(value, str) or control
        )
        return text, field, wrong
    return "", None, False


async def _mark_answer_kind(run_id: UUID, kind: str, db_path: str) -> None:
    """runs.answer_kind for this run; a database hiccup never fails the run."""
    try:
        from src.db.state import set_run_answer_kind  # noqa: PLC0415

        await set_run_answer_kind(run_id, kind, db_path=db_path)
    except Exception:
        logger.warning("could not record answer_kind=%s for run %s", kind, run_id)


async def _handle_compose_result(
    compose_result: dict,
    results: dict,
    run_id: UUID,
    db_path: str,
    reask=None,
    rows: Optional["_RowContext"] = None,
    reask_invalid=None,
) -> str:
    """The run's final answer from a compose reply (planner ruling
    20, R2). In order:

    1. A final text in the reply is used: ``response``, or (wrong shape,
       logged BRT-ORC-028, no second call) ``direct_answer`` or another
       answer field, a non-string response written out as JSON, or a
       response next to an ``action``.
    2. ``{"action": "fail"}`` returns its rationale and marks the
       run ``answer_kind = "declined"``.
    3. Otherwise compose is asked once more (``reask``, logged BRT-ORC-029)
       and its reply goes through 1 and 2.
    4. If that fails too, the finished steps' results are returned with a
       plain note that the write-up failed (BRT-ORC-030, warning) and the
       run is marked ``answer_kind = "salvaged"``; finished work is never
       thrown away. Raw step output is kept out of it in raw_local and
       all_local (``_salvage_finished_results``).
    5. Nothing finished: BRT-ORC-009.

    A composed answer marks the run ``answer_kind = "composed"``.

    Row markers (planner ruling 21; only when ``rows`` holds list steps, so
    the block was sent): every marker in the chosen answer (or a declined
    rationale) must name a list step, else compose is asked once more
    (``reask_invalid``, BRT-ORC-032) and a second invalid reply is salvaged
    (BRT-ORC-032 warning). Valid markers are replaced by the stored rows;
    each placed block passes the lane's rows guard (BRT-LANE-009) first.
    """
    text, field, wrong = _compose_text(compose_result)
    action = str(compose_result.get("action") or "").lower().strip() if isinstance(
        compose_result, dict) else ""
    if rows is not None and (text or action == "fail"):
        candidate = text if text else str(
            compose_result.get("rationale") or compose_result.get("msg") or "")
        from src.orchestrator import row_markers  # noqa: PLC0415

        # Checked on the decoded answer (an object's keys and leaves, never
        # its JSON dump): every hit ``place`` would not replace, a key
        # included, takes the BRT-ORC-032 path, so none is left.
        value = compose_result[field] if text else candidate
        if row_markers.invalid_hits(value, rows.steps, rows.quoted):
            if reask_invalid is not None:
                await _log_decision(run_id, db_path, {
                    "code": "BRT-ORC-032",
                    "msg": "compose used a list marker that names no list step; "
                           "asking compose once more",
                    "reask": True,
                })
                try:
                    second = await reask_invalid()
                except LaneError:
                    raise
                except ValueError as exc:
                    logger.warning("compose marker re-ask failed for run %s: %s", run_id, exc)
                    second = None
                if second is not None:
                    return await _handle_compose_result(
                        second, results, run_id, db_path, reask=reask, rows=rows)
            return await _salvage_or_fail(
                results, run_id, db_path, "BRT-ORC-032",
                "compose used a list marker that names no list step, also when asked "
                "again; the answer is the finished steps' results with a note",
                action,
            )
        if text:
            placed_value, placed = row_markers.place(
                compose_result[field], rows.steps, guard=rows.guard())
            if placed:
                text, _, _ = _compose_text({**compose_result, field: placed_value})
        else:
            placed_value, placed = row_markers.place(candidate, rows.steps, guard=rows.guard())
            if placed:
                compose_result = {**compose_result, "rationale": placed_value}
        if placed:
            await _log_decision(run_id, db_path, {
                "msg": "rows placed by code (row markers)",
                "rows_placed": {sid: len(rows.steps[sid].rows) for sid in placed},
                "markers": placed,
            })
    if text:
        if wrong:
            try:
                await write_event(
                    "orchestrator_decision",
                    run_id=run_id,
                    decision={
                        "code": "BRT-ORC-028",
                        "msg": "compose reply in the wrong shape; its final text was used",
                        "field": field,
                        "compose_action": compose_result.get("action"),
                    },
                    db_path=db_path,
                )
            except Exception:
                pass
        if _no_step_finished(results):
            # Planner ruling 26a: every planned step failed; the planning
            # model wrote this answer alone, and the run says so.
            await _mark_planner_only(results, run_id, db_path)
        else:
            await _mark_answer_kind(run_id, "composed", db_path)
        return text

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
                    "code": "BRT-ORC-031",
                    "msg": "compose returned action=fail; surfacing rationale to user",
                    "rationale": rationale,
                },
                db_path=db_path,
            )
        except Exception:
            pass
        await _mark_answer_kind(run_id, "declined", db_path)
        return rationale

    if reask is not None:
        try:
            await write_event(
                "orchestrator_decision",
                run_id=run_id,
                decision={
                    "code": "BRT-ORC-029",
                    "msg": "compose reply had no final answer; asking compose once more",
                    "compose_action": action or "(missing)",
                },
                db_path=db_path,
            )
        except Exception:
            pass
        try:
            second = await reask()
        except LaneError:
            raise
        except ValueError as exc:
            # The re-ask itself failed (BRT-ORC-001/002): salvage below.
            logger.warning("compose re-ask failed for run %s: %s", run_id, exc)
            second = None
        if second is not None:
            # No reask this time: a second reply with no final text salvages.
            return await _handle_compose_result(
                second, results, run_id, db_path, rows=rows, reask_invalid=reask_invalid)

    salvaged = _salvage_finished_results(results, current_lane())
    if salvaged:
        try:
            await write_event(
                "error",
                run_id=run_id,
                decision={
                    "code": "BRT-ORC-030",
                    "severity": "warning",
                    "msg": "compose returned no final answer, also when asked again; "
                           "the answer is the finished steps' results with a note",
                    "compose_action": action or "(missing)",
                    "steps": [k for k, v in results.items()
                              if getattr(v, "status", None) == "resolved"],
                },
                db_path=db_path,
            )
        except Exception:
            pass
        await _mark_answer_kind(run_id, "salvaged", db_path)
        return salvaged

    # Genuinely nothing usable anywhere — keep the original error so
    # operators still see the failure mode when it's real.
    raise ValueError("BRT-ORC-009: compose mode returned no response")


def _all_items_failed(env) -> bool:
    """An apply_to_each aggregate with items and no successful one."""
    out = getattr(env, "output", None)
    return (
        isinstance(out, dict) and isinstance(out.get("results"), list)
        and ("success_count" in out or "failures" in out)
        and not out["results"] and bool(out.get("failures"))
    )


def _step_finished(env) -> bool:
    """Planner ruling 26f: resolved, and not an apply_to_each whose every
    item failed (one successful item is enough)."""
    return getattr(env, "status", None) == "resolved" and not _all_items_failed(env)


def _no_step_finished(results: dict) -> bool:
    """True when steps were planned and none of them finished (``_step_finished``)."""
    return bool(results) and not any(_step_finished(env) for env in results.values())


def _step_failure_codes(results: dict) -> list:
    """The failure code of every failed step, in run order, as compose sees it.

    A raw step (it read the run's documents) is ``{"code", "doc"}``: the
    code and its document ids only, never a message. Any other step is its
    code as a string.
    """
    codes: list = []
    for env in (results or {}).values():
        if env is not None and _all_items_failed(env):
            # 26f: every item failed; its item codes, once each.
            from src.orchestrator.row_markers import _failure_code  # noqa: PLC0415

            model_reasons = getattr(env, "_model_reason_indexes", None) or set()
            seen: list = []
            for entry in env.output.get("failures") or []:
                if isinstance(entry, dict):
                    code = _failure_code(entry, entry.get("index") in model_reasons)
                    if code not in seen:
                        seen.append(code)
            for code in seen or [_FAILURE_CODE_FALLBACK]:
                if getattr(env, "_data_scope", None) == "raw":
                    codes.append({"code": code, "doc": getattr(env, "_doc_label", None)})
                else:
                    codes.append(code)
            continue
        if env is None or getattr(env, "status", None) != "failed":
            continue
        view = _stage_output_for_prompt(env, "compose")
        failure = view.get("failure") if isinstance(view, dict) else None
        code = failure.get("code") if isinstance(failure, dict) else None
        code = str(code or _FAILURE_CODE_FALLBACK)
        if getattr(env, "_data_scope", None) == "raw":
            doc = failure.get("doc") if isinstance(failure, dict) else None
            codes.append({"code": code, "doc": doc or getattr(env, "_doc_label", None)})
        else:
            codes.append(code)
    return codes


def _codes_text(codes: list) -> str:
    parts = []
    for c in codes:
        if isinstance(c, dict):
            parts.append(f"{c['code']} (document {c['doc']})" if c.get("doc") else c["code"])
        else:
            parts.append(str(c))
    return ", ".join(parts) if parts else "none"


async def _mark_planner_only(results: dict, run_id: UUID, db_path: str) -> None:
    """Ruling 26a: ``answer_kind = "planner_only"`` and a BRT-ORC-033 warning
    event carrying the failed-step count and their codes (the receipt reads it)."""
    codes = _step_failure_codes(results)
    failed = sum(1 for env in results.values() if not _step_finished(env))
    try:
        await write_event(
            "error",
            run_id=run_id,
            decision={
                "code": "BRT-ORC-033",
                "severity": "warning",
                "msg": "every planned step failed; the planning model wrote this answer alone",
                "failed_steps": failed,
                "step_codes": codes,
            },
            db_path=db_path,
        )
    except Exception:
        pass
    await _mark_answer_kind(run_id, "planner_only", db_path)


async def _fail_if_no_document_step(results: dict, run_id: UUID, db_path: str) -> None:
    """Ruling 26b: a run with documents (any lane mode) in which no step that
    read them finished fails with BRT-ORC-034 before compose is called, so no
    compose text exists. The error lists the steps' failure codes; a raw step
    shows its code and document ids only.

    "Read them" is the runtime's own mark: the step's data scope is raw.

    Raises:
        ValueError: BRT-ORC-034.
    """
    lane = current_lane()
    if lane is None or not lane.has_documents:
        return
    if any(
        _step_finished(env) and getattr(env, "_data_scope", None) == "raw"
        for env in (results or {}).values()
    ):
        return
    codes = _step_failure_codes(results)
    failed = sum(1 for env in (results or {}).values() if not _step_finished(env))
    message = (
        "BRT-ORC-034: the run has documents and no step that read them finished, "
        f"so no answer was written. Step failures: {_codes_text(codes)}"
    )
    try:
        await write_event(
            "error",
            run_id=run_id,
            decision={"code": "BRT-ORC-034", "msg": message,
                      "failed_steps": failed, "step_codes": codes},
            db_path=db_path,
        )
    except Exception:
        pass
    raise ValueError(message)


async def _log_decision(run_id: UUID, db_path: str, decision: dict) -> None:
    """An ``orchestrator_decision`` event; a logging hiccup never fails the run."""
    try:
        await write_event("orchestrator_decision", run_id=run_id, decision=decision,
                          db_path=db_path)
    except Exception:
        pass


async def _salvage_or_fail(results: dict, run_id: UUID, db_path: str, code: str,
                           msg: str, action: str) -> str:
    """The finished steps' results with a note, recorded with ``code`` as a
    warning and ``answer_kind = "salvaged"``; with nothing finished, the run
    fails with ``code``."""
    salvaged = _salvage_finished_results(results, current_lane())
    if not salvaged:
        raise ValueError(f"{code}: {msg}")
    try:
        await write_event(
            "error",
            run_id=run_id,
            decision={
                "code": code,
                "severity": "warning",
                "msg": msg,
                "compose_action": action or "(missing)",
                "steps": [k for k, v in results.items()
                          if getattr(v, "status", None) == "resolved"],
            },
            db_path=db_path,
        )
    except Exception:
        pass
    await _mark_answer_kind(run_id, "salvaged", db_path)
    return salvaged


SALVAGE_NOTE = (
    "The final write-up step failed, so this answer was not composed. "
    "Below are the results of the steps that finished, as they came back."
)


def _salvage_finished_results(results: dict, lane: Optional[LaneContext] = None) -> Optional[str]:
    """The finished steps' results under a plain note, or None when no step
    finished (planner ruling 20, R2 step 4).

    One section per resolved step, in run order: its answer text when the
    output has one (``_step_text``), otherwise its output as indented JSON;
    an apply_to_each step lists each item's output (with the item and its
    index) and says how many items failed.

    Lane rule: in raw_local and all_local a raw step's output appears only
    when it passed the output guard (``lane_guarded``); an apply_to_each
    step's per-item outputs each passed it, but an item is shown only when
    it is a document part id, never text. Anything else from a raw step is
    named as withheld, with no content.
    """
    if not results:
        return None
    hide_raw = lane is not None and lane.keeps_raw_local
    sections: list[str] = []
    withheld: list[str] = []
    for step_id, env in results.items():
        if env is None or getattr(env, "status", None) != "resolved":
            continue
        out = getattr(env, "output", None)
        raw = hide_raw and getattr(env, "_data_scope", None) == "raw"
        aggregate = isinstance(out, dict) and isinstance(out.get("results"), list) and (
            "success_count" in out or "failures" in out
        )
        if raw and not aggregate and env.lane_guarded is not True:
            withheld.append(str(step_id))
            continue
        if aggregate:
            rows = []
            for entry in out["results"]:
                if not isinstance(entry, dict):
                    continue
                row = {"index": entry.get("index")}
                item = entry.get("item")
                if not raw or (lane is not None and lane.is_part_id(item)):
                    row["item"] = item
                row["output"] = entry.get("output")
                rows.append(row)
            failed = len(out.get("failures") or [])
            head = f"## {step_id} ({len(rows)} item(s) finished"
            head += f", {failed} failed)" if failed else ")"
            sections.append(head + "\n" + json.dumps(rows, indent=2, ensure_ascii=False))
            continue
        body = _step_text(out)
        if body is None and out:
            body = json.dumps(out, indent=2, ensure_ascii=False, default=str)
        if body:
            sections.append(f"## {step_id}\n{body}")
    if not sections:
        return None
    text = SALVAGE_NOTE + "\n\n" + "\n\n".join(sections)
    if withheld:
        text += (
            "\n\nNot shown (steps that read your documents whose output did not pass "
            "the on-premise output check): " + ", ".join(withheld)
        )
    return text


def _step_text(out: object) -> Optional[str]:
    """A step output's answer text (``response``, ``answer``, ``text``,
    ``content``, ``summary``; or a bare string), or None."""
    if isinstance(out, str) and out.strip():
        return out.strip()
    if not isinstance(out, dict):
        return None
    for key in ("response", "answer", "text", "content", "summary"):
        val = out.get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
    return None


def _salvage_answer_from_stage_results(results: dict) -> Optional[str]:
    """Helper: the answer text of the last resolved step that has
    one (``_step_text``), or None. The compose path now salvages every
    finished step instead (``_salvage_finished_results``, ruling 20); this
    stays for callers that want the single most recent answer text.
    """
    candidates = [
        text for env in (results or {}).values()
        if env is not None and getattr(env, "status", None) == "resolved"
        for text in [_step_text(getattr(env, "output", None))] if text
    ]
    return candidates[-1] if candidates else None
