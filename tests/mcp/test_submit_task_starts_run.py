"""R9: MCP borg_route.submit_task must start the run it creates."""
import asyncio

import pytest

import src.db.schema as schema
from src.db.schema import create_all
from src.mcp import server as mcp_server
from src.orchestrator import engine
from tests.e2e.stub_model_server import StubModels, drain_log_writes, three_tier_matrix

PLAN = [
    {"type": "answerer", "id": "n1", "instruction": "Step one text.",
     "task_type": "extraction", "tier": "heavy", "output_schema": {"result": "string"}},
]


async def _call(arguments: dict) -> dict:
    # Direct call: the stub model patches httpx, so an httpx ASGI client would be stubbed too.
    return {"result": await mcp_server._tool_submit_task(arguments)}


async def _wait_for_end(run_id: str, db_path: str) -> dict:
    import uuid
    from src.db.state import get_run
    for _ in range(100):
        run = await get_run(uuid.UUID(run_id), db_path=db_path)
        if run["status"] in ("completed", "failed"):
            break
        await asyncio.sleep(0.1)
    await asyncio.gather(*list(mcp_server._BACKGROUND_RUNS), return_exceptions=True)
    await drain_log_writes()
    return await get_run(uuid.UUID(run_id), db_path=db_path)


@pytest.mark.asyncio
async def test_submit_task_run_reaches_completed(tmp_path, monkeypatch):
    db_path = str(tmp_path / "mcp.db")
    await create_all(db_path)
    monkeypatch.setattr(schema, "DB_PATH", db_path)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-mcp")
    monkeypatch.setenv("BORG_RULE_ZERO_SHORTCUT", "false")
    monkeypatch.setattr(engine, "_shared_matrix", three_tier_matrix())
    StubModels(plan_nodes=PLAN).install(monkeypatch)

    resp = await _call({"instruction": "Do one thing."})
    run_id = resp["result"]["_meta"]["run_id"]
    assert resp["result"]["_meta"]["status"] == "running"

    run = await _wait_for_end(run_id, db_path)
    assert run["status"] == "completed", run.get("failure_reason")


@pytest.mark.asyncio
async def test_submit_task_failed_run_records_reason(tmp_path, monkeypatch):
    db_path = str(tmp_path / "mcp_fail.db")
    await create_all(db_path)
    monkeypatch.setattr(schema, "DB_PATH", db_path)

    async def boom(**kwargs):
        raise RuntimeError("stub orchestration failure")

    monkeypatch.setattr(engine, "orchestrate_run", boom)

    resp = await _call({"instruction": "Do one thing."})
    run_id = resp["result"]["_meta"]["run_id"]

    run = await _wait_for_end(run_id, db_path)
    assert run["status"] == "failed"
    assert "stub orchestration failure" in (run.get("failure_reason") or "")


@pytest.mark.asyncio
async def test_submit_task_without_instruction_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(schema, "DB_PATH", str(tmp_path / "unused.db"))
    with pytest.raises(ValueError, match="BRT-API-034"):
        await _call({})
    assert not mcp_server._BACKGROUND_RUNS
