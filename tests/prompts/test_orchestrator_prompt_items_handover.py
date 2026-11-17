"""The planner is told where an apply_to_each step's items come from
(Planner ruling 9, 16-Nov-2026: the second approved additive edit to the
planner prompt, for the list hand-over, work order 1.10 only).
"""
import pytest

from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from tests.prompts.test_orchestrator_prompt_routing_labels import _shape, planner_prompt  # noqa: F401

ITEMS_LINE = (
    '"items": ["only when the list is in the user\'s message: '
    'the items themselves, one string each"],'
)
PARAGRAPH = (
    "Where the items come from. If the list is in the user's message, copy the "
    "items into `items`, one string per item, and leave `inputs_ref` out. If an "
    "earlier step produces the list, set `inputs_ref` to that step's id. "
    "`inputs_ref` is always the id of a step; a name you make up resolves to "
    "nothing. The same holds for a skill's `items_ref`."
)


def test_the_apply_to_each_shape_offers_inline_items_right_after_inputs_ref():
    shape = _shape(ORCHESTRATOR_SYSTEM_PROMPT, "apply_to_each")
    assert ITEMS_LINE in shape
    assert shape.index('"inputs_ref"') < shape.index(ITEMS_LINE)


INPUTS_REF_LINE = (
    '"inputs_ref": "id of the earlier step that produces the list '
    '(omit when you give items)",'
)


def test_the_shape_example_names_a_step_id_not_a_made_up_list():
    """Ruling 10: the example agrees with ruling 9's paragraph."""
    shape = _shape(ORCHESTRATOR_SYSTEM_PROMPT, "apply_to_each")
    assert INPUTS_REF_LINE in shape
    assert '"input_list"' not in ORCHESTRATOR_SYSTEM_PROMPT
    assert "the runtime resolves this from run state" not in ORCHESTRATOR_SYSTEM_PROMPT


def test_the_items_line_is_only_in_the_apply_to_each_shape():
    assert ORCHESTRATOR_SYSTEM_PROMPT.count(ITEMS_LINE) == 1
    assert ITEMS_LINE not in _shape(ORCHESTRATOR_SYSTEM_PROMPT, "answerer")


def test_the_hand_over_paragraph_follows_the_failure_note():
    prompt = ORCHESTRATOR_SYSTEM_PROMPT
    assert prompt.count(PARAGRAPH) == 1
    failure_note = prompt.index("You handle the failures\nin re_plan.")
    assert failure_note < prompt.index(PARAGRAPH) < prompt.index("### 3. `state_op`")


@pytest.mark.asyncio
async def test_the_rendered_planner_prompt_carries_both_additions(planner_prompt):  # noqa: F811
    """What the model reads, after brace un-doubling and substitution."""
    assert ITEMS_LINE in _shape(planner_prompt, "apply_to_each")
    assert INPUTS_REF_LINE in _shape(planner_prompt, "apply_to_each")
    assert PARAGRAPH in planner_prompt
    assert planner_prompt.count("{") == planner_prompt.count("}")
