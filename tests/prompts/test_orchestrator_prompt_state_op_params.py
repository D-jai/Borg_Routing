"""The planner is told each state_op's params (the owner's ruling 13, 18-Nov-2026:
an additive edit to the planner prompt, state_op section only), and the prompt
cannot drift from the code's OP_PARAMS.
"""
import re

import pytest

from src.orchestrator.state_ops import OP_PARAMS
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from tests.prompts.test_orchestrator_prompt_routing_labels import planner_prompt  # noqa: F401

LINES = (
    "Params of each op. Any other param fails the step.",
    "- `filter`: `key` (a field name) and `value` (keep the rows where that field equals it); "
    "optional `negate`: true keeps the rows that do not match.",
    "- `count`: no params gives the number of rows; optional `key` gives a count per value of that field.",
    "- `group_by`: `key` (a field name) gives the rows per value; add `count`: true for counts per value.",
    "- `sample`: `n` (rows to take); optional `key` takes n per value of that field; optional `seed`. "
    "Over a `group_by` source without `count` it takes n per group.",
    "A field name is a field of the upstream step's output, written bare (`bucket`).",
    "These four are the only ops: there is no join, sort, order or limit.",
)
BLOCK = "\n".join(LINES)

# The one place an op line names another op in passing rather than as a param.
IN_PASSING = {"sample": "Over a `group_by` source without `count` it takes n per group."}


def _op_lines(prompt: str) -> dict[str, str]:
    start = prompt.index(LINES[0])
    block = prompt[start:prompt.index(LINES[-1], start)]
    out = {}
    for line in block.splitlines():
        m = re.match(r"- `(\w+)`: (.*)", line)
        if m:
            out[m.group(1)] = m.group(2)
    return out


def _named_params(op: str, text: str) -> set[str]:
    return set(re.findall(r"`(\w+)`", text.replace(IN_PASSING.get(op, "\0"), "")))


def test_the_seven_lines_sit_between_the_state_op_close_and_orchestrate():
    prompt = ORCHESTRATOR_SYSTEM_PROMPT
    assert prompt.count(BLOCK) == 1
    anchor = prompt.index("Every state_op saves tokens and adds zero error risk.")
    assert anchor < prompt.index(BLOCK) < prompt.index("### 4. `orchestrate`")


@pytest.mark.asyncio
async def test_a_real_planner_call_carries_the_block_unchanged(planner_prompt):  # noqa: F811
    """What the model reads, built by the engine on a real plan-mode call. The
    template has no mode branch, so one mode stands for all three."""
    assert "{" not in BLOCK and "}" not in BLOCK
    assert BLOCK in planner_prompt


def test_every_op_in_the_code_has_a_line_and_no_other_op_does():
    assert set(_op_lines(ORCHESTRATOR_SYSTEM_PROMPT)) == set(OP_PARAMS)


@pytest.mark.parametrize("op", sorted(OP_PARAMS))
def test_the_params_named_for_an_op_are_exactly_its_code_params(op):
    code = {spec.split("|")[0] for spec in OP_PARAMS[op]}  # `key|group_by` is written `key`
    assert _named_params(op, _op_lines(ORCHESTRATOR_SYSTEM_PROMPT)[op]) == code


def test_the_drift_check_catches_a_param_added_to_the_prompt_only():
    """Both directions, including a prompt-only param that shares an op's name."""
    line = _op_lines(ORCHESTRATOR_SYSTEM_PROMPT)["sample"]
    code = {spec.split("|")[0] for spec in OP_PARAMS["sample"]}
    assert _named_params("sample", line + " optional `count`.") != code
    assert _named_params("sample", line + " optional `limit`.") != code
    assert _named_params("sample", line) != code | {"limit"}  # code-only addition
