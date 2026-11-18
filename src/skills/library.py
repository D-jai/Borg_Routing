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
            # An arg whose hint includes "| null" may be left out (treated as null).
            needed = [str(a) for a, h in (req_args or {}).items() if not _is_optional(h)]
            optional = [str(a) for a, h in (req_args or {}).items() if _is_optional(h)]
            args_str = ", ".join(needed) if needed else "(none)"
            if optional:
                args_str += "; optional: " + ", ".join(optional)

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

        The step's own fields are honoured without changing the skill
        (planner ruling 20, R4, narrowed by the R1-R4 review): only an
        explicit ``extra_fields`` arg (a name -> type or description map, a
        list of names or ``{name, type, description}`` entries, a comma
        string, that as a JSON string, or a JSON-Schema object/array whose
        ``properties`` are the fields) adds fields the skill's schema lacks
        to the per-item schema, where the inbound check requires them, and
        lists them in one "Also return" line of the instruction. The skill's
        own keys always win. The plan node's ``output_schema`` is never read
        here: it describes the final answer's rows, not each item. A
        JSON-Schema keyword (type, items, properties, required ...) is never
        a field.

        Raises:
            RuntimeError: BRT-SKL-003/004 if skill not found.
            RuntimeError: BRT-SKL-005 if a required arg is missing.
            RuntimeError: BRT-SKL-006 if the envelope_type is not valid.
            RuntimeError: BRT-SKL-008 if a placeholder in any templated field
                cannot be filled, a schema cannot be built from the args, a
                null is given for an arg whose hint has no "| null", or an
                arg is in a shape its hint cannot take.
        """
        skill = self.get(skill_id, version)
        required_args = skill.get("required_args", {}) or {}
        given_args = args
        args = dict(args or {})
        for arg_name, hint in required_args.items():
            optional = _is_optional(hint)
            if arg_name not in args:
                if optional:
                    # An omitted "| null" arg is null.
                    args[arg_name] = None
                    continue
                raise RuntimeError(
                    f"BRT-SKL-005: render failed — required arg '{arg_name}' "
                    f"missing for skill '{skill_id}' v{version}"
                )
            if args[arg_name] is None:
                if not optional:
                    raise RuntimeError(
                        f"BRT-SKL-008: render failed — arg '{arg_name}' of skill "
                        f"'{skill_id}' is null, but its type ({hint}) does not allow null"
                    )
                continue
            args[arg_name] = _coerce_arg(skill_id, arg_name, hint, args[arg_name])

        env_type = skill.get("envelope_type", "answerer")
        if env_type not in VALID_ENVELOPE_TYPES:
            raise RuntimeError(
                f"BRT-SKL-006: invalid envelope_type '{env_type}' for skill '{skill_id}'"
            )

        template = skill.get("outbound_template", {}) or {}

        # Every placeholder the model will see must be filled: from an arg, or
        # from a derived arg (``fields_formatted`` from ``fields`` ...). One
        # that nothing fills fails here, before any model call (BRT-SKL-008).
        # Every templated field is rendered: instruction, context, state_query,
        # inputs_ref, output_schema, concurrency_limit, trigger_reason.
        raw_instruction = template.get("instruction", skill.get("description", ""))
        output_schema_raw = template.get("output_schema", {})
        state_query_raw = template.get("state_query")
        context_raw = template.get("context")
        trigger_raw = template.get("trigger_reason")
        inputs_ref_raw = (
            template.get("inputs_ref", args.get("items_ref", ""))
            if env_type == "apply_to_each" else ""
        )
        values = _fill_values(
            skill_id,
            [raw_instruction, output_schema_raw, state_query_raw, inputs_ref_raw,
             context_raw, trigger_raw, template.get("concurrency_limit")],
            args,
        )

        instruction = _safe_substitute(raw_instruction, values)

        # Build output_schema: always a real per-item schema, never a note.
        if isinstance(output_schema_raw, str):
            output_schema = _string_schema(skill_id, output_schema_raw, values)
        else:
            output_schema = _substitute_tree(output_schema_raw, values)

        # R4: the step's own extra_fields only (never the skill's template,
        # never the node's output_schema).
        added = _step_fields(skill_id, args.get("extra_fields"), output_schema)
        if added:
            output_schema = _with_fields(output_schema, added)
            instruction = instruction.rstrip() + "\n" + _also_return_line(added)

        # Build side_effects
        side_effects = template.get("side_effects", [])
        if isinstance(side_effects, list):
            side_effects = [str(s) for s in side_effects]
        else:
            side_effects = []

        # state_query (rendered for the record; never executed)
        state_query = _safe_substitute(state_query_raw, values) if state_query_raw else None
        context = _safe_substitute(str(context_raw), values) if context_raw else ""
        if trigger_raw:
            trigger_reason = _safe_substitute(str(trigger_raw), values)

        # dependency_input — include inputs_ref if apply_to_each
        dependency_input = {}
        if env_type == "apply_to_each":
            dependency_input["inputs_ref"] = _safe_substitute(str(inputs_ref_raw), values)
            dependency_input["concurrency_limit"] = _concurrency_limit(
                skill_id, template.get("concurrency_limit"), values
            )

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
            context=context,
            state_query=state_query,
            side_effects=side_effects,
            dependency_input=dependency_input,
            output_schema=output_schema,
            trigger_reason=trigger_reason,
            skill_id=skill_id,
            skill_version=version,
            skill_args=given_args,
        )


    def arg_names(self, skill_id: str, version: Optional[int]) -> Optional[list[str]]:
        """The skill's declared required_args names (plan validation, ruling
        32d(iii)); None when the skill or version is not found."""
        try:
            return [str(a) for a in (self.get(skill_id, version).get("required_args") or {})]
        except Exception:
            return None

    def field_names(self, value: object) -> Optional[list[str]]:
        """The field names an output_schema value names (see schema_field_names)."""
        return schema_field_names(value)

    def return_fields(self, skill_id: str, version: Optional[int], args: object) -> Optional[list[str]]:
        """The field names a skill step's rows hold: the skill's own
        output_schema keys, the fields its schema takes from an arg
        (``result_or_defer``'s ``{output_schema}``, ``{expected_return_schema}``,
        ``{original_output_schema}``, a schema "derived from fields"), the
        step's ``extra_fields``, and ``item`` and ``error`` (every row has
        them). None when the fields cannot be read (a skill not found, an arg
        that should hold a schema and does not): the step is then not checked.
        """
        try:
            skill = self.get(skill_id, version)
            raw = (skill.get("outbound_template") or {}).get("output_schema")
            a = args if isinstance(args, dict) else {}
            if isinstance(raw, str):
                text = raw.strip()
                whole = _PLACEHOLDER.fullmatch(text)
                if whole:
                    names = schema_field_names(a.get(whole.group(1)))
                elif _DERIVED_SCHEMA.fullmatch(text):
                    src = _DERIVED_SCHEMA.fullmatch(text).group(1)
                    names = [f["name"] for f in _normalise_fields(skill_id, src, a.get(src))]
                else:
                    names = None
                if names is None:
                    return None
                base: list = list(names)
            elif isinstance(raw, dict) and raw:
                base = list(raw["properties"] if isinstance(raw.get("properties"), dict) else raw)
                for value in raw.values():
                    for ph in _PLACEHOLDER.findall(value) if isinstance(value, str) else []:
                        got = a.get(ph)
                        if isinstance(got, dict):
                            base += schema_field_names(got) or []
                        elif "schema" in ph:
                            return None
            else:
                return None
            names = list(dict.fromkeys(str(n) for n in base))
            extra = _step_fields(skill_id, a.get("extra_fields"), {n: "" for n in names})
            names += [n for n in extra if n not in names]
            return names + [n for n in ("item", "error") if n not in names]
        except Exception:
            return None


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
                if isinstance(value, list):
                    value = ", ".join(str(v) for v in value)
                elif isinstance(value, dict):
                    value = json.dumps(value)
                elif source == "max_length" and isinstance(value, str):
                    # "100 words" -> "100": the wording already says "words".
                    value = re.sub(r"\s*words?\s*\.?\s*$", "", value, flags=re.I)
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


def _value_text(value: object, sep: str = ", ") -> str:
    """Text for a value in a template string.

    A list of plain values is joined with ``sep`` (", " in prose, "|" right
    after "enum:"); a dict, or a list holding dicts or lists, is JSON.
    """
    if value is None:
        return ""
    if isinstance(value, list) and not any(isinstance(v, (list, dict)) for v in value):
        return sep.join(str(v) for v in value)
    if isinstance(value, (list, dict)):
        return json.dumps(value)
    return str(value)


def _safe_substitute(template: str, args: dict) -> str:
    """Replace {key} placeholders in one pass. Unknown keys left as-is.

    One pass, so braces inside a substituted value are never read as a
    placeholder. A null value renders as "".
    """
    def _one(m: re.Match) -> str:
        if m.group(1) not in args:
            return m.group(0)
        sep = "|" if m.string[: m.start()].endswith("enum:") else ", "
        return _value_text(args[m.group(1)], sep)

    return _PLACEHOLDER.sub(_one, template)


def _is_optional(hint: object) -> bool:
    """True when a required_args type hint allows null (``int | null``)."""
    return "null" in str(hint).lower()


def _skl_008(skill_id: str, why: str) -> RuntimeError:
    return RuntimeError(f"BRT-SKL-008: render failed — skill '{skill_id}': {why}")


def _coerce_arg(skill_id: str, name: str, hint: object, value: object) -> object:
    """An arg in the shape its hint names.

    A ``list[...]`` arg given as a string is JSON when it looks like JSON
    (starts with ``[`` or ``{``), else a comma list ("billing, sales"). A
    JSON object is kept for a ``list[dict]`` arg (``fields``, ``criteria``:
    name -> type or spec), never split on its commas. A ``dict`` arg given
    as a string is parsed as JSON.

    Raises:
        RuntimeError: BRT-SKL-008 on an empty comma entry, invalid JSON in an
            arg that looks like JSON, or a JSON value of a shape the hint
            cannot take.
    """
    hint_s = str(hint).replace(" ", "").lower()
    if not isinstance(value, str):
        return value
    text = value.strip()
    if hint_s.startswith("list"):
        if text[:1] in ("[", "{"):
            try:
                parsed = json.loads(text)
            except ValueError as exc:
                raise _skl_008(skill_id, f"arg '{name}' looks like JSON but is not "
                                         f"valid JSON: {exc}") from exc
            if isinstance(parsed, list):
                return parsed
            if isinstance(parsed, dict) and "dict" in hint_s:
                return parsed
            raise _skl_008(skill_id, f"arg '{name}' ({hint}) must be a JSON list, "
                                     f"got {value!r}")
        if not text:
            return []
        parts = [p.strip() for p in text.split(",")]
        if any(not p for p in parts):
            raise _skl_008(skill_id, f"arg '{name}' has an empty entry: {value!r}")
        return parts
    if hint_s.startswith("dict"):
        try:
            parsed = json.loads(text)
        except ValueError as exc:
            raise _skl_008(skill_id, f"arg '{name}' is not valid JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise _skl_008(skill_id, f"arg '{name}' must be a JSON object, got {value!r}")
        return parsed
    return value


_DERIVED_SCHEMA = re.compile(r"derived\s+from\s+(\w+)", re.I)


def _string_schema(skill_id: str, raw: str, values: dict) -> dict:
    """The output_schema for a template that gives it as a string.

    "derived from <arg>" builds a flat schema from that arg; "{<arg>}" uses
    that arg's dict (a JSON string is parsed before this). Anything else is
    BRT-SKL-008: a schema the model and validator can use is never faked.
    """
    derived = _DERIVED_SCHEMA.fullmatch(raw.strip())
    if derived:
        source = derived.group(1)
        if source not in values:
            raise _skl_008(skill_id, f"output_schema is derived from '{source}', "
                                     f"which no arg supplies")
        return _schema_from_fields(skill_id, source, values[source])
    whole = _PLACEHOLDER.fullmatch(raw.strip())
    if whole and isinstance(values.get(whole.group(1)), dict) and values[whole.group(1)]:
        return values[whole.group(1)]
    raise _skl_008(skill_id, f"output_schema {raw!r} does not resolve to a schema dict "
                             f"(got {values.get(whole.group(1)) if whole else raw!r})")


def _normalise_fields(skill_id: str, name: str, value: object) -> list[dict]:
    """Fields as ``[{name, type?, enum_values?, description?}]``.

    Accepts a list of dicts, a list of names, a dict name -> type or spec,
    or a comma string (split by ``_coerce_arg``).

    Raises:
        RuntimeError: BRT-SKL-008 on an empty, nameless or duplicate field.
    """
    if isinstance(value, dict):
        entries = [{"name": k, **(v if isinstance(v, dict) else
                                  ({"type": v} if v not in (None, "") else {}))}
                   for k, v in value.items()]
    elif isinstance(value, list):
        entries = []
        for e in value:
            if isinstance(e, str):
                entries.append({"name": e})
            elif isinstance(e, dict):
                entries.append(dict(e))
            else:
                raise _skl_008(skill_id, f"arg '{name}' has an entry that is not a "
                                         f"name or a dict: {e!r}")
    else:
        entries = []
    if not entries:
        raise _skl_008(skill_id, f"arg '{name}' is empty; the output schema needs "
                                 f"at least one field")
    seen: set[str] = set()
    for e in entries:
        fname = e.get("name")
        if not isinstance(fname, str) or not fname.strip():
            raise _skl_008(skill_id, f"arg '{name}' has an entry with no name: {e!r}")
        e["name"] = fname.strip()
        if e["name"] in seen:
            raise _skl_008(skill_id, f"arg '{name}' names field '{e['name']}' twice")
        seen.add(e["name"])
    return entries


def _schema_from_fields(skill_id: str, name: str, value: object) -> dict:
    """A flat per-item schema: one required key per field.

    Hint: ``"<type> | null"`` (type defaults to string), or
    ``"enum:a|b|c|null"`` when enum_values are given; " — <description>"
    is added when the field has one.
    """
    schema: dict = {}
    for f in _normalise_fields(skill_id, name, value):
        enum_values = f.get("enum_values")
        if isinstance(enum_values, str):
            enum_values = [v.strip() for v in enum_values.split(",") if v.strip()]
        if enum_values:
            hint = "enum:" + "|".join(str(v) for v in enum_values) + "|null"
        else:
            ftype = str(f.get("type") or "string").strip()
            hint = ftype if "null" in ftype.lower() else f"{ftype} | null"
        if f.get("description"):
            hint += f" — {f['description']}"
        schema[f["name"]] = hint
    return schema


def _schema_keys(schema: object) -> set[str]:
    """The field names of a flat or JSON-Schema-shaped output schema."""
    if not isinstance(schema, dict):
        return set()
    if isinstance(schema.get("properties"), dict):
        return set(schema["properties"])
    return set(schema)


# JSON-Schema keywords: never a field name in a step's extra_fields.
_JSON_SCHEMA_KEYWORDS = frozenset({
    "type", "items", "properties", "required", "additionalProperties",
    "$schema", "$id", "$ref", "$defs", "definitions", "title",
    "minItems", "maxItems", "uniqueItems", "patternProperties",
    "allOf", "anyOf", "oneOf", "not",
})
# Keywords of a typed schema that are only names elsewhere (a field may be
# called "description"), so they are dropped only from a typed schema.
_SCHEMA_ANNOTATIONS = frozenset({
    "description", "enum", "format", "default", "minimum", "maximum", "pattern",
    "examples", "const",
})


def _unwrap_json_schema(value: object) -> object:
    """A JSON-Schema-shaped ``extra_fields`` reduced to its field map: an
    array's ``items`` is followed, an object's ``properties`` are the fields;
    a schema with no properties gives ``{}``. Anything else is returned as is.
    """
    for _ in range(4):
        if not isinstance(value, dict):
            return value
        if isinstance(value.get("properties"), dict):
            return value["properties"]
        if value.get("type") == "array" or "items" in value:
            if isinstance(value.get("items"), dict):
                value = value["items"]
                continue
            return {}
        if value.get("type") in ("object", "string", "integer", "number", "boolean"):
            # A typed schema with no properties: its keywords (description,
            # enum ... included) are not fields; any other key is.
            return {k: v for k, v in value.items()
                    if k not in _JSON_SCHEMA_KEYWORDS and k not in _SCHEMA_ANNOTATIONS}
        return value
    return value


def _step_fields(skill_id: str, extra_fields: object, skill_schema: object) -> dict:
    """The fields a skill step asks for beyond its skill's schema, name ->
    hint, from the step's explicit ``extra_fields`` arg only (R1-R4 review:
    never the node's ``output_schema``). JSON-Schema keywords are skipped.

    Raises:
        RuntimeError: BRT-SKL-008 when ``extra_fields`` is given but holds a
            nameless, duplicate or malformed entry (or invalid JSON).
    """
    have = _schema_keys(skill_schema)
    added: dict = {}
    if extra_fields in (None, "", [], {}):
        return added
    value = extra_fields
    if isinstance(value, str):
        text = value.strip()
        if text.startswith(("[", "{")):
            try:
                value = json.loads(text)
            except ValueError as exc:
                raise _skl_008(skill_id, f"arg 'extra_fields' is not valid JSON: {exc}")
        else:
            value = [v.strip() for v in text.split(",")]
    value = _unwrap_json_schema(value)
    if isinstance(value, dict):
        value = {k: v for k, v in value.items() if str(k).strip() not in _JSON_SCHEMA_KEYWORDS}
        if not value:
            return added
    for f in _normalise_fields(skill_id, "extra_fields", value):
        if f["name"] in _JSON_SCHEMA_KEYWORDS:
            continue
        hint = str(f.get("type") or "string").strip()
        if f.get("description"):
            hint += f" — {f['description']}"
        if f["name"] not in have:
            added[f["name"]] = hint
    return added


def _with_fields(schema: object, added: dict) -> dict:
    """``schema`` with ``added`` fields.

    Each added field is required by the inbound check, except a field whose hint
    holds "(only if", which may be omitted unless the schema has an explicit
    ``required`` list (the added names are appended to it, so they stay required).
    """
    if isinstance(schema, dict) and isinstance(schema.get("properties"), dict):
        out = dict(schema)
        out["properties"] = {**schema["properties"],
                             **{k: {"type": "string", "description": v} for k, v in added.items()}}
        if isinstance(schema.get("required"), list):
            out["required"] = list(schema["required"]) + list(added)
        return out
    return {**(schema if isinstance(schema, dict) else {}), **added}


def _also_return_line(added: dict) -> str:
    """One instruction line naming every added field and its hint."""
    return "Also return these fields: " + "; ".join(
        f"{name} ({hint})" for name, hint in added.items()
    ) + "."


def _concurrency_limit(skill_id: str, raw: object, values: dict) -> int:
    """The rendered concurrency_limit: the arg's int, or 20 when null/omitted.

    Raises:
        RuntimeError: BRT-SKL-008 if the value is not a positive integer.
    """
    if isinstance(raw, str):
        whole = _PLACEHOLDER.fullmatch(raw.strip())
        value = values.get(whole.group(1)) if whole else _safe_substitute(raw, values)
    elif raw is None:
        value = values.get("concurrency_limit")
    else:
        value = raw
    if value in (None, ""):
        return 20
    try:
        if isinstance(value, bool):
            raise ValueError
        if isinstance(value, int):
            limit = value
        else:
            # A whole-number float (12.0, "12.0") is 12; 2.5, "nan" or
            # "inf" is not a whole number.
            number = float(str(value).strip())
            if not number.is_integer():
                raise ValueError
            limit = int(number)
    except (ValueError, TypeError, OverflowError):
        raise _skl_008(skill_id, f"concurrency_limit {value!r} is not a whole number") from None
    if limit < 1:
        raise _skl_008(skill_id, f"concurrency_limit {limit} must be at least 1")
    return limit


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


def schema_field_names(value: object) -> Optional[list[str]]:
    """The field names an output_schema names, read like a step's
    ``extra_fields``: a flat map, a JSON-Schema object (its ``properties``),
    an array schema (its ``items``), a bare typed schema, a JSON string of
    these, a list of names or ``{name, ...}`` entries. None when it is none
    of these.
    """
    if isinstance(value, str):
        text = value.strip()
        if not text.startswith(("[", "{")):
            return None
        try:
            value = json.loads(text)
        except ValueError:
            return None
    value = _unwrap_json_schema(value)
    if isinstance(value, dict):
        return [str(k) for k in value if str(k).strip() not in _JSON_SCHEMA_KEYWORDS]
    if isinstance(value, list):
        out = []
        for v in value:
            name = v.get("name") if isinstance(v, dict) else v
            if not isinstance(name, str) or not name.strip():
                return None
            out.append(name.strip())
        return out
    return None
