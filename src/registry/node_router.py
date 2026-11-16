"""Per-step routing: choose the model for one plan node and say why.

The orchestrator plans; this module decides which model does each step.
It is a pure function over the capability matrix: no I/O, no globals.

Order of decision:
  1. ``recommended_model`` on the node, when it names a checked row that
     has an endpoint (reason ``planner_pick``).
  2. Otherwise the cheapest checked model whose score for the step's kind
     of work clears the floor of the step's tier (reason ``routed``).
     Kind of work: node ``task_type``, else the skill's
     ``recommended_specialty``, else ``reasoning``.
     Tier: node ``tier``, else the skill's ``recommended_tier``, else ``mid``.
  3. When no model clears the floor: the run's default model (reason
     ``fallback_default``). The caller logs BRT-ORC-019.

The returned ``decision`` dict is written to the run trace, so the operator
can read why a step went where it went.
"""
from typing import Any, Optional

from .capability_matrix import CapabilityMatrix
from .cost import call_dollars, effective_cost
from .models import ModelInfo
from .router import select_model

TASK_TYPES = (
    "reasoning", "coding", "summarize", "writing", "roleplay", "agentic", "extraction",
)
TIERS = ("light", "mid", "heavy")
DEFAULT_FLOORS: dict[str, float] = {"light": 6.0, "mid": 7.5, "heavy": 9.0}
DEFAULT_TASK_TYPE = "reasoning"
DEFAULT_TIER = "mid"

# Words a planner or a skill may use that mean one of the seven task types.
_TASK_TYPE_ALIASES = {
    "creative": "writing",
    "summarise": "summarize",
    "summarization": "summarize",
    "summarisation": "summarize",
    "summary": "summarize",
    "code": "coding",
    "extract": "extraction",
}

_EST_OUTPUT_TOKENS = 500
_MIN_EST_INPUT_TOKENS = 200
_MAX_CANDIDATES_RECORDED = 8


def _word(value: Any) -> str:
    return str(value).strip().lower() if value is not None else ""


def _resolve_task_type(node: dict, skill: Optional[dict], notes: list[str]) -> str:
    raw = _word(node.get("task_type")) or _word((skill or {}).get("recommended_specialty"))
    if not raw or raw in ("null", "none"):
        return DEFAULT_TASK_TYPE
    if raw in _TASK_TYPE_ALIASES:
        mapped = _TASK_TYPE_ALIASES[raw]
        notes.append(f"kind of work '{raw}' read as {mapped}")
        raw = mapped
    if raw not in TASK_TYPES:
        notes.append(f"unknown kind of work '{raw}'; treated as {DEFAULT_TASK_TYPE}")
        return DEFAULT_TASK_TYPE
    return raw


def _resolve_tier(node: dict, skill: Optional[dict], notes: list[str]) -> str:
    raw = _word(node.get("tier")) or _word((skill or {}).get("recommended_tier"))
    if not raw or raw in ("null", "none"):
        return DEFAULT_TIER
    if raw not in TIERS:
        notes.append(f"unknown tier '{raw}'; treated as {DEFAULT_TIER}")
        return DEFAULT_TIER
    return raw


def _planner_pick(node: dict, matrix: CapabilityMatrix, notes: list[str]) -> Optional[ModelInfo]:
    named = node.get("recommended_model")
    if not isinstance(named, str) or not named.strip() or _word(named) in ("null", "none"):
        return None
    named = named.strip()
    row = matrix.get(named)
    if row is None:
        # Planners often drop the provider prefix ("x/y" for "openrouter/x/y").
        matches = [m for m in matrix.all() if m.model_id.split("/", 1)[-1] == named]
        row = matches[0] if len(matches) == 1 else None
    if row is None or not row.checked or not row.endpoint:
        notes.append(f"recommended_model '{named}' is not in the pool; routed instead")
        return None
    return row


def _estimate_input_tokens(node: dict) -> int:
    text = f"{node.get('instruction') or ''}{node.get('context') or ''}"
    skill_args = node.get("skill_args")
    if skill_args:  # a skill step's args are its context, so they count
        text += str(skill_args)
    return max(_MIN_EST_INPUT_TOKENS, len(text) // 4)


def select_model_for_node(
    node: dict,
    skill: Optional[dict],
    matrix: CapabilityMatrix,
    floors: dict[str, float],
    time_cost_per_sec: float,
    default_model: ModelInfo,
) -> tuple[ModelInfo, dict]:
    """Return ``(model, decision)`` for one plan node.

    Args:
        node: the plan node as the orchestrator emitted it.
        skill: the skill record when the node invokes one, else None.
        matrix: the capability matrix (only checked rows are routed to).
        floors: minimum score per tier, keys ``light``, ``mid``, ``heavy``.
        time_cost_per_sec: passed through to the cost formula.
        default_model: used when no model clears the floor.

    Never raises for a routing miss: the miss is reported in the decision
    (``reason == "fallback_default"`` with the router's error text).
    """
    notes: list[str] = []
    task_type = _resolve_task_type(node, skill, notes)
    tier = _resolve_tier(node, skill, notes)
    floor = float(floors.get(tier, DEFAULT_FLOORS[tier]))
    est_in = _estimate_input_tokens(node)
    est_out = _EST_OUTPUT_TOKENS

    def cost_of(model: ModelInfo) -> float:
        return effective_cost(model, est_in, est_out, time_cost_per_sec)

    def dollars_of(model: ModelInfo) -> float:
        return call_dollars(model, est_in, est_out)

    def candidates_for() -> list[dict]:
        # Who else was in the running: every checked row that clears the
        # floor and fits the input, cheapest first. est_cost is what the
        # router ranks by (dollars plus time); est_dollars is dollars only.
        offered = matrix.query_by_task(task_type, min_score=floor, min_context_length=est_in)
        offered.sort(key=cost_of)
        return [
            {
                "model_id": m.model_id,
                "score": m.task_scores.get(task_type),
                "est_cost": cost_of(m),
                "est_dollars": dollars_of(m),
            }
            for m in offered[:_MAX_CANDIDATES_RECORDED]
        ]

    decision: dict = {
        "task_type": task_type,
        "tier": tier,
        "floor": floor,
        "est_input_tokens": est_in,
        "est_output_tokens": est_out,
        "candidates": [],
        "notes": notes,
    }

    picked = _planner_pick(node, matrix, notes)
    if picked is not None:
        try:
            decision["candidates"] = candidates_for()  # for information only
        except Exception:
            decision["candidates"] = []
            decision["candidates_reason"] = "candidate list could not be built"
        decision.update(
            reason="planner_pick",
            chosen=picked.model_id,
            est_cost=cost_of(picked),
            est_dollars=dollars_of(picked),
        )
        return picked, decision

    try:
        chosen = select_model(
            matrix=matrix,
            task_type=task_type,
            estimated_input_tokens=est_in,
            estimated_output_tokens=est_out,
            quality_requirement=floor,
            time_cost_per_sec=time_cost_per_sec,
        )
    except ValueError as exc:
        decision.update(
            reason="fallback_default",
            chosen=default_model.model_id,
            est_cost=None,
            est_dollars=dollars_of(default_model),
            candidates_reason="no model cleared the floor, so there is no candidate list",
            error=str(exc),
        )
        return default_model, decision

    decision["candidates"] = candidates_for()
    decision.update(
        reason="routed",
        chosen=chosen.model_id,
        est_cost=cost_of(chosen),
        est_dollars=dollars_of(chosen),
    )
    return chosen, decision


def fallback_decision(
    node: dict,
    skill: Optional[dict],
    floors: dict[str, float],
    default_model: ModelInfo,
    error: str,
) -> dict:
    """The decision for a step whose routing raised unexpectedly.

    Carries every field a routed decision has (reason, task_type, tier,
    floor, chosen, est_cost, candidates) plus ``error``, so the trace reads
    the same whatever went wrong. Never raises: a field that cannot be
    worked out is left at its default.
    """
    notes: list[str] = []
    try:
        task_type = _resolve_task_type(node, skill, notes)
    except Exception:
        task_type = DEFAULT_TASK_TYPE
    try:
        tier = _resolve_tier(node, skill, notes)
    except Exception:
        tier = DEFAULT_TIER
    try:
        floor: Optional[float] = float(floors.get(tier, DEFAULT_FLOORS[tier]))
    except Exception:
        floor = DEFAULT_FLOORS.get(tier)
    # Same token estimate as a routed decision. If it cannot be made, the
    # estimates stay null (never a fake 0.0).
    est_in: Optional[int] = None
    est_dollars: Optional[float] = None
    try:
        est_in = _estimate_input_tokens(node)
        est_dollars = call_dollars(default_model, est_in, _EST_OUTPUT_TOKENS)
    except Exception:
        est_in = None
        est_dollars = None
    return {
        "reason": "fallback_default",
        "task_type": task_type,
        "tier": tier,
        "floor": floor,
        "est_input_tokens": est_in,
        "est_output_tokens": _EST_OUTPUT_TOKENS if est_in is not None else None,
        "chosen": default_model.model_id,
        "est_cost": None,
        "est_dollars": est_dollars,
        "candidates": [],
        "candidates_reason": "routing failed, so there is no candidate list",
        "notes": notes,
        "error": error,
    }
