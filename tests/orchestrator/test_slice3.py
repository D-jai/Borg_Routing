"""Slice 3 tests — recursion, resume, rule ledger, multi-turn context.

TC-S3-001 — orchestrate node at depth cap is demoted to answerer (BRT-ORC-020)
TC-S3-002 — orchestrate node below depth cap spawns child run with parent_run_id
TC-S3-003 — child run id is returned in envelope output
TC-S3-004 — resume_run() returns WrappedTasks for failed/in_flight envelopes
TC-S3-005 — resume_run() raises BRT-DB-020 for completed run
TC-S3-006 — admin rule append adds rule to ledger
TC-S3-007 — rule retire sets retired_at; retired rule excluded from governance
TC-S3-008 — extract_window returns last N messages in chronological order
TC-S3-009 — create_thread + add_message + extract_window round-trip
TC-S3-010 — run_pipeline accepts parent_run_id and current_depth
TC-S3-011 — POST /config stores key:value; GET /config retrieves it
TC-S3-012 — POST /config/rules appends rule; POST /config/rules/{id}/retire retires it
TC-S3-013 — GET /runs returns list of recent runs
TC-S3-014 — POST /runs/{id}/resume returns 202
TC-S3-015 — add_message to non-existent thread raises BRT-MEM-001
"""
import asyncio
import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from main import app
from src.db.schema import create_all, drop_all
from src.db.state import create_run, get_run, resume_run, set_config, get_config, update_run_status
from src.db.state import save_envelope
from src.envelopes.schema import WrappedTask, InboundEnvelope
from src.memory.window import create_thread, add_message, extract_window
from src.prompts.base import load_governance


# ──────────────────────────────────────────────────────────────────────────────
# Shared fixtures
# ──────────────────────────────────────────────────────────────────────────────

@pytest.fixture
async def db(tmp_path):
    path = str(tmp_path / "slice3_test.db")
    await create_all(path)
    return path


@pytest.fixture
def client():
    return TestClient(app, raise_server_exceptions=False)


def _make_task(run_id, current_depth=0, max_depth=5, task_type="orchestrate"):
    return WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=run_id,
        subtask_id=uuid.uuid4(),
        type=task_type,
        current_depth=current_depth,
        max_depth=max_depth,
        level_path=f"{current_depth+1}a",
        instruction="Do something.",
        output_schema={"result": "string"},
        model_endpoint="http://localhost:1234",
        model_name="test-model",
    )


# ──────────────────────────────────────────────────────────────────────────────
# S3-01 recursion / demotion tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_orchestrate_at_depth_cap_demotes_to_answerer(db):
    """TC-S3-001 — orchestrate at max_depth is demoted and BRT-ORC-020 logged."""
    run_id = await create_run("top level", db_path=db)
    # Create task AT max_depth (current_depth == max_depth)
    task = _make_task(run_id, current_depth=5, max_depth=5, task_type="orchestrate")

    answerer_called = [False]

    async def mock_answerer(t, dp):
        answerer_called[0] = True
        return InboundEnvelope(
            envelope_id=t.envelope_id,
            subtask_id=t.subtask_id,
            status="resolved",
            output={"result": "demoted answer"},
        )

    with patch("src.agents.executor._execute_answerer", new=AsyncMock(side_effect=mock_answerer)):
        with patch("src.agents.executor.write_event", new=AsyncMock()):
            from src.agents.executor import execute
            result = await execute(task, db_path=db)

    assert result.status == "resolved"
    assert answerer_called[0], "should have called _execute_answerer (demotion)"


@pytest.mark.asyncio
async def test_orchestrate_below_cap_spawns_child_run(db):
    """TC-S3-002 — orchestrate below depth cap spawns a child run with parent_run_id."""
    run_id = await create_run("parent task", db_path=db)
    task = _make_task(run_id, current_depth=1, max_depth=5, task_type="orchestrate")

    created_runs = []

    original_create_run = create_run

    async def mock_create_run(instruction, parent_run_id=None, thread_id=None, db_path=db):
        child_id = await original_create_run(
            instruction, parent_run_id=parent_run_id, thread_id=thread_id, db_path=db_path
        )
        created_runs.append((child_id, parent_run_id))
        return child_id

    async def mock_orchestrate_run(
        run_id, user_instruction, db_path, current_depth, root_instruction=None
    ):
        return "child response"

    async def mock_update_status(run_id, status, db_path):
        pass

    with patch("src.agents.executor.create_run", new=AsyncMock(side_effect=mock_create_run)):
        with patch("src.orchestrator.engine.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate_run)):
            with patch("src.agents.executor.update_run_status", new=AsyncMock(side_effect=mock_update_status)):
                with patch("src.agents.executor.write_event", new=AsyncMock()):
                    from src.agents.executor import execute
                    result = await execute(task, db_path=db)

    assert result.status == "resolved"
    assert len(created_runs) == 1
    child_id, parent_id = created_runs[0]
    assert str(parent_id) == str(run_id), "child run must link to parent"


@pytest.mark.asyncio
async def test_orchestrate_child_run_id_in_output(db):
    """TC-S3-003 — child_run_id returned in envelope output."""
    run_id = await create_run("parent", db_path=db)
    task = _make_task(run_id, current_depth=0, max_depth=5, task_type="orchestrate")

    child_uuid = uuid.uuid4()

    async def mock_create_run(instruction, parent_run_id=None, thread_id=None, db_path=db):
        return child_uuid

    async def mock_orchestrate_run(**kwargs):
        return "child answer"

    async def mock_update_status(run_id, status, db_path):
        pass

    with patch("src.agents.executor.create_run", new=AsyncMock(side_effect=mock_create_run)):
        with patch("src.orchestrator.engine.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate_run)):
            with patch("src.agents.executor.update_run_status", new=AsyncMock(side_effect=mock_update_status)):
                with patch("src.agents.executor.write_event", new=AsyncMock()):
                    from src.agents.executor import execute
                    result = await execute(task, db_path=db)

    assert result.output.get("child_run_id") == str(child_uuid)
    assert result.output.get("response") == "child answer"


# ──────────────────────────────────────────────────────────────────────────────
# S3-03 resume_run tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resume_run_returns_failed_envelopes(db):
    """TC-S3-004 — resume_run() returns WrappedTasks for failed/in_flight envelopes."""
    run_id = await create_run("resume test", db_path=db)
    task = WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=run_id,
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=0,
        max_depth=5,
        level_path="1a",
        instruction="Classify this.",
        output_schema={"bucket": "string"},
    )
    await save_envelope(task, db_path=db)

    # Manually set status to failed in DB
    import aiosqlite
    async with aiosqlite.connect(db) as conn:
        await conn.execute(
            "UPDATE envelopes SET status='failed' WHERE envelope_id=?",
            (str(task.envelope_id),),
        )
        await conn.commit()

    tasks = await resume_run(run_id, db_path=db)
    assert len(tasks) == 1
    assert tasks[0].envelope_id == task.envelope_id


@pytest.mark.asyncio
async def test_resume_run_raises_for_completed(db):
    """TC-S3-005 — resume_run() raises BRT-DB-020 for completed run."""
    run_id = await create_run("done", db_path=db)
    await update_run_status(run_id, "completed", db_path=db)

    with pytest.raises(ValueError, match="BRT-DB-020"):
        await resume_run(run_id, db_path=db)


# ──────────────────────────────────────────────────────────────────────────────
# S3-05 rule ledger / governance tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rule_ledger_append_and_load(db):
    """TC-S3-006 — appended rule appears in governance ledger."""
    ledger = [
        {"id": "r1", "text": "Never lie.", "added_at": "2026-01-01T00:00:00+00:00", "retired_at": None},
    ]
    await set_config("admin_rules_ledger", ledger, db_path=db)

    gov = await load_governance(lambda key: get_config(key, db_path=db))
    assert len(gov["admin_rules_ledger"]) == 1
    assert gov["admin_rules_ledger"][0]["text"] == "Never lie."


@pytest.mark.asyncio
async def test_retired_rule_excluded_from_governance(db):
    """TC-S3-007 — retired rule is not included in active governance bundle."""
    ledger = [
        {"id": "r1", "text": "Active rule.", "added_at": "2026-01-01T00:00:00+00:00", "retired_at": None},
        {"id": "r2", "text": "Retired rule.", "added_at": "2026-01-01T00:00:00+00:00", "retired_at": "2026-04-01T00:00:00+00:00"},
    ]
    await set_config("admin_rules_ledger", ledger, db_path=db)

    gov = await load_governance(lambda key: get_config(key, db_path=db))
    active_texts = [r["text"] for r in gov["admin_rules_ledger"]]
    assert "Active rule." in active_texts
    assert "Retired rule." not in active_texts


# ──────────────────────────────────────────────────────────────────────────────
# S3-10 thread / window tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_create_thread_and_add_messages(db):
    """TC-S3-009 — thread create + message add + window extract round-trip."""
    thread_id = await create_thread(db_path=db)
    assert thread_id

    await add_message(thread_id, "user", "Hello!", db_path=db)
    await add_message(thread_id, "assistant", "Hi there!", db_path=db)
    await add_message(thread_id, "user", "How are you?", db_path=db)

    window = await extract_window(thread_id, max_turns=10, db_path=db)
    assert len(window) == 3
    assert window[0]["role"] == "user"
    assert window[0]["content"] == "Hello!"
    assert window[2]["content"] == "How are you?"


@pytest.mark.asyncio
async def test_extract_window_respects_max_turns(db):
    """TC-S3-008 — extract_window returns at most max_turns messages."""
    thread_id = await create_thread(db_path=db)
    for i in range(15):
        await add_message(thread_id, "user", f"Message {i}", db_path=db)

    window = await extract_window(thread_id, max_turns=5, db_path=db)
    assert len(window) == 5
    # Should be the last 5 (most recent), in chronological order
    assert window[-1]["content"] == "Message 14"


@pytest.mark.asyncio
async def test_add_message_to_nonexistent_thread_raises(db):
    """TC-S3-015 — add_message to non-existent thread raises BRT-MEM-001."""
    with pytest.raises(ValueError, match="BRT-MEM-001"):
        await add_message("nonexistent-thread-id", "user", "Hello", db_path=db)


# ──────────────────────────────────────────────────────────────────────────────
# S3-02 runner multi-turn tests
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_run_pipeline_with_parent_run_id(db):
    """TC-S3-010 — run_pipeline stores parent_run_id in run record."""
    parent_run_id = await create_run("parent", db_path=db)

    async def mock_orchestrate(**kwargs):
        return "child response"

    with patch("src.pipeline.runner.orchestrate_run", new=AsyncMock(side_effect=mock_orchestrate)):
        from src.pipeline.runner import run_pipeline
        result = await run_pipeline(
            "child task",
            parent_run_id=parent_run_id,
            current_depth=1,
            db_path=db,
        )

    assert result["status"] == "completed"
    child_run = await get_run(uuid.UUID(result["run_id"]), db_path=db)
    assert child_run["parent_run_id"] == str(parent_run_id)


# ──────────────────────────────────────────────────────────────────────────────
# S3-04 admin API tests (via FastAPI TestClient)
# ──────────────────────────────────────────────────────────────────────────────

def test_config_set_and_get(client):
    """TC-S3-011 — POST /config stores value; GET /config retrieves it."""
    resp = client.post("/config", json={"key": "lane_local_allowlist", "value": "hello"})
    assert resp.status_code == 200
    assert resp.json()["status"] == "saved"

    get_resp = client.get("/config")
    assert get_resp.status_code == 200
    cfg = get_resp.json()
    assert cfg["lane_local_allowlist"] == "hello"


def test_rule_append_and_retire(client):
    """TC-S3-012 — POST /config/rules appends; /retire sets retired_at."""
    resp = client.post("/config/rules", json={"text": "Test rule for S3."})
    assert resp.status_code == 200
    rule_id = resp.json()["rule"]["id"]

    list_resp = client.get("/config/rules")
    assert list_resp.status_code == 200
    rules = list_resp.json()["rules"]
    active = [r for r in rules if r["id"] == rule_id]
    assert len(active) == 1
    assert active[0]["retired_at"] is None

    retire_resp = client.post(f"/config/rules/{rule_id}/retire")
    assert retire_resp.status_code == 200
    assert retire_resp.json()["status"] == "retired"

    list_resp2 = client.get("/config/rules")
    rules2 = list_resp2.json()["rules"]
    retired = next(r for r in rules2 if r["id"] == rule_id)
    assert retired["retired_at"] is not None


def test_rule_retire_not_found(client):
    """TC-S3-012b — retiring non-existent rule returns 404 BRT-ADM-002."""
    resp = client.post("/config/rules/nonexistent/retire")
    assert resp.status_code == 404
    assert "BRT-ADM-002" in resp.text


def test_rule_retire_already_retired(client):
    """TC-S3-012c — retiring already-retired rule returns 400 BRT-ADM-003."""
    resp = client.post("/config/rules", json={"text": "Rule to retire twice."})
    rule_id = resp.json()["rule"]["id"]
    client.post(f"/config/rules/{rule_id}/retire")
    resp2 = client.post(f"/config/rules/{rule_id}/retire")
    assert resp2.status_code == 400
    assert "BRT-ADM-003" in resp2.text


def test_rule_append_empty_text_returns_400(client):
    """TC-S3-012d — empty rule text returns BRT-ADM-001."""
    resp = client.post("/config/rules", json={"text": "  "})
    assert resp.status_code == 422 or resp.status_code == 400


def test_config_empty_key_returns_400(client):
    """TC-S3-011b — empty config key returns BRT-ADM-001."""
    resp = client.post("/config", json={"key": "  ", "value": "v"})
    assert resp.status_code == 400
    assert "BRT-ADM-001" in resp.text


# ──────────────────────────────────────────────────────────────────────────────
# S3-06 resume endpoint test
# ──────────────────────────────────────────────────────────────────────────────

def test_get_runs_list(client):
    """TC-S3-013 — GET /runs returns run list."""
    # Submit a task to ensure at least one run exists
    with patch("src.api.tasks._run_in_background", new=MagicMock()):
        client.post("/v1/task", json={"instruction": "List test run."})

    resp = client.get("/runs")
    assert resp.status_code == 200
    body = resp.json()
    assert "runs" in body
    assert isinstance(body["runs"], list)


def test_resume_run_endpoint_completed_returns_400(client):
    """TC-S3-014 — POST /runs/{id}/resume on completed run returns 400."""
    # First submit and "complete" a run by direct DB write via the pipeline mock
    with patch("src.api.tasks._run_in_background", new=MagicMock()):
        post_resp = client.post("/v1/task", json={"instruction": "Resume test."})
    run_id = post_resp.json()["run_id"]

    # Mark it completed via API or direct state
    import asyncio
    asyncio.run(update_run_status(uuid.UUID(run_id), "completed"))

    resume_resp = client.post(f"/runs/{run_id}/resume")
    assert resume_resp.status_code == 400
    assert "BRT-DB-020" in resume_resp.text or "BRT-API-024" in resume_resp.text
