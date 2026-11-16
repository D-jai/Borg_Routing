"""R5: the chat alias reaches the pipeline as run-level routing options."""
import pytest
from fastapi.testclient import TestClient

from main import app
from src.api.chat_completions import _route_options_for_alias


@pytest.mark.parametrize("alias, expected", [
    ("borg_route", None),
    ("borg_route:light", {"alias": "borg_route:light", "tier": "light"}),
    ("borg_route:mid", {"alias": "borg_route:mid", "tier": "mid"}),
    ("borg_route:heavy", {"alias": "borg_route:heavy", "tier": "heavy"}),
    ("borg_route:coding", {"alias": "borg_route:coding", "task_type": "coding"}),
    ("borg_route:creative", {"alias": "borg_route:creative", "task_type": "creative"}),
    ("borg_route:local", {"alias": "borg_route:local", "strategy": "LOCAL_FIRST"}),
    ("borg_route:nonsense", None),
])
def test_alias_to_route_options(alias, expected):
    assert _route_options_for_alias(alias) == expected


@pytest.mark.parametrize("stream", [False, True])
def test_endpoint_hands_the_alias_options_to_the_pipeline(monkeypatch, stream):
    seen = {}

    async def fake_pipeline(user_instruction, thread_window=None, **kwargs):
        seen.update(kwargs)
        return {"run_id": "rid", "response": "ok", "status": "completed",
                "latency_ms": 1, "envelope_count": 0, "error": None}

    async def noop(*a, **k):
        return None

    monkeypatch.setattr("src.api.chat_completions.run_pipeline", fake_pipeline)
    monkeypatch.setattr("src.api.chat_completions._ensure_db", noop)
    resp = TestClient(app).post("/v1/chat/completions", json={
        "model": "borg_route:heavy", "stream": stream,
        "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    assert seen.get("route_options") == {"alias": "borg_route:heavy", "tier": "heavy"}
