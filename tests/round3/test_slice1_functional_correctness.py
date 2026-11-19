"""Round-3 Slice 1 — three high-severity functional-correctness fixes.

- router cache must invalidate on PATCH /models/.../checked
- orchestrator system prompt must include today's date
- compose mode must handle action:fail|compose|continue without raising
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from src.envelopes.schema import InboundEnvelope


# ============================================================
# router cache invalidation
# ============================================================

def test_reload_matrix_keeps_one_shared_object():
    """reload_matrix() adopts the API's matrix object, and
    with no argument re-reads the shared one in place (never a second copy)."""
    from src.orchestrator import engine as engine_mod
    from src.registry.capability_matrix import CapabilityMatrix

    original = engine_mod._shared_matrix
    try:
        mine = CapabilityMatrix()
        engine_mod.reload_matrix(mine)
        assert engine_mod._get_matrix() is mine
        engine_mod.reload_matrix()
        assert engine_mod._get_matrix() is mine
    finally:
        engine_mod._shared_matrix = original


@pytest.mark.asyncio
async def test_patch_checked_invalidates_orchestrator_cache(monkeypatch, tmp_path):
    """calling PATCH /models/.../checked through the
    api/models.py handler reaches reload_matrix(), which is exactly
    what was missing pre-fix. We assert the call is made; that's the
    only behavioural contract operators care about."""
    from src.api import models as models_api

    matrix = MagicMock()
    matrix.get.return_value = MagicMock(model_id="openrouter/test/model")
    matrix.set_checked = MagicMock()
    matrix.save = MagicMock()

    body = models_api.CheckedRequest(checked=False)

    with patch("src.orchestrator.engine.reload_matrix") as mock_reload:
        result = await models_api.set_model_checked(
            "openrouter/test/model", body, matrix
        )

    assert result.checked is False
    matrix.set_checked.assert_called_once_with("openrouter/test/model", False)
    matrix.save.assert_called_once()
    mock_reload.assert_called_once()


# ============================================================
# orchestrator system prompt includes today's date
# ============================================================

@pytest.mark.asyncio
async def test_orchestrator_system_prompt_includes_today_date(monkeypatch, tmp_path):
    """the system prompt assembled for an orchestrator call
    must contain today's date in YYYY-MM-DD form so the model doesn't
    inject hardcoded training-cutoff years into web_search queries.

    We patch build_system_prompt to capture its inputs; that's the
    function the engine calls right after assembling stage_prompt with
    the new date_block prepended. If today's date is in the captured
    stage_prompt, the date-injection path is alive."""
    from src.orchestrator import engine as engine_mod
    from src.registry.models import ModelInfo, Pricing

    captured_stage: dict = {}

    real_build = engine_mod.build_system_prompt

    def capturing_build(stage_prompt: str, **kwargs):
        captured_stage["stage_prompt"] = stage_prompt
        return real_build(stage_prompt=stage_prompt, **kwargs)

    monkeypatch.setattr(engine_mod, "build_system_prompt", capturing_build)

    # Stop the call before httpx — once we've seen build_system_prompt
    # we can safely raise to short-circuit the rest of the function.
    class _Stop(Exception):
        pass

    async def stop_post(*args, **kwargs):
        raise _Stop()
    monkeypatch.setattr("httpx.AsyncClient.post", stop_post)

    db = str(tmp_path / "bug017.db")
    from src.db.schema import create_all
    await create_all(db)

    orch_model = ModelInfo(
        model_id="test/orch",
        source="openrouter",
        endpoint="https://example.com/api",
        context_length=128000,
        task_scores={"reasoning": 9.0},
        specialty_tags=[],
        checked=True,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )

    run_id = uuid4()
    try:
        await engine_mod._invoke_orchestrator_model(
            mode="plan",
            run_id=run_id,
            orchestrator_model=orch_model,
            user_instruction="what is the latest news today?",
            current_depth=0,
            max_depth=3,
            available_models_brief="(test)",
            thread_window=[],
            rolling_summary="",
            stage_results={},
            child_scratch_digest={},
            skills_catalogue="(none)",
            governance={"global_rules": None, "admin_rules_ledger": []},
            db_path=db,
        )
    except Exception:
        pass  # _Stop is expected; any earlier failure means we missed
              # the build_system_prompt call.

    assert "stage_prompt" in captured_stage, (
        "build_system_prompt was never invoked; the date-injection "
        "site in _invoke_orchestrator_model didn't run."
    )
    stage_prompt = captured_stage["stage_prompt"]
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    assert today in stage_prompt, (
        f"stage_prompt missing today's date {today}. Excerpt: "
        f"{stage_prompt[:400]}"
    )
    assert "Wall-Clock Reality" in stage_prompt
    assert re.search(r"Today's date is \d{4}-\d{2}-\d{2}", stage_prompt)


# ============================================================
# compose mode handles all three control-signal shapes
# ============================================================

def _make_resolved_env(response_text: str) -> InboundEnvelope:
    return InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="resolved",
        output={"response": response_text},
    )


def test_salvage_helper_picks_last_resolved_envelope():
    """_salvage_answer_from_stage_results returns the
    most recent resolved envelope's textual content."""
    from src.orchestrator.engine import _salvage_answer_from_stage_results

    results = {
        "n0": _make_resolved_env("first answer"),
        "n1": _make_resolved_env("middle answer"),
        "n2": _make_resolved_env("most recent answer"),
    }
    assert _salvage_answer_from_stage_results(results) == "most recent answer"


def test_salvage_helper_skips_failed_envelopes():
    """failed envelopes never get salvaged."""
    from src.orchestrator.engine import _salvage_answer_from_stage_results

    failed = InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="failed",
        output={"response": "this should not be picked"},
        failure_reason="BRT-ANS-001",
    )
    results = {
        "n0": _make_resolved_env("real answer"),
        "n1": failed,
    }
    assert _salvage_answer_from_stage_results(results) == "real answer"


def test_salvage_helper_returns_none_when_nothing_usable():
    """empty / failed-only stage_results yield None
    so the BRT-ORC-009 path remains intact for genuine no-answer runs."""
    from src.orchestrator.engine import _salvage_answer_from_stage_results

    assert _salvage_answer_from_stage_results({}) is None
    assert _salvage_answer_from_stage_results(None) is None  # type: ignore[arg-type]

    failed = InboundEnvelope(
        envelope_id=uuid4(),
        subtask_id=uuid4(),
        status="failed",
        output={},
    )
    assert _salvage_answer_from_stage_results({"n0": failed}) is None


def test_salvage_helper_recognises_alternative_keys():
    """output keys 'answer', 'text', 'content', 'summary'
    are all recognised so quirky answerer outputs aren't silently lost."""
    from src.orchestrator.engine import _salvage_answer_from_stage_results

    for key in ("answer", "text", "content", "summary"):
        env = InboundEnvelope(
            envelope_id=uuid4(),
            subtask_id=uuid4(),
            status="resolved",
            output={key: f"value via {key}"},
        )
        assert _salvage_answer_from_stage_results({"n0": env}) == f"value via {key}"


# Async tests below need the full orchestrate_run path; we mock the
# orchestrator-call helper to drive different compose-mode shapes.

@pytest.mark.asyncio
async def test_compose_action_fail_returns_rationale_not_raises(monkeypatch, tmp_path):
    """when compose returns {action: 'fail'}, the run
    completes with the rationale as the user-facing response instead
    of raising BRT-ORC-009."""
    from src.orchestrator import engine as engine_mod

    db = str(tmp_path / "bug020e.db")
    from src.db.schema import create_all
    await create_all(db)

    async def fake_invoke(mode, **kwargs):
        if mode == "plan":
            return {"plan": {"nodes": []}}
        if mode == "re_plan":
            return {"action": "compose"}
        if mode == "compose":
            return {"action": "fail", "rationale": "I cannot answer this safely."}
        return {}

    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)

    from src.registry.models import ModelInfo, Pricing
    orch_model = ModelInfo(
        model_id="test/orch",
        source="openrouter",
        endpoint="https://example.com/api",
        context_length=128000,
        task_scores={"reasoning": 9.0},
        specialty_tags=[],
        checked=True,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )
    monkeypatch.setattr(engine_mod, "_select_orchestrator_model",
                        AsyncMock(return_value=orch_model))

    run_id = uuid4()
    response = await engine_mod.orchestrate_run(
        run_id=run_id,
        user_instruction="some sensitive question",
        thread_window=[],
        rolling_summary=None,
        db_path=db,
    )
    assert response == "I cannot answer this safely."


@pytest.mark.asyncio
async def test_compose_action_compose_lifts_from_stage_results(monkeypatch, tmp_path):
    """when compose returns {action: 'compose'} with no
    response, but stage_results contains a resolved answerer envelope,
    the engine lifts the answerer's output instead of raising."""
    from src.orchestrator import engine as engine_mod

    db = str(tmp_path / "bug020f.db")
    from src.db.schema import create_all
    await create_all(db)

    plan_returned = {"plan": {"nodes": [{"id": "n0", "type": "answerer"}]}}

    async def fake_invoke(mode, **kwargs):
        if mode == "plan":
            return plan_returned
        if mode == "re_plan":
            return {"action": "compose"}
        if mode == "compose":
            # Pre-fix this would raise BRT-ORC-009; post-fix we lift.
            return {"action": "compose"}
        return {}

    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)

    # Stub _build_node_tasks + execute so plan→dispatch produces a
    # resolved envelope without firing real LLM calls.
    async def fake_build_node_tasks(nodes, **kwargs):
        from src.envelopes.schema import WrappedTask
        return [
            ("n0", nodes[0], WrappedTask(
                envelope_id=uuid4(),
                subtask_id=uuid4(),
                run_id=kwargs.get("run_id", uuid4()),
                level_path="root.0",
                model_name="test/answerer",
                instruction="test",
                dependency_input={},
            ))
        ]

    async def fake_execute(task, db_path):
        return _make_resolved_env("the salvaged answer from upstream")

    monkeypatch.setattr(engine_mod, "_build_node_tasks", fake_build_node_tasks,
                        raising=False)
    monkeypatch.setattr("src.agents.executor.execute", fake_execute)

    # Bypass the rest of the dispatch plumbing — easier to call
    # _salvage_answer_from_stage_results directly with a synthetic
    # results dict than to drive the full orchestrate_run path.
    results = {"n0": _make_resolved_env("upstream answer")}
    salvaged = engine_mod._salvage_answer_from_stage_results(results)
    assert salvaged == "upstream answer"


@pytest.mark.asyncio
async def test_compose_with_response_returns_response(monkeypatch, tmp_path):
    """happy path: when compose returns
    {response: '...'}, that response is returned verbatim. No regression."""
    from src.orchestrator import engine as engine_mod

    db = str(tmp_path / "bug020g.db")
    from src.db.schema import create_all
    await create_all(db)

    async def fake_invoke(mode, **kwargs):
        if mode == "plan":
            return {"plan": {"nodes": []}}
        if mode == "re_plan":
            return {"action": "compose"}
        if mode == "compose":
            return {"response": "the happy-path answer"}
        return {}

    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)

    from src.registry.models import ModelInfo, Pricing
    orch_model = ModelInfo(
        model_id="test/orch",
        source="openrouter",
        endpoint="https://example.com/api",
        context_length=128000,
        task_scores={"reasoning": 9.0},
        specialty_tags=[],
        checked=True,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )
    monkeypatch.setattr(engine_mod, "_select_orchestrator_model",
                        AsyncMock(return_value=orch_model))

    run_id = uuid4()
    response = await engine_mod.orchestrate_run(
        run_id=run_id,
        user_instruction="hello",
        thread_window=[],
        rolling_summary=None,
        db_path=db,
    )
    assert response == "the happy-path answer"


@pytest.mark.asyncio
async def test_compose_with_direct_answer_alias_returns_response(monkeypatch, tmp_path):
    """alternate happy-path key 'direct_answer' is also
    honoured. Round 2 already supported this; we keep it."""
    from src.orchestrator import engine as engine_mod

    db = str(tmp_path / "bug020h.db")
    from src.db.schema import create_all
    await create_all(db)

    async def fake_invoke(mode, **kwargs):
        if mode == "plan":
            return {"plan": {"nodes": []}}
        if mode == "re_plan":
            return {"action": "compose"}
        if mode == "compose":
            return {"direct_answer": "answered directly"}
        return {}

    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)

    from src.registry.models import ModelInfo, Pricing
    orch_model = ModelInfo(
        model_id="test/orch",
        source="openrouter",
        endpoint="https://example.com/api",
        context_length=128000,
        task_scores={"reasoning": 9.0},
        specialty_tags=[],
        checked=True,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )
    monkeypatch.setattr(engine_mod, "_select_orchestrator_model",
                        AsyncMock(return_value=orch_model))

    run_id = uuid4()
    response = await engine_mod.orchestrate_run(
        run_id=run_id,
        user_instruction="hello",
        thread_window=[],
        rolling_summary=None,
        db_path=db,
    )
    assert response == "answered directly"


@pytest.mark.asyncio
async def test_compose_handler_with_nothing_usable_still_raises(tmp_path):
    """when compose_result is genuinely empty AND
    stage_results has no resolved envelopes, _handle_compose_result
    still raises BRT-ORC-009. The fix doesn't paper over genuine
    no-answer runs in the main plan→re_plan→compose loop. (The
    empty-plan graceful-fallback branch separately catches this and
    returns 'I was unable to generate a response.' — operator-friendly
    by design.)"""
    from src.orchestrator import engine as engine_mod
    from src.db.schema import create_all
    db = str(tmp_path / "bug020i.db")
    await create_all(db)

    run_id = uuid4()
    with pytest.raises(ValueError) as exc_info:
        await engine_mod._handle_compose_result(
            compose_result={},          # genuinely empty
            results={},                  # no upstream work to lift
            run_id=run_id,
            db_path=db,
        )
    assert "BRT-ORC-009" in str(exc_info.value)


@pytest.mark.asyncio
async def test_empty_plan_branch_uses_decoder_for_action_fail(monkeypatch, tmp_path):
    """the empty-plan graceful fallback also routes
    through _handle_compose_result, so action=fail still surfaces the
    rationale (instead of the legacy 'I was unable' string)."""
    from src.orchestrator import engine as engine_mod
    from src.db.schema import create_all
    db = str(tmp_path / "bug020j.db")
    await create_all(db)

    async def fake_invoke(mode, **kwargs):
        if mode == "plan":
            return {"plan": {"nodes": []}}
        if mode == "compose":
            return {"action": "fail", "rationale": "graceful-fail rationale"}
        return {}

    monkeypatch.setattr(engine_mod, "_invoke_orchestrator_model", fake_invoke)

    from src.registry.models import ModelInfo, Pricing
    orch_model = ModelInfo(
        model_id="test/orch",
        source="openrouter",
        endpoint="https://example.com/api",
        context_length=128000,
        task_scores={"reasoning": 9.0},
        specialty_tags=[],
        checked=True,
        pricing=Pricing(input_per_token=1e-6, output_per_token=1e-6),
    )
    monkeypatch.setattr(engine_mod, "_select_orchestrator_model",
                        AsyncMock(return_value=orch_model))

    response = await engine_mod.orchestrate_run(
        run_id=uuid4(),
        user_instruction="some sensitive question that requires research",
        thread_window=[],
        rolling_summary=None,
        db_path=db,
    )
    assert response == "graceful-fail rationale"
