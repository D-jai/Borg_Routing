"""Planner ruling 28 (a): a top-level run probes ticked unmeasured local models at its start; a child
run does not. The run is stopped right after the probe point (no model is called)."""
import uuid

import pytest

from src.orchestrator import engine


class _Stop(Exception):
    pass


def _stop_after_probe(monkeypatch):
    real = engine.get_config

    async def get_config(key, *a, **k):
        if key == "time_cost_per_sec":       # read just after the run-start probe
            raise _Stop()
        return await real(key, *a, **k)

    monkeypatch.setattr(engine, "get_config", get_config)
    calls = []

    async def probe(matrix, run_id=None, db_path=None):
        calls.append(run_id)

    monkeypatch.setattr(engine, "_measure_local_speeds", probe)
    return calls


async def _run(depth):
    try:
        await engine.orchestrate_run(uuid.uuid4(), "say hi", current_depth=depth,
                                     **({"root_instruction": "say hi"} if depth else {}))
    except _Stop:
        return
    raise AssertionError("the run went past the probe point")


@pytest.mark.asyncio
async def test_a_top_level_run_probes_local_speeds_at_its_start(monkeypatch):
    calls = _stop_after_probe(monkeypatch)
    await _run(0)
    assert len(calls) == 1 and calls[0] is not None      # with the run id, for the BRT-REG-016 event


@pytest.mark.asyncio
async def test_a_child_run_does_not_probe(monkeypatch):
    calls = _stop_after_probe(monkeypatch)
    await _run(1)
    assert calls == []


@pytest.mark.asyncio
async def test_a_failed_run_start_probe_writes_a_brt_reg_016_event(monkeypatch):
    from src.registry import throughput
    from src.registry.capability_matrix import CapabilityMatrix
    from src.registry.models import ModelInfo

    m = CapabilityMatrix(path="__test_unused__.yaml")
    m.upsert(ModelInfo(model_id="ollama/llama3.2:3b", source="ollama", endpoint="http://localhost:11434",
                       context_length=8192, checked=True))
    events = []

    async def write_event(event_type, **kw):
        events.append((event_type, kw))

    monkeypatch.setattr(engine, "write_event", write_event)
    run_id = uuid.uuid4()
    await engine._measure_local_speeds(m, run_id=run_id, db_path="x.db")  # conftest blocks the transport
    assert len(events) == 1
    event_type, kw = events[0]
    assert event_type == "error" and kw["run_id"] == run_id and kw["model_id"] == "ollama/llama3.2:3b"
    assert kw["decision"]["code"] == "BRT-REG-016" and kw["decision"]["model_id"] == "ollama/llama3.2:3b"
    assert throughput.failure_of("ollama/llama3.2:3b")
