"""The planner is asked to label each step with its kind of work and size,
is shown only the models it may actually use, and reads JSON examples with
single braces (the template's doubled braces are an escape, not content).
"""
import pytest

from src.orchestrator import engine
from src.orchestrator.engine import _available_models_brief, _undouble_braces
from src.pipeline.runner import run_pipeline
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from src.registry.capability_matrix import CapabilityMatrix
from tests.e2e.stub_model_server import StubModels, drain_log_writes, stub_model

TASK_TYPE_LINE = (
    '"task_type": "reasoning | coding | summarize | writing | roleplay | agentic | extraction",'
)
TIER_LINE = '"tier": "light | mid | heavy (the smallest that does this step well)",'


def _shape(prompt: str, node_type: str) -> str:
    """The JSON example for one node type, up to its closing fence."""
    start = prompt.index(f'"type": "{node_type}"')
    return prompt[start:prompt.index("```", start)]


@pytest.mark.parametrize("node_type", ["answerer", "apply_to_each"])
def test_routable_shapes_ask_for_kind_of_work_and_size(node_type):
    shape = _shape(ORCHESTRATOR_SYSTEM_PROMPT, node_type)
    assert TASK_TYPE_LINE in shape
    assert TIER_LINE in shape
    assert '"recommended_model"' in shape        # still there, still optional


def test_the_labels_were_added_to_exactly_the_two_routable_shapes():
    assert ORCHESTRATOR_SYSTEM_PROMPT.count('"task_type":') == 2
    assert ORCHESTRATOR_SYSTEM_PROMPT.count('"tier":') == 2


# --- what the model actually reads ---------------------------------------

def test_undoubling_turns_escaped_braces_into_single_ones():
    assert _undouble_braces('{{"type": "answerer", "output_schema": {{ ... }}}}') == (
        '{"type": "answerer", "output_schema": { ... }}'
    )


def test_undoubling_leaves_placeholders_and_plain_json_alone():
    assert _undouble_braces("Mode: {current_mode}") == "Mode: {current_mode}"
    plain = '`tool_requests: [{"tool": "web_search", "args": {"query": "..."}}]`'
    assert _undouble_braces(plain) == plain     # a real nested close, not an escape


def test_undoubling_handles_an_escaped_object_holding_a_plain_one():
    assert _undouble_braces('{{"a": {"b": 1}}}') == '{"a": {"b": 1}}'


@pytest.fixture
async def planner_prompt(tmp_path, monkeypatch):
    """The system prompt of a real plan-mode call, plus the matrix used."""
    from src.db.schema import create_all

    db_path = str(tmp_path / "labels.db")
    await create_all(db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-labels")
    monkeypatch.setenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "9.0")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")

    matrix = CapabilityMatrix()
    matrix.upsert(stub_model("cheap", 6.5, 1e-7, 2e-7))
    matrix.upsert(stub_model("ghost", 9.9, 1e-9, 1e-9, checked=False))
    matrix.upsert(stub_model("top", 9.5, 5e-6, 2.5e-5))
    matrix.upsert(stub_model("mid", 8.0, 1e-6, 3e-6))
    monkeypatch.setattr(engine, "_shared_matrix", matrix)

    plan = [{"type": "answerer", "id": "n1", "instruction": "Do it.",
             "output_schema": {"result": "string"}}]
    stub = StubModels(plan_nodes=plan).install(monkeypatch)
    result = await run_pipeline("List the options, then pick one.\n1. a\n2. b", db_path=db_path)
    await drain_log_writes()
    assert result["status"] == "completed", result.get("error")
    return stub.of_kind("plan")[0].system


@pytest.mark.asyncio
async def test_the_planner_reads_single_braced_json_examples(planner_prompt):
    assert "{{" not in planner_prompt
    assert '{"type": "answerer",' in planner_prompt
    assert '{"type": "apply_to_each",' in planner_prompt
    assert '"args": {"query": "..."}}]' in planner_prompt     # untouched plain JSON
    assert planner_prompt.count("{") == planner_prompt.count("}")


@pytest.mark.asyncio
async def test_the_planner_is_shown_only_models_in_the_pool_best_first(planner_prompt):
    assert "openrouter/stub/ghost" not in planner_prompt       # unchecked
    positions = [
        planner_prompt.index(f"openrouter/stub/{name}") for name in ("top", "mid", "cheap")
    ]
    assert positions == sorted(positions)


def test_model_brief_says_what_each_model_is_good_at_and_costs():
    matrix = CapabilityMatrix()
    top = stub_model("top", 9.5, 5e-6, 2.5e-5)
    top.task_scores.update({"coding": 9.9, "roleplay": 3.0})
    top.specialty_tags = ["frontier"]
    matrix.upsert(top)
    matrix.upsert(stub_model("local", 6.5, 0, 0, source="ollama"))

    brief = _available_models_brief(matrix)
    top_line, local_line = brief.splitlines()

    assert top_line.startswith("- openrouter/stub/top")
    assert "frontier" in top_line
    assert "coding 9.9" in top_line and "roleplay" not in top_line   # strongest only
    assert "$5.00 in / $25.00 out per million tokens" in top_line
    assert local_line.startswith("- ollama/stub/local")
    assert "runs on this machine, no token price" in local_line


def test_model_brief_with_nothing_checked_says_so():
    matrix = CapabilityMatrix()
    matrix.upsert(stub_model("ghost", 9.9, 1e-9, 1e-9, checked=False))
    assert _available_models_brief(matrix) == "(no models are checked in the pool)"
