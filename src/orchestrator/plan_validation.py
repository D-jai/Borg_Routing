"""Plan validation: errors found in a plan before any step of it has run.

Planner ruling 32c (and the lane class rule): a plan-validation message is
built from the plan and the skill list only, so it may go to re_plan in every
lane. This module therefore takes only the plan's nodes and a static skill
registry. It imports nothing from the lane, the envelope store, the database
or the engine, and it is never given step results, rows, run state or
document content; tests pin the imports.

Checks are plain functions in ``_CHECKS``; each takes ``(node, skills)`` and
returns a ``PlanIssue`` or None. A node whose type is unknown gets that one
issue and no other check.

Error codes:
- BRT-ORC-035: a skill step's output_schema names a field the skill does not
               return
- BRT-ORC-036: a plan node key, or a skill_args key, that nothing accepts for
               that node type (or skill), or a node type that is not one of
               the four (planner ruling 32d(ii)-(iv))
"""
from typing import Any, NamedTuple, Optional, Protocol

NAME_MAX = 40        # a name taken from the plan is cut at this many characters
MESSAGE_MAX = 200    # what re_plan is shown of a failure

HINT_OTHER_FIELDS = "for other fields use apply_to_each with your own output_schema"
HINT_RENAME = "to rename fields, do it when composing"


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
    message: str            # at most MESSAGE_MAX characters
    skill_id: str = ""
    fields: tuple = ()      # the names the plan asked for that cannot be produced
    where: str = ""         # BRT-ORC-036: "node", "skill_args" or "type"


def _cut(text: object, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def skill_schema_message(skill_id: str, returned: list) -> str:
    """BRT-ORC-035 text, three things in order: the skill's field names
    (``item`` and ``error`` always last), the other-fields hint, the rename
    hint. At most 200 characters; the field list is trimmed, never the hints.
    The names the plan asked for travel on the ``PlanIssue``, not in the text."""
    own = [str(n) for n in returned if n not in ("item", "error")]
    tail = f"; {HINT_OTHER_FIELDS}; {HINT_RENAME}"
    always = ", item, error"
    fixed = len("BRT-ORC-035: '' returns: ") + len(tail) + len(always)
    sid = _cut(skill_id, NAME_MAX)
    # The skill's name gives way before the field list does: it needs 16 characters.
    sid = _cut(sid, max(8, len(sid) - max(0, 16 - (MESSAGE_MAX - fixed - len(sid)))))
    room = MESSAGE_MAX - fixed - len(sid)
    have = _cut(", ".join(own), max(8, room)) if own else ""
    fields = (have + always) if have else always[2:]
    return f"BRT-ORC-035: '{sid}' returns: {fields}{tail}"


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
    return PlanIssue(
        node_id=str(node.get("id", "")), code="BRT-ORC-035",
        message=skill_schema_message(str(skill_id), returned),
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
# the node's own fields below are never read there. state_query is never
# executed on any path. A state_op ignores instruction and context.
_SKILL_PATH_UNUSED = ("instruction", "context", "output_schema", "concurrency_limit",
                      "side_effects", "tool_requests", "max_output_tokens",
                      "dependency_input", "state_query")
_PATH_UNUSED = {"answerer": ("state_query",), "orchestrate": ("state_query",),
                "apply_to_each": (), "state_op": ("instruction", "context")}


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
    """BRT-ORC-036 text in the BRT-SOP-006 pattern: what does not take which
    names, then the accepted names; each plan-taken name cut at 40, the whole
    at 200 (the accepted list gives way first)."""
    asked = _cut(", ".join(_cut(n, NAME_MAX) for n in unknown), 90)
    head = f"BRT-ORC-036: {subject} does not take {asked}. {label}: "
    return head + _names(list(accepted), MESSAGE_MAX - len(head))


def _type(node: dict) -> object:
    """The node's type; none written (or null) is an answerer, as the engine reads it."""
    value = node.get("type")
    return "answerer" if value is None else value


def _check_type(node: dict, skills: SkillRegistry) -> Optional[PlanIssue]:
    node_type = _type(node)
    if node_type in NODE_TYPES:
        return None
    shown = _cut(node_type, NAME_MAX)
    return PlanIssue(
        node_id=str(node.get("id", "")), code="BRT-ORC-036",
        message=(f"BRT-ORC-036: step type '{shown}' is not a node type. "
                 f"Types: {', '.join(NODE_TYPES)}"),
        fields=("type",), where="type",
    )


def _check_node_keys(node: dict, skills: SkillRegistry) -> Optional[PlanIssue]:
    node_type = _type(node)
    accepted = NODE_FIELDS[node_type]
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


def accepted_unused(nodes: Any) -> list:
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
        unused = _SKILL_PATH_UNUSED if node.get("skill_id") else _PATH_UNUSED[node_type]
        fields = tuple(sorted(str(k) for k in node if k in unused))
        if fields:
            out.append((str(node.get("id", "")), fields))
    return out
