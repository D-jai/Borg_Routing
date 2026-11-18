"""Plan validation: errors found in a plan before any step of it has run.

Planner ruling 32c (and the lane class rule): a plan-validation message is
built from the plan and the skill list only, so it may go to re_plan in every
lane. This module therefore takes only the plan's nodes and a static skill
registry. It imports nothing from the lane, the envelope store, the database
or the engine, and it is never given step results, rows, run state or
document content; tests pin the imports.

Checks are plain functions in ``_CHECKS``; each takes ``(node, skills)`` and
returns a ``PlanIssue`` or None. Later checks (unknown node keys, unknown
skill_args keys, unknown type) are added to that tuple.

Error codes:
- BRT-ORC-035: a skill step's output_schema names a field the skill does not
               return
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


class PlanIssue(NamedTuple):
    node_id: str
    code: str
    message: str            # at most MESSAGE_MAX characters
    skill_id: str = ""
    fields: tuple = ()      # the names the plan asked for that cannot be produced


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


_CHECKS = (_check_skill_output_schema,)


def validate_plan(nodes: Any, skills: SkillRegistry) -> list:
    """Every issue in the plan's nodes, in node order. Non-object entries are
    skipped (the engine drops them, BRT-ORC-027)."""
    issues: list = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        for check in _CHECKS:
            issue = check(node, skills)
            if issue is not None:
                issues.append(issue)
    return issues
