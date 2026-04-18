"""Slice 2 E2E tests — Core Pattern End-to-End.

These tests verify that all Slice 2 components integrate correctly.
Model calls are mocked (no real model required) so the pipeline runs
without any local or external AI services.

TC-E2E-010 — POST /v1/chat/completions returns OpenAI-shaped response
TC-E2E-011 — MCP borg_route.chat tool call succeeds
TC-E2E-012 — POST /v1/task + GET /runs/{id} lifecycle works
TC-E2E-013 — SkillLibrary render + validate_outbound: all 10 skills valid
TC-E2E-014 — apply_to_each over 10 issues aggregates correctly (mocked)
TC-E2E-015 — fold_in detects residue + cleans it + logs event
TC-E2E-016 — run_pipeline happy path with mocked orchestrator
TC-E2E-017 — MCP initialize → tools/list → tools/call full flow
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import create_all
from src.envelopes.schema import WrappedTask, InboundEnvelope
from src.envelopes.validator import validate_outbound
from src.skills.library import SkillLibrary
from src.orchestrator.apply_to_each import expand_and_run
from src.orchestrator.fold_in import fold_in
from src.db.scratch import write_scratch, count_scratch_for_level
from src.pipeline.runner import run_pipeline

SKILLS_PATH = "data/skills.yaml"
ISSUES_PATH = "tests/fixtures/issues.json"


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "e2e_slice2.db")
    await create_all(path)
    return path


# ─── TC-E2E-010 ──────────────────────────────────────────────────────────────

def test_e2e_chat_completions_openai_shape(client):
    """TC-E2E-010 — /v1/chat/completions returns OpenAI-shaped response."""
    async def mock_pipeline(user_instruction, **kwargs):
        return {
            "run_id": "e2e-run-01",
            "response": "Classified: 5 bugs, 3 features, 2 questions.",
            "status": "completed",
            "latency_ms": 200,
            "envelope_count": 3,
            "error": None,
        }

    with patch("src.api.chat_completions.run_pipeline", new=mock_pipeline):
        resp = client.post("/v1/chat/completions", json={
            "model": "borg_route",
            "messages": [{"role": "user", "content": "Classify these 10 issues."}],
        })

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "chat.completion"
    assert "5 bugs" in body["choices"][0]["message"]["content"]
    assert body["choices"][0]["finish_reason"] == "stop"
    assert "usage" in body


# ─── TC-E2E-011 ──────────────────────────────────────────────────────────────

def test_e2e_mcp_chat_tool(client):
    """TC-E2E-011 — MCP borg_route.chat tool call succeeds."""
    async def mock_tool_chat(args):
        return {
            "content": [{"type": "text", "text": "MCP answer: 5 bugs, 3 features."}],
            "isError": False,
            "_meta": {"run_id": "mcp-e2e-run"},
        }

    with patch("src.mcp.server._tool_chat", new=AsyncMock(side_effect=mock_tool_chat)):
        resp = client.post("/mcp", json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "borg_route.chat",
                "arguments": {
                    "messages": [{"role": "user", "content": "Classify these 10 issues."}]
                }
            }
        })

    assert resp.status_code == 200
    body = resp.json()
    assert "result" in body
    assert "MCP answer" in body["result"]["content"][0]["text"]


# ─── TC-E2E-012 ──────────────────────────────────────────────────────────────

def test_e2e_task_api_lifecycle(client):
    """TC-E2E-012 — POST /v1/task + GET /runs/{id} full lifecycle."""
    async def mock_background(*args, **kwargs):
        pass

    with patch("src.api.tasks._run_in_background", new=mock_background):
        post_resp = client.post("/v1/task", json={
            "instruction": "Classify these 10 GitHub issues into buckets."
        })

    assert post_resp.status_code == 202
    run_id = post_resp.json()["run_id"]
    assert uuid.UUID(run_id)

    get_resp = client.get(f"/runs/{run_id}")
    assert get_resp.status_code == 200
    data = get_resp.json()
    assert data["run"]["run_id"] == run_id
    assert data["run"]["status"] in ("pending", "running", "completed", "failed")
    assert isinstance(data["envelopes"], list)
    assert isinstance(data["events"], list)


# ─── TC-E2E-013 ──────────────────────────────────────────────────────────────

def test_e2e_all_10_skills_render_valid():
    """TC-E2E-013 — All 10 skills render to validate_outbound-passing WrappedTasks."""
    lib = SkillLibrary()
    lib.load(SKILLS_PATH)

    skill_sample_args = {
        "classify_into_buckets": {
            "buckets": ["bug", "feature", "question"],
            "items_ref": "run:issues",
            "concurrency_limit": 10,
        },
        "extract_fields": {
            "items_ref": "run:docs",
            "fields": [{"name": "author", "type": "string", "description": "Author name"}],
            "concurrency_limit": 5,
        },
        "summarize": {
            "items_ref": "run:articles",
            "max_points": 5,
            "focus": "key findings",
        },
        "compare_and_rank": {
            "items_ref": "run:proposals",
            "criteria": [{"name": "fit", "weight": 0.5, "description": "Technical fit"}],
        },
        "filter_by_criterion": {
            "items_ref": "run:items",
            "criterion": "Is this item urgent?",
            "concurrency_limit": 10,
        },
        "transform_style": {
            "items_ref": "run:texts",
            "target_style": "plain English",
            "target_format": "prose",
            "max_length": 100,
        },
        "decompose_task": {
            "sub_task_instruction": "Analyse this document.",
            "context_for_child": "The document is about AI routing.",
            "expected_return_schema": {"summary": "string"},
        },
        "verify_claim": {
            "claim": "Model X is faster than Model Y.",
            "evidence_ref": "run:benchmarks",
        },
        "retry_escalate": {
            "failed_items_ref": "run:failures",
            "original_instruction": "Classify this item.",
            "original_output_schema": {"bucket": "string"},
            "escalation_note": "Previous attempt was uncertain.",
        },
        "defer_and_collect": {
            "items_ref": "run:items",
            "instruction": "Classify each item.",
            "output_schema": {"category": "string"},
            "confidence_threshold": 0.7,
        },
    }

    for skill_id, args in skill_sample_args.items():
        try:
            task = lib.render(skill_id=skill_id, version=1, args=args)
            validate_outbound(task)
        except Exception as exc:
            pytest.fail(f"Skill '{skill_id}' failed: {exc}")


# ─── TC-E2E-014 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_apply_to_each_10_issues(db):
    """TC-E2E-014 — apply_to_each over 10 issues with 2 injected failures."""
    import json as _json
    with open(ISSUES_PATH) as f:
        issues = _json.load(f)

    assert len(issues) == 10

    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=uuid.uuid4(),
        subtask_id=uuid.uuid4(),
        type="apply_to_each",
        current_depth=1,
        max_depth=5,
        level_path="1",
        instruction="Classify this issue into: bug, feature_request, or question.",
        output_schema={"bucket": "bug|feature_request|question"},
        dependency_input={
            "items": issues,
            "concurrency_limit": 5,
        },
        model_endpoint="http://localhost:1234",
        model_name="test-model",
    )

    call_count = [0]
    async def mock_dispatch(child_task, db_path):
        call_count[0] += 1
        idx = int(child_task.trigger_reason.split("item_")[1])
        if idx in (2, 7):  # inject 2 failures
            return InboundEnvelope(
                envelope_id=child_task.envelope_id,
                subtask_id=child_task.subtask_id,
                status="failed",
                output={},
                failure_reason="Simulated timeout.",
            )
        buckets = ["bug", "feature_request", "question"]
        bucket = buckets[idx % 3]
        return InboundEnvelope(
            envelope_id=child_task.envelope_id,
            subtask_id=child_task.subtask_id,
            status="resolved",
            output={"bucket": bucket},
            confidence=0.9,
        )

    with patch("src.orchestrator.apply_to_each._dispatch_child", new=mock_dispatch):
        env = await expand_and_run(task, db_path=db)

    assert env.status == "resolved"
    assert env.output["total"] == 10
    assert env.output["success_count"] == 8
    assert env.output["failure_count"] == 2
    assert len(env.output["results"]) == 8
    assert len(env.output["failures"]) == 2
    assert call_count[0] == 10
    # Verify failures are at indices 2 and 7
    failure_indices = [f["index"] for f in env.output["failures"]]
    assert 2 in failure_indices
    assert 7 in failure_indices


# ─── TC-E2E-015 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_fold_in_residue_detection(db):
    """TC-E2E-015 — fold_in detects residue + cleans it + logs child_residue."""
    import aiosqlite
    run_id = uuid.uuid4()
    env_id = uuid.uuid4()

    # Child leaves 3 scratch rows (simulates a misbehaving agent)
    for i in range(3):
        await write_scratch(run_id, "2a", env_id, "deferrals", {"item_id": f"i{i}"}, db_path=db)

    digest = await fold_in(run_id, "2a", child_model_id="bad-model", db_path=db)

    assert digest["residue_found"] is True
    assert digest["residue_count"] == 3
    assert digest["child_model"] == "bad-model"

    # Scratch must be empty after fold_in
    remaining = await count_scratch_for_level(run_id, "2a", db_path=db)
    assert remaining == 0

    # Log event must exist
    for _ in range(20):
        await asyncio.sleep(0)
    await asyncio.sleep(0.2)

    async with aiosqlite.connect(db) as conn:
        async with conn.execute(
            "SELECT event_type, model_id FROM logs WHERE event_type='child_residue'"
        ) as cur:
            rows = await cur.fetchall()
    assert len(rows) >= 1
    assert rows[0][1] == "bad-model"


# ─── TC-E2E-016 ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_e2e_run_pipeline_direct_answer(db):
    """TC-E2E-016 — run_pipeline happy path returns completed result."""
    async def mock_orchestrate(run_id, user_instruction, **kwargs):
        return "The distribution is: 5 bugs, 3 feature_requests, 2 questions."

    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate)):
        result = await run_pipeline(
            "Classify these 10 GitHub issues and summarise the distribution.",
            db_path=db,
        )

    assert result["status"] == "completed"
    assert "5 bugs" in result["response"]
    assert result["error"] is None
    assert result["run_id"] is not None


# ─── TC-E2E-017 ──────────────────────────────────────────────────────────────

def test_e2e_mcp_full_flow(client):
    """TC-E2E-017 — MCP initialize → tools/list → tools/call sequence."""
    # Step 1: initialize
    init_resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}
    })
    assert init_resp.status_code == 200
    assert init_resp.json()["result"]["serverInfo"]["name"] == "borg_route"

    # Step 2: list tools
    list_resp = client.post("/mcp", json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}
    })
    tools = {t["name"] for t in list_resp.json()["result"]["tools"]}
    assert "borg_route.chat" in tools
    assert "borg_route.list_models" in tools

    # Step 3: call list_models
    models_resp = client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": 3,
        "method": "tools/call",
        "params": {"name": "borg_route.list_models", "arguments": {}},
    })
    assert models_resp.status_code == 200
    result = models_resp.json()["result"]
    assert result["isError"] is False
    assert isinstance(json.loads(result["content"][0]["text"]), list)
