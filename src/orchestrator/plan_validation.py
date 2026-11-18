"""Plan validation: issues found in a plan before any step of it has run.

The lane class rule (readings under planner ruling 32 (2)): a plan-validation
message is built from the plan and the skill list only, so it may go to
re_plan in every lane. This module therefore takes only the plan's nodes and
a static skill registry. It imports nothing from the lane, the envelope
store, the database or the engine, and it is never given step results, rows,
run state or document content; tests pin the imports.

Checks are plain functions in ``_CHECKS``; each takes ``(node, skills)`` and
returns a ``PlanIssue`` or None. A node whose type is unknown gets that one
issue and no other check.

Codes (planner ruling 35 reversed ruling 32c: only an unknown type refuses):
- BRT-ORC-035: a skill step's output_schema names fields the skill does not
               return. Not a refusal: the step runs as written and each name
               goes on the run's "asked for but not produced" list (35a).
- BRT-ORC-036: a plan node key, or a skill_args key, that nothing accepts for
               that node type (or skill): not a refusal, the key is left out
               and the step runs (35b). A node type that is not one of the
               four (ruling 32d(iv)) IS a plan error: its message names the
               value first and never cuts it, and ``PlanIssue.node`` carries
               the node as written for the re_plan request.
"""
import json
from typing import Any, NamedTuple, Optional, Protocol

NAME_MAX = 40        # a name taken from the plan is cut at this many characters
MESSAGE_MAX = 200    # the most of a message the types list may fill (never the type value)
NODE_SHOWN_MAX = 600  # the node an unknown type was written on, as re_plan is shown it


class SkillRegistry(Protocol):
    """What validation may ask about skills: field names only."""

    def return_fields(self, skill_id: str, version: Optional[int], args: object) -> Optional[list]:
        ...

    def field_names(self, value: object) -> Optional[list]:
        ...

    def arg_names(self, skill_id: str, version: Optional[int]) -> Optional[list]:
        """The skill's declared required_args names; None when not found."""
        ...


class PlanIssue(NamedTuple):
    node_id: str
    code: str
    message: str            # a plan note's text; for an unknown type, what re_plan is shown
    skill_id: str = ""
    fields: tuple = ()      # the names the plan asked for that cannot be produced
    where: str = ""         # BRT-ORC-036: "node", "skill_args" or "type"
    node: str = ""          # unknown type only: the node as JSON, cut at NODE_SHOWN_MAX


def _cut(text: object, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _check_skill_output_schema(node: dict, skills: SkillRegistry) -> Optional[PlanIssue]:
    skill_id = node.get("skill_id")
    named = node.get("output_schema")
    if not skill_id or not named:
        return None
    names = skills.field_names(named)
    if not names:
        return None
    args = node.get("skill_args")
    returned = skills.return_fields(
        str(skill_id), node.get("skill_version", 1), args if isinstance(args, dict) else {}
    )
    if returned is None:
        return None     # the skill's fields cannot be read: not checked
    unknown = [str(n) for n in names if n and str(n) not in returned]
    if not unknown:
        return None
    # Ruling 35a: a note, never sent to re_plan (names cannot tell a rename
    # from a new field, so the step runs as written).
    asked = _cut(", ".join(_cut(n, NAME_MAX) for n in unknown), 90)
    return PlanIssue(
        node_id=str(node.get("id", "")), code="BRT-ORC-035",
        message=f"BRT-ORC-035: '{_cut(skill_id, NAME_MAX)}' does not return {asked}; "
                "the step ran as written",
        skill_id=str(skill_id), fields=tuple(unknown),
    )


# --- BRT-ORC-036: accepted field names (planner ruling 32d(ii)-(iv)) ----------
# Static data from the 32d survey, checked against the code on 18-Nov-2026.
# Not branch-aware: a node's keys are checked against its type's list, whether
# or not it is a skill step.

# Every node, any type: the ready check (engine._node_refs: depends_on,
# inputs_ref, source, source_ref), the lane (documents), the skill path
# (engine._build_task_from_node: skill_id, skill_version, skill_args,
# trigger_reason), routing (registry/node_router.py: tier, task_type,
# recommended_model), and what every prompt example teaches (instruction,
# context).
COMMON_FIELDS = (
    "id", "type", "depends_on", "inputs_ref", "source", "source_ref", "documents",
    "skill_id", "skill_version", "skill_args", "trigger_reason", "tier", "task_type",
    "recommended_model", "instruction", "context",
)

# Per type, in the order shown to the planner. Sources: engine.py
# _build_task_from_node (the branch for that type) and the node examples in
# src/prompts/orchestrator.py ("The Four Node Types", the web_search examples).
_OWN_FIELDS = {
    # answerer/orchestrate branch reads instruction, context, dependency_input,
    # tool_requests, max_output_tokens, state_query, output_schema,
    # side_effects; the prompt's answerer example teaches the same.
    # ``items``: engine._inline_items reads it on any skill step, and an
    # answerer skill that names its data (compare_and_rank, verify_claim)
    # writes it into the step's context (added to the survey's list).
    "answerer": ("output_schema", "tool_requests", "side_effects", "state_query",
                 "max_output_tokens", "dependency_input", "items"),
    "orchestrate": ("output_schema", "tool_requests", "side_effects", "state_query",
                    "max_output_tokens", "dependency_input"),
    # apply_to_each branch: inputs_ref, items, concurrency_limit, instruction,
    # context, output_schema, side_effects; the prompt's example teaches the
    # same; failed_items_only by ruling 32d(i).
    "apply_to_each": ("items", "output_schema", "concurrency_limit", "side_effects",
                      "failed_items_only"),
    # state_op branch: op, params, items, source / source_ref / inputs_ref
    # (_state_op_source); the prompt's example teaches op, source, params.
    "state_op": ("op", "params", "items"),
}
NODE_TYPES = ("answerer", "apply_to_each", "state_op", "orchestrate")
NODE_FIELDS = {t: tuple(dict.fromkeys(_OWN_FIELDS[t] + COMMON_FIELDS)) for t in NODE_TYPES}

# Shown after a type's own fields in the message, most useful first.
_COMMON_SHOWN = ("instruction", "context", "depends_on", "inputs_ref", "source",
                 "documents", "skill_id", "skill_args", "tier", "task_type",
                 "recommended_model", "trigger_reason", "skill_version", "source_ref")

# Accepted in any skill's skill_args besides its declared required_args
# (ruling 32d(iii)): the R4 extra_fields, inline items, any ``*_ref`` (inline
# items or a step id, engine._inline_items / _node_refs), failed_items_only.
SKILL_ARG_EXTRAS = ("extra_fields", "items", "failed_items_only")

# Change 1 (readings under ruling 32 (3)): accepted fields that build nothing
# on the node's path. A skill step is built from its template (library.render);
# the node's own fields below are never read there, so they build nothing for
# the model. They are not free: the router counts the node-level instruction,
# context and skill_args in its size estimate (node_router.py:142-147), so they
# can change the model chosen or raise BRT-REG-020. state_query is never
# executed on any path. A state_op ignores instruction and context, and the
# routing fields (a state_op makes no model call). ``items`` builds nothing on
# a non-skill answerer or orchestrate step; skill_args and skill_version build
# nothing without a skill_id. A state_op with a skill_id runs the skill branch,
# which never reads op or params. Node-level failed_items_only is read only
# when the skill renders as an apply_to_each.
_SKILL_PATH_UNUSED = ("instruction", "context", "output_schema", "concurrency_limit",
                      "side_effects", "tool_requests", "max_output_tokens",
                      "dependency_input", "state_query")
_PATH_UNUSED = {"answerer": ("state_query", "items"), "orchestrate": ("state_query", "items"),
                "apply_to_each": (),
                "state_op": ("instruction", "context", "tier", "task_type",
                             "recommended_model")}
_NO_SKILL_UNUSED = ("skill_args", "skill_version")


def _names(names: list, room: int) -> str:
    """Names joined with ", ", whole names only, ``...`` when some are left out."""
    out = ""
    for i, name in enumerate(names):
        part = (", " if out else "") + name
        rest = len(names) - i - 1
        if len(out) + len(part) + (5 if rest else 0) > room:
            return out + (", ..." if out else "...")
        out += part
    return out


def _unknown_message(subject: str, unknown: list, label: str, accepted: list) -> str:
    """BRT-ORC-036 plan-note text for a key nothing takes (BRT-SOP-006
    pattern): what does not take which names, then the accepted names; each
    plan-taken name cut at 40, the whole at 200 (the accepted list gives way
    first). Ruling 35b: recorded, not sent to re_plan."""
    asked = _cut(", ".join(_cut(n, NAME_MAX) for n in unknown), 90)
    head = f"BRT-ORC-036: {subject} does not take {asked}. {label}: "
    return head + _names(list(accepted), MESSAGE_MAX - len(head))


def _type(node: dict) -> object:
    """The node's type; none written (or null) is an answerer, as the engine reads it."""
    value = node.get("type")
    return "answerer" if value is None else value


def type_message(node_type: object) -> str:
    """BRT-ORC-036 text for an unknown type (ruling 35b): the offending value
    first and whole, never cut; then the four types, cut by whole names to
    keep the message within MESSAGE_MAX where the value leaves room."""
    head = f"BRT-ORC-036: step type '{node_type}' is not a node type. Types: "
    return head + _names(list(NODE_TYPES), MESSAGE_MAX - len(head))


def node_text(node: dict) -> str:
    """The node as the plan wrote it, as JSON, cut at NODE_SHOWN_MAX (ruling
    35b: the re_plan request shows the planner the node it wrote). Built from
    the plan only."""
    text = json.dumps(node, ensure_ascii=False, default=str)
    return _cut(text, NODE_SHOWN_MAX)


def _check_type(node: dict, skills: SkillRegistry) -> Optional[PlanIssue]:
    node_type = _type(node)
    if node_type in NODE_TYPES:
        return None
    return PlanIssue(
        node_id=str(node.get("id", "")), code="BRT-ORC-036",
        message=type_message(node_type),
        fields=("type",), where="type", node=node_text(node),
    )


def _check_node_keys(node: dict, skills: SkillRegistry) -> Optional[PlanIssue]:
    node_type = _type(node)
    accepted = NODE_FIELDS[node_type]
    if node.get("skill_id"):
        # The engine's skill branch reads failed_items_only on any node with a
        # skill_id, whatever its type (or none).
        accepted = accepted + ("failed_items_only",)
    unknown = [str(k) for k in node if k not in accepted]
    if not unknown:
        return None
    shown = list(_OWN_FIELDS[node_type]) + list(_COMMON_SHOWN)
    return PlanIssue(
        node_id=str(node.get("id", "")), code="BRT-ORC-036",
        message=_unknown_message(f"a {node_type} step", unknown, "Its fields", shown),
        fields=tuple(unknown), where="node",
    )


def _check_skill_args(node: dict, skills: SkillRegistry) -> Optional[PlanIssue]:
    skill_id = node.get("skill_id")
    args = node.get("skill_args")
    if not skill_id or not isinstance(args, dict) or not args:
        return None
    declared = skills.arg_names(str(skill_id), node.get("skill_version", 1))
    if declared is None:
        return None     # unknown skill: the builder fails it (BRT-SKL-003/004)
    accepted = set(declared) | set(SKILL_ARG_EXTRAS)
    unknown = [str(k) for k in args if k not in accepted and not str(k).endswith("_ref")]
    if not unknown:
        return None
    sid = _cut(skill_id, NAME_MAX)
    shown = list(declared) + list(SKILL_ARG_EXTRAS) + ["any *_ref"]
    return PlanIssue(
        node_id=str(node.get("id", "")), code="BRT-ORC-036",
        message=_unknown_message(f"skill '{sid}'", unknown, "Its args", shown),
        skill_id=str(skill_id), fields=tuple(unknown), where="skill_args",
    )


_CHECKS = (_check_node_keys, _check_skill_args, _check_skill_output_schema)


def validate_plan(nodes: Any, skills: SkillRegistry) -> list:
    """Every issue in the plan's nodes, in node order. Non-object entries are
    skipped (the engine drops them, BRT-ORC-027). A node of unknown type has
    that one issue only."""
    issues: list = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        bad_type = _check_type(node, skills)
        if bad_type is not None:
            issues.append(bad_type)
            continue
        for check in _CHECKS:
            issue = check(node, skills)
            if issue is not None:
                issues.append(issue)
    return issues


def accepted_unused(nodes: Any, skills: Any = None) -> list:
    """Change 1 (instrument only): ``(node_id, fields)`` for each node holding
    accepted fields that build nothing on the path it takes; fields sorted.
    Nodes of unknown type are left out (they are refused)."""
    out: list = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        node_type = _type(node)
        if node_type not in NODE_TYPES:
            continue
        if node.get("skill_id") and node_type == "state_op":
            unused = _PATH_UNUSED["state_op"] + ("op", "params")
        elif node.get("skill_id"):
            unused = _SKILL_PATH_UNUSED
            envelope = getattr(skills, "envelope_type", None)
            if envelope is not None and envelope(
                    str(node["skill_id"]), node.get("skill_version", 1)) not in (None, "apply_to_each"):
                unused = unused + ("failed_items_only",)
        else:
            unused = _PATH_UNUSED[node_type] + _NO_SKILL_UNUSED
        fields = tuple(sorted(str(k) for k in node if k in unused))
        if fields:
            out.append((str(node.get("id", "")), fields))
    return out
