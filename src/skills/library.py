"""Skill library — load, query, render, and track skills.

The skill library is a catalogue of reusable orchestrator move templates.
Each skill encodes a known-good output_schema and instruction pattern for a
common WBS move. The orchestrator consults the library before crafting a
custom envelope.

Error codes:
- BRT-SKL-001: skills.yaml not found
- BRT-SKL-002: malformed skills.yaml
- BRT-SKL-003: skill_id not found
- BRT-SKL-004: skill version not found
- BRT-SKL-005: render failed — missing required arg
- BRT-SKL-006: render failed — invalid outbound_template type
- BRT-SKL-007: skill_usage_log write failed
- BRT-SKL-008: render failed — a template placeholder no arg can fill
"""
import json
import re
from typing import Optional
from uuid import UUID, uuid4

import aiosqlite
import yaml

from src.db.schema import DB_PATH
from src.envelopes.schema import WrappedTask

SKILLS_PATH = "data/skills.yaml"

VALID_ENVELOPE_TYPES = {"answerer", "apply_to_each", "orchestrate", "state_op"}


class SkillLibrary:
    def __init__(self) -> None:
        self._skills: list[dict] = []

    def load(self, path: str = SKILLS_PATH) -> None:
        """Load skills from YAML. Idempotent — replaces current catalogue.

        Raises:
            RuntimeError: BRT-SKL-001 if file not found.
            RuntimeError: BRT-SKL-002 if YAML is malformed or not a list.
        """
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = yaml.safe_load(f)
        except FileNotFoundError as exc:
            raise RuntimeError(f"BRT-SKL-001: skills file not found: '{path}'") from exc
        except yaml.YAMLError as exc:
            raise RuntimeError(f"BRT-SKL-002: malformed skills YAML: {exc}") from exc

        if not isinstance(data, list):
            raise RuntimeError(
                f"BRT-SKL-002: skills.yaml must be a list, got {type(data).__name__}"
            )
        self._skills = data

    def get(self, skill_id: str, version: Optional[int] = None) -> dict:
        """Return a skill dict by id and optional version.

        If version is None, returns the highest version.

        Raises:
            RuntimeError: BRT-SKL-003 if skill_id not found.
            RuntimeError: BRT-SKL-004 if version not found for that skill_id.
        """
        matching = [s for s in self._skills if s.get("skill_id") == skill_id]
        if not matching:
            raise RuntimeError(f"BRT-SKL-003: skill '{skill_id}' not found in library")

        if version is None:
            return max(matching, key=lambda s: s.get("version", 1))

        versioned = [s for s in matching if s.get("version") == version]
        if not versioned:
            available = [s.get("version") for s in matching]
            raise RuntimeError(
                f"BRT-SKL-004: version {version} not found for skill '{skill_id}'. "
                f"Available: {available}"
            )
        return versioned[0]

    def catalogue_for_prompt(self) -> str:
        """Format the skill library as a compact section for the orchestrator prompt."""
        if not self._skills:
            return "(no skills loaded)"

        lines = ["## Available Skills (check before crafting a custom envelope)\n"]
        for skill in self._skills:
            sid = skill.get("skill_id", "?")
            ver = skill.get("version", 1)
            desc = skill.get("description", "")
            when = skill.get("when_to_use", [])
            not_when = skill.get("not_when", [])
            tier = skill.get("recommended_tier", "?")
            env_type = skill.get("envelope_type", "?")
            req_args = skill.get("required_args", {})

            when_str = "\n".join(f"  - {w}" for w in when)
            not_when_str = "\n".join(f"  - {w}" for w in not_when) if not_when else "  (none listed)"
            args_str = ", ".join(req_args.keys()) if req_args else "(none)"

            lines.append(
                f"### skill: {sid} (v{ver}) — tier: {tier}, type: {env_type}\n"
                f"**What:** {desc}\n"
                f"**When to use:**\n{when_str}\n"
                f"**Not when:**\n{not_when_str}\n"
                f"**Required args:** {args_str}\n"
            )

        return "\n".join(lines)

    def render(
        self,
        skill_id: str,
        version: int,
        args: dict,
        run_id: UUID = None,
        parent_run_id: Optional[UUID] = None,
        current_depth: int = 1,
        max_depth: int = 5,
        level_path: str = "1a",
        trigger_reason: str = "initial_plan",
    ) -> WrappedTask:
        """Substitute args into the skill template and return a WrappedTask.

        Raises:
            RuntimeError: BRT-SKL-003/004 if skill not found.
            RuntimeError: BRT-SKL-005 if a required arg is missing.
            RuntimeError: BRT-SKL-006 if the envelope_type is not valid.
            RuntimeError: BRT-SKL-008 if a placeholder in the instruction,
                output_schema, state_query or inputs_ref cannot be filled.
        """
        skill = self.get(skill_id, version)
        required_args = skill.get("required_args", {})
        for arg_name in required_args:
            if arg_name not in args:
                raise RuntimeError(
                    f"BRT-SKL-005: render failed — required arg '{arg_name}' "
                    f"missing for skill '{skill_id}' v{version}"
                )

        env_type = skill.get("envelope_type", "answerer")
        if env_type not in VALID_ENVELOPE_TYPES:
            raise RuntimeError(
                f"BRT-SKL-006: invalid envelope_type '{env_type}' for skill '{skill_id}'"
            )

        template = skill.get("outbound_template", {})

        # Every placeholder the model will see must be filled: from an arg, or
        # from a derived arg (``fields_formatted`` from ``fields`` ...). One
        # that nothing fills fails here, before any model call (BRT-SKL-008).
        raw_instruction = template.get("instruction", skill.get("description", ""))
        output_schema_raw = template.get("output_schema", {})
        state_query_raw = template.get("state_query")
        inputs_ref_raw = (
            template.get("inputs_ref", args.get("items_ref", ""))
            if env_type == "apply_to_each" else ""
        )
        values = _fill_values(
            skill_id,
            [raw_instruction, output_schema_raw, state_query_raw, inputs_ref_raw],
            args,
        )

        instruction = _safe_substitute(raw_instruction, values)

        # Build output_schema
        if isinstance(output_schema_raw, str):
            whole = _PLACEHOLDER.fullmatch(output_schema_raw.strip())
            if whole and isinstance(values.get(whole.group(1)), dict):
                # "{expected_return_schema}" given as a dict: use the dict.
                output_schema = values[whole.group(1)]
            else:
                # String schema (e.g. "derived from fields") — pass as a note
                output_schema = {"schema_note": _safe_substitute(output_schema_raw, values)}
        else:
            output_schema = _substitute_tree(output_schema_raw, values)

        # Build side_effects
        side_effects = template.get("side_effects", [])
        if isinstance(side_effects, list):
            side_effects = [str(s) for s in side_effects]
        else:
            side_effects = []

        # state_query
        state_query = _safe_substitute(state_query_raw, values) if state_query_raw else None

        # dependency_input — include inputs_ref if apply_to_each
        dependency_input = {}
        if env_type == "apply_to_each":
            dependency_input["inputs_ref"] = _safe_substitute(str(inputs_ref_raw), values)
            cl_raw = template.get("concurrency_limit", args.get("concurrency_limit", 20))
            try:
                dependency_input["concurrency_limit"] = int(str(cl_raw).strip("{} "))
            except (ValueError, TypeError):
                dependency_input["concurrency_limit"] = 20

        if run_id is None:
            run_id = uuid4()

        return WrappedTask(
            envelope_id=uuid4(),
            run_id=run_id,
            parent_run_id=parent_run_id,
            subtask_id=uuid4(),
            type=env_type,
            current_depth=current_depth,
            max_depth=max_depth,
            level_path=level_path,
            instruction=instruction.strip() or skill.get("description", "Execute skill."),
            state_query=state_query,
            side_effects=side_effects,
            dependency_input=dependency_input,
            output_schema=output_schema,
            trigger_reason=trigger_reason,
            skill_id=skill_id,
            skill_version=version,
            skill_args=args,
        )


_PLACEHOLDER = re.compile(r"\{(\w+)\}")

# Wording for an optional arg's ``<arg>_instruction`` line; any other arg
# reads "<Arg words>: <value>". A null or empty arg gives no line at all.
_INSTRUCTION_WORDING = {
    "focus": "Focus on: {value}.",
    "target_format": "Target format: {value}.",
    "max_length": "Keep it to at most {value} words.",
}


def _placeholders(part: object) -> set[str]:
    """Placeholder names in a template part (a string, or a dict/list of them)."""
    if isinstance(part, str):
        return set(_PLACEHOLDER.findall(part))
    if isinstance(part, dict):
        return set().union(*(_placeholders(v) for v in part.values())) if part else set()
    if isinstance(part, list):
        return set().union(*(_placeholders(v) for v in part)) if part else set()
    return set()


def _format_entry(entry: object) -> str:
    """One line for a field or criterion: name (type/weight/values): description."""
    if not isinstance(entry, dict):
        return str(entry)
    name = entry.get("name")
    details = []
    for key, value in entry.items():
        if key in ("name", "description") or value in (None, "", []):
            continue
        if isinstance(value, list):
            value = ", ".join(str(v) for v in value)
        details.append(str(value) if key == "type" else f"{key.replace('_', ' ')} {value}")
    head = str(name) if name else json.dumps({k: v for k, v in entry.items() if k != "description"})
    line = f"{head} ({'; '.join(details)})" if details and name else head
    if entry.get("description"):
        line += f": {entry['description']}"
    return line


def _derive(skill_id: str, name: str, args: dict) -> str:
    """Build a derived arg the template names from the arg it is derived from.

    ``<arg>_formatted`` is ``<arg>`` as a bullet list (one line per entry);
    ``<arg>_instruction`` is one sentence from an optional ``<arg>``, or "".

    Raises:
        RuntimeError: BRT-SKL-008 if no arg supplies it, or its source is empty.
    """
    for suffix in ("_formatted", "_instruction"):
        source = name[: -len(suffix)] if name.endswith(suffix) else None
        if source and source in args:
            value = args[source]
            if suffix == "_instruction":
                if value in (None, "", [], {}):
                    return ""
                if isinstance(value, (list, dict)):
                    value = json.dumps(value)
                wording = _INSTRUCTION_WORDING.get(
                    source, source.replace("_", " ").capitalize() + ": {value}."
                )
                return wording.format(value=value)
            entries = value if isinstance(value, list) else ([value] if value else [])
            if isinstance(value, dict):
                entries = [{"name": k, **(v if isinstance(v, dict) else {"type": v})}
                           for k, v in value.items()]
            if not entries:
                raise RuntimeError(
                    f"BRT-SKL-008: render failed — placeholder '{{{name}}}' in skill "
                    f"'{skill_id}' cannot be filled: arg '{source}' is empty"
                )
            return "\n".join(f"- {_format_entry(e)}" for e in entries)
    raise RuntimeError(
        f"BRT-SKL-008: render failed — placeholder '{{{name}}}' in skill '{skill_id}' "
        f"is not filled by any arg (given: {sorted(args)})"
    )


def _fill_values(skill_id: str, parts: list, args: dict) -> dict:
    """Args plus every derived arg the template parts name; fails on a gap."""
    values = dict(args)
    for name in sorted(set().union(*(_placeholders(p) for p in parts))):
        if name not in values:
            values[name] = _derive(skill_id, name, args)
    return values


def _value_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _safe_substitute(template: str, args: dict) -> str:
    """Replace {key} placeholders in one pass. Unknown keys left as-is.

    One pass, so braces inside a substituted value are never read as a
    placeholder. A null value renders as "".
    """
    return _PLACEHOLDER.sub(
        lambda m: _value_text(args[m.group(1)]) if m.group(1) in args else m.group(0),
        template,
    )


def _substitute_tree(part: object, args: dict) -> object:
    """_safe_substitute over every string in a dict/list template part."""
    if isinstance(part, str):
        return _safe_substitute(part, args)
    if isinstance(part, dict):
        return {k: _substitute_tree(v, args) for k, v in part.items()}
    if isinstance(part, list):
        return [_substitute_tree(v, args) for v in part]
    return part


async def log_usage(
    skill_id: str,
    skill_version: int,
    run_id: UUID,
    envelope_id: UUID,
    outcome: str,
    tokens_used: int = 0,
    latency_ms: int = 0,
    composer_accepted: Optional[bool] = None,
    child_residue_count: int = 0,
    db_path: str = DB_PATH,
) -> None:
    """Log a skill usage event to skill_usage_log.

    Raises:
        RuntimeError: BRT-SKL-007 if the write fails.
    """
    try:
        async with aiosqlite.connect(db_path) as db:
            await db.execute(
                """INSERT INTO skill_usage_log(
                    skill_id, skill_version, run_id, envelope_id, outcome,
                    tokens_used, latency_ms, composer_accepted, child_residue_count
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (
                    skill_id,
                    skill_version,
                    str(run_id),
                    str(envelope_id),
                    outcome,
                    tokens_used,
                    latency_ms,
                    composer_accepted,
                    child_residue_count,
                ),
            )
            await db.commit()
    except Exception as exc:
        raise RuntimeError(f"BRT-SKL-007: skill_usage_log write failed: {exc}") from exc
