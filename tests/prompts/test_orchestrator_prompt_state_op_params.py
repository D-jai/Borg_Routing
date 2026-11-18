"""The planner is told each state_op's params (Deej's ruling 13, 18-Nov-2026:
an additive edit to the planner prompt, state_op section only), and the prompt
cannot drift from the code's OP_PARAMS.
"""
import re

import pytest

from src.orchestrator.engine import _ORCHESTRATOR_TEMPLATE, _safe_sub
from src.orchestrator.state_ops import OP_PARAMS
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT

LINES = (
    "Params of each op. Any other param fails the step.",
    "- `filter`: `key` (a field name) and `value` (keep the rows where that field equals it); "
    "optional `negate`: true keeps the rows that do not match.",
    "- `count`: no params gives the number of rows; optional `key` gives a count per value of that field.",
    "- `group_by`: `key` (a field name) gives the rows per value; add `count`: true for counts per value.",
    "- `sample`: `n` (rows to take); optional `key` takes n per value of that field; optional `seed`. "
    "Over a `group_by` source it takes n per group.",
    "A field name is a field of the upstream step's output, written bare (`bucket`).",
    "These four are the only ops: there is no join, sort, order or limit.",
)
BLOCK = "\n".join(LINES)

PLACEHOLDERS = (
    "current_mode", "run_id", "current_depth", "max_depth", "available_models_brief",
    "thread_window", "rolling_summary", "stage_results", "tool_results",
    "child_scratch_digest", "context_budget_hint", "skills_catalogue",
)


def _op_lines(prompt: str) -> dict[str, str]:
    start = prompt.index(LINES[0])
    block = prompt[start:prompt.index(LINES[-1], start)]
    out = {}
    for line in block.splitlines():
        m = re.match(r"- `(\w+)`: (.*)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def test_the_seven_lines_sit_between_the_state_op_close_and_orchestrate():
    prompt = ORCHESTRATOR_SYSTEM_PROMPT
    assert prompt.count(BLOCK) == 1
    anchor = prompt.index("Every state_op saves tokens and adds zero error risk.")
    assert anchor < prompt.index(BLOCK) < prompt.index("### 4. `orchestrate`")


@pytest.mark.parametrize("mode", ["plan", "re_plan", "compose"])
def test_the_prompt_still_formats_in_every_mode(mode):
    # The engine fills the template with _safe_sub after un-doubling braces (the raw
    # string already holds single-brace JSON, so plain str.format never applied).
    assert "{" not in BLOCK and "}" not in BLOCK
    subs = {name: f"<{name}>" for name in PLACEHOLDERS} | {"current_mode": mode}
    rendered = _safe_sub(_ORCHESTRATOR_TEMPLATE, subs)
    assert BLOCK in rendered
    assert not any("{" + name + "}" in rendered for name in PLACEHOLDERS)
    assert f"<run_id>" in rendered and mode in rendered


def test_every_op_in_the_code_has_a_line_and_no_other_op_does():
    assert set(_op_lines(ORCHESTRATOR_SYSTEM_PROMPT)) == set(OP_PARAMS)


@pytest.mark.parametrize("op", sorted(OP_PARAMS))
def test_the_params_named_for_an_op_are_exactly_its_code_params(op):
    text = _op_lines(ORCHESTRATOR_SYSTEM_PROMPT)[op]
    code = {spec.split("|")[0] for spec in OP_PARAMS[op]}  # `key|group_by` is written `key`
    passing = set(OP_PARAMS) - code  # an op named in passing ("Over a `group_by` source")
    named = set(re.findall(r"`(\w+)`", text)) - passing
    assert named == code
