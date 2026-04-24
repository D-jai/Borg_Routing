"""Main orchestration loop — the brain of BORG_ROUTE.

orchestrate_run() is the entry point. It drives the plan → dispatch →
re_plan → compose cycle using the orchestrator model.

Error codes:
- BRT-ORC-001: orchestrator model call failed
- BRT-ORC-002: orchestrator returned unparseable JSON
- BRT-ORC-003: plan is empty or malformed
- BRT-ORC-004: node dispatch failed (individual; the run continues)
- BRT-ORC-005: maximum loop count exceeded (safety ceiling)
- BRT-ORC-006: orchestrator returned action=fail
- BRT-ORC-007: no orchestrator model available
- BRT-ORC-007-DEGRADED: quality bar relaxed below the default because no model qualified
- BRT-ORC-008: node builder failed for a plan node
- BRT-ORC-009: compose mode returned no response
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Optional
from uuid import UUID, uuid4

import httpx

logger = logging.getLogger(__name__)

from src.agents.executor import execute
from src.db.scratch import count_scratch_for_level
from src.db.state import (
    create_run,
    get_config,
    save_envelope,
    update_envelope_inbound,
    update_run_status,
)
from src.envelopes.schema import InboundEnvelope, WrappedTask
from src.logging.writer import write_event
from src.orchestrator.fold_in import fold_in
from src.prompts.base import build_system_prompt, load_governance
from src.prompts.orchestrator import ORCHESTRATOR_SYSTEM_PROMPT
from src.registry.capability_matrix import CapabilityMatrix
from src.registry.model_call import resolve_model_call
from src.registry.models import ModelInfo
from src.registry.router import select_model
from src.skills.library import SkillLibrary

MAX_LOOPS = 10
MAX_DEPTH = 5
_MODEL_CALL_TIMEOUT = 180
_MATRIX_PATH = "data/capability_matrix.yaml"
_SKILLS_PATH = "data/skills.yaml"

_shared_matrix: Optional[CapabilityMatrix] = None
_shared_skill_lib: Optional[SkillLibrary] = None


def _get_matrix() -> CapabilityMatrix:
    global _shared_matrix
    if _shared_matrix is None:
        m = CapabilityMatrix()
        try:
            m.load(_MATRIX_PATH)
        except Exception:
            pass
        _shared_matrix = m
    return _shared_matrix


def _get_skill_lib() -> SkillLibrary:
    global _shared_skill_lib
    if _shared_skill_lib is None:
        sl = SkillLibrary()
        try:
            sl.load(_SKILLS_PATH)
        except Exception:
            pass
        _shared_skill_lib = sl
    return _shared_skill_lib


def _extract_json(text: str) -> dict:
    """Extract first JSON object from text."""
    try:
        return json.loads(text.strip())
    except json.JSONDecodeError:
        pass
    fence_m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence_m:
        try:
            return json.loads(fence_m.group(1))
        except json.JSONDecodeError:
            pass
    brace_m = re.search(r"\{.*\}", text, re.DOTALL)
    if brace_m:
        try:
            return json.loads(brace_m.group(0))
        except json.JSONDecodeError:
            pass
    raise ValueError("BRT-ORC-002: no valid JSON in orchestrator response")


def _safe_sub(template: str, subs: dict) -> str:
    """Safe placeholder substitution — avoids KeyError from JSON {} in prompt."""
    result = template
    for key, value in subs.items():
        if isinstance(value, (dict, list)):
            value_str = json.dumps(value)
        else:
            value_str = str(value)
        result = result.replace("{" + key + "}", value_str)
    return result


def _pinned_model(endpoint: str, name: str) -> ModelInfo:
    """Wrap a pinned-config endpoint/name in a synthetic ModelInfo so the
    resolve_model_call helper can handle it uniformly with matrix rows."""
    return ModelInfo(
        model_id=f"lmstudio/{name}" if "/" not in name else name,
        source="lmstudio",
        endpoint=endpoint,
        context_length=4096,
    )


async def _select_orchestrator_model(
    task_type: str,
    db_path: str,
    quality_requirement: float = 6.5,
    time_cost_per_sec: Optional[float] = None,
) -> ModelInfo:
    """Select the orchestrator ModelInfo row.

    Returns: ModelInfo

    Raises:
        ValueError: BRT-ORC-007 if no model available.
    """
    # Check config for a pinned orchestrator endpoint
    pinned = await get_config("orchestrator_endpoint", db_path=db_path)
    if pinned:
        name = await get_config("orchestrator_model_name", db_path=db_path) or "orchestrator"
        return _pinned_model(pinned, name)

    # Try to select from capability matrix
    matrix = _get_matrix()
    select_kwargs: dict = {}
    if time_cost_per_sec is not None:
        select_kwargs["time_cost_per_sec"] = time_cost_per_sec
    try:
        return select_model(
            matrix=matrix,
            task_type=task_type,
            quality_requirement=quality_requirement,
            **select_kwargs,
        )
    except Exception:
        pass

    # Degraded fallback: if no checked model meets the quality bar, retry at
    # 5.1 — just above the default 5.0 unresearched score, so rows that have
    # genuinely been scored even weakly are preferred over flat defaults.
    try:
        model = select_model(
            matrix=matrix,
            task_type=task_type,
            quality_requirement=5.1,
            **select_kwargs,
        )
        logger.warning(
            "BRT-ORC-007-DEGRADED: no model met quality_requirement=%s for "
            "task_type='%s'; falling back to '%s' at its current score. "
            "Run Get Model Info to refresh scores.",
            quality_requirement, task_type, model.model_id,
        )
        return model
    except Exception:
        pass

    raise ValueError(
        "BRT-ORC-007: no orchestrator model available. "
        "Check that at least one model is checked on the Models page, "
        "or set orchestrator_endpoint in config."
    )


async def _invoke_orchestrator_model(
    mode: str,
    run_id: UUID,
    user_instruction: str,
    current_depth: int,
    max_depth: int,
    thread_window: list[dict],
    rolling_summary: str,
    stage_results: dict,
    child_scratch_digest: dict,
    skills_catalogue: str,
    governance: dict,
    available_models_brief: str,
    orchestrator_model: ModelInfo,
    db_path: str,
) -> dict:
    """One call to the orchestrator model. Returns parsed JSON dict.

    Raises:
        ValueError: BRT-ORC-001 on HTTP error.
        ValueError: BRT-ORC-002 on JSON parse failure.
    """
    stage_results_str = json.dumps(
        {k: v.output for k, v in stage_results.items()}, indent=2
    ) if stage_results else "(none)"

    subs = {
        "current_mode": mode,
        "run_id": str(run_id),
        "current_depth": str(current_depth),
        "max_depth": str(max_depth),
        "available_models_brief": available_models_brief,
        "thread_window": json.dumps(thread_window, indent=2),
        "rolling_summary": rolling_summary or "(none)",
        "stage_results": stage_results_str,
        "child_scratch_digest": json.dumps(child_scratch_digest) if child_scratch_digest else "(none)",
        "context_budget_hint": "(unknown — use cascading distillation if context feels dense)",
        "skills_catalogue": skills_catalogue,
    }

    stage_prompt = _safe_sub(ORCHESTRATOR_SYSTEM_PROMPT, subs)
    system_prompt = build_system_prompt(
        stage_prompt=stage_prompt,
        global_rules=governance["global_rules"],
        admin_rules_ledger=governance["admin_rules_ledger"],
        user_instruction=user_instruction,
    )

    url, headers, body_model = resolve_model_call(orchestrator_model)
    max_tokens = int(os.getenv("ORCHESTRATOR_MAX_TOKENS", "2000"))

    payload = {
        "model": body_model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_instruction if mode == "plan" else "continue"},
        ],
        "temperature": 0.1,
        "max_tokens": max_tokens,
    }

    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_MODEL_CALL_TIMEOUT) as client:
            resp = await client.post(url, json=payload, headers=headers)
        resp.raise_for_status()
    except httpx.TimeoutException as exc:
        raise ValueError(f"BRT-ORC-001: orchestrator call timed out") from exc
    except httpx.HTTPStatusError as exc:
        raise ValueError(
            f"BRT-ORC-001: HTTP {exc.response.status_code} from orchestrator"
        ) from exc
    except httpx.RequestError as exc:
        raise ValueError(f"BRT-ORC-001: request error calling orchestrator: {exc}") from exc

    latency_ms = int((time.monotonic() - t0) * 1000)
    body = resp.json()
    content = body["choices"][0]["message"]["content"]
    usage = body.get("usage", {})
    tokens_in = usage.get("prompt_tokens", 0)
    tokens_out = usage.get("completion_tokens", 0)

    parsed = _extract_json(content)

    # Log orchestrator_decision event
    await write_event(
        "orchestrator_decision",
        run_id=run_id,
        model_id=orchestrator_model.model_id,
        tokens_input=tokens_in,
        tokens_output=tokens_out,
        latency_ms=latency_ms,
        decision={"mode": mode, "parsed": parsed, "rationale": parsed.get("rationale", "")},
        payload_in=system_prompt[:2000],
        payload_out=content[:2000],
        db_path=db_path,
    )

    return parsed


def _build_task_from_node(
    node: dict,
    run_id: UUID,
    current_depth: int,
    max_depth: int,
    level_path: str,
    model_endpoint: str,
    model_name: str,
    user_instruction: str,
) -> WrappedTask:
    """Convert a plan node dict into a WrappedTask.

    Handles skill-based nodes (has skill_id) and custom nodes.
    For state_op nodes, packages the op/params/source into dependency_input.

    Raises:
        ValueError: BRT-ORC-008 on build failure.
    """
    node_type = node.get("type", "answerer")

    # If skill node
    if node.get("skill_id"):
        try:
            lib = _get_skill_lib()
            return lib.render(
                skill_id=node["skill_id"],
                version=node.get("skill_version", 1),
                args=node.get("skill_args", {}),
                run_id=run_id,
                current_depth=current_depth,
                max_depth=max_depth,
                level_path=level_path,
                trigger_reason=node.get("trigger_reason", "initial_plan"),
            )
        except Exception as exc:
            raise ValueError(f"BRT-ORC-008: skill render failed: {exc}") from exc

    # state_op node
    if node_type == "state_op":
        dep_input = {
            "op": node.get("op", "count"),
            "params": node.get("params", {}),
            "items": node.get("items", []),
        }
        source = node.get("source")
        if source:
            dep_input["source_ref"] = source

        return WrappedTask(
            envelope_id=uuid4(),
            run_id=run_id,
            subtask_id=uuid4(),
            type="state_op",
            current_depth=current_depth,
            max_depth=max_depth,
            level_path=level_path,
            instruction=f"Apply state_op: {node.get('op', 'count')}",
            output_schema={"result": "any"},
            dependency_input=dep_input,
            trigger_reason=node.get("trigger_reason", "initial_plan"),
        )

    # apply_to_each node
    if node_type == "apply_to_each":
        dep_input = {
            "inputs_ref": node.get("inputs_ref", ""),
            "items": node.get("items", []),
            "concurrency_limit": node.get("concurrency_limit", 20),
        }
        return WrappedTask(
            envelope_id=uuid4(),
            run_id=run_id,
            subtask_id=uuid4(),
            type="apply_to_each",
            current_depth=current_depth,
            max_depth=max_depth,
            level_path=level_path,
            model_endpoint=model_endpoint,
            model_name=model_name,
            instruction=node.get("instruction", ""),
            context=node.get("context", ""),
            output_schema=node.get("output_schema", {}),
            side_effects=node.get("side_effects", []),
            dependency_input=dep_input,
            trigger_reason=node.get("trigger_reason", "initial_plan"),
        )

    # answerer or orchestrate
    return WrappedTask(
        envelope_id=uuid4(),
        run_id=run_id,
        subtask_id=uuid4(),
        type=node_type if node_type == "orchestrate" else "answerer",
        current_depth=current_depth,
        max_depth=max_depth,
        level_path=level_path,
        model_endpoint=model_endpoint,
        model_name=model_name,
        instruction=node.get("instruction", user_instruction),
        context=node.get("context", ""),
        state_query=node.get("state_query"),
        output_schema=node.get("output_schema", {}),
        side_effects=node.get("side_effects", []),
        dependency_input=node.get("dependency_input", {}),
        trigger_reason=node.get("trigger_reason", "initial_plan"),
    )


async def orchestrate_run(
    run_id: UUID,
    user_instruction: str,
    thread_window: list[dict] = None,
    rolling_summary: str = "",
    db_path: str = "data/borg_route.db",
    current_depth: int = 0,
) -> str:
    """Main orchestration loop. Returns the final response text.

    Raises:
        ValueError: BRT-ORC-001..009 on fatal failures.
    """
    if thread_window is None:
        thread_window = []

    governance = await load_governance(lambda key: get_config(key, db_path=db_path))

    # Read routing knobs from config (falls back to env, then hardcoded defaults).
    # BRT-RT-01 lowers the orchestrator quality bar from 8.0 → 6.5 so mid-tier
    # local models (e.g. lmstudio/gemma-4-26b-a4b at reasoning=6.8) are eligible.
    configured_quality = await get_config("orchestrator_quality_requirement", db_path=db_path)
    quality_req = float(
        configured_quality if configured_quality is not None
        else os.getenv("ORCHESTRATOR_QUALITY_REQUIREMENT", "6.5")
    )
    configured_time_cost = await get_config("time_cost_per_sec", db_path=db_path)
    time_cost = float(
        configured_time_cost if configured_time_cost is not None
        else os.getenv("TIME_COST_PER_SEC", "0.0001")
    )

    try:
        orch_model = await _select_orchestrator_model(
            "reasoning",
            db_path=db_path,
            quality_requirement=quality_req,
            time_cost_per_sec=time_cost,
        )
    except ValueError:
        orch_model = _pinned_model(endpoint="", name="unavailable")

    skill_lib = _get_skill_lib()
    skills_catalogue = skill_lib.catalogue_for_prompt()

    matrix = _get_matrix()
    available_models_brief = ", ".join(
        m.model_id for m in matrix.all()[:5]
    ) or "(none discovered)"

    base_kwargs = dict(
        run_id=run_id,
        user_instruction=user_instruction,
        current_depth=current_depth,
        max_depth=MAX_DEPTH,
        thread_window=thread_window,
        rolling_summary=rolling_summary,
        skills_catalogue=skills_catalogue,
        governance=governance,
        available_models_brief=available_models_brief,
        orchestrator_model=orch_model,
        db_path=db_path,
    )

    # --- PLAN MODE ---
    plan_result = await _invoke_orchestrator_model(
        mode="plan",
        stage_results={},
        child_scratch_digest={},
        **base_kwargs,
    )

    if plan_result.get("direct_answer"):
        return plan_result["direct_answer"]

    plan = plan_result.get("plan")
    if not plan or not plan.get("nodes"):
        # Graceful fallback to compose
        compose_result = await _invoke_orchestrator_model(
            mode="compose",
            stage_results={},
            child_scratch_digest={},
            **base_kwargs,
        )
        return compose_result.get("response", "I was unable to generate a response.")

    current_nodes = plan["nodes"]
    results: dict[str, InboundEnvelope] = {}
    child_digests: dict = {}
    loop_count = 0

    # Select a default answerer model
    try:
        default_model = select_model(
            matrix=matrix,
            task_type="reasoning",
            quality_requirement=quality_req,
            time_cost_per_sec=time_cost,
        )
        answerer_endpoint = default_model.endpoint or ""
        answerer_name = default_model.model_id
    except Exception:
        answerer_endpoint = orch_model.endpoint or ""
        answerer_name = orch_model.model_id

    while loop_count < MAX_LOOPS:
        loop_count += 1

        # Find ready nodes
        completed_ids = set(results.keys())
        ready = [
            n for n in current_nodes
            if n.get("id") not in completed_ids
            and all(d in completed_ids for d in n.get("depends_on", []))
        ]

        if not ready:
            break

        # Dispatch ready nodes in parallel
        tasks_to_dispatch = []
        for i, node in enumerate(ready):
            node_id = node.get("id", f"n{i}")
            level = f"{current_depth+1}{chr(ord('a') + i % 26)}"
            try:
                task = _build_task_from_node(
                    node=node,
                    run_id=run_id,
                    current_depth=current_depth + 1,
                    max_depth=MAX_DEPTH,
                    level_path=level,
                    model_endpoint=answerer_endpoint,
                    model_name=answerer_name,
                    user_instruction=user_instruction,
                )
                tasks_to_dispatch.append((node_id, node, task))
            except Exception as exc:
                # Node build failed — record as failed result
                results[node_id] = InboundEnvelope(
                    envelope_id=uuid4(),
                    subtask_id=uuid4(),
                    status="failed",
                    output={},
                    failure_reason=str(exc),
                )

        # Save and execute in parallel
        async def _dispatch_one(node_id: str, node: dict, task: WrappedTask):
            try:
                await save_envelope(task, db_path=db_path)
            except Exception:
                pass
            try:
                env = await execute(task, db_path=db_path)
            except Exception as exc:
                env = InboundEnvelope(
                    envelope_id=task.envelope_id,
                    subtask_id=task.subtask_id,
                    status="failed",
                    output={},
                    failure_reason=f"BRT-ORC-004: {exc}",
                )
            try:
                await update_envelope_inbound(task.envelope_id, env, db_path=db_path)
            except Exception:
                pass

            # Fold-in for orchestrate nodes (Law 2)
            if node.get("type") == "orchestrate":
                digest = await fold_in(
                    run_id=run_id,
                    child_level_path=task.level_path,
                    child_model_id=task.model_name,
                    db_path=db_path,
                )
                if digest:
                    child_digests[node_id] = digest

            return node_id, env

        dispatched = await asyncio.gather(
            *[_dispatch_one(nid, n, t) for nid, n, t in tasks_to_dispatch]
        )
        for nid, env in dispatched:
            results[nid] = env

        # Check if all nodes dispatched
        all_node_ids = {n.get("id", f"n{i}") for i, n in enumerate(current_nodes)}
        if all_node_ids.issubset(set(results.keys())):
            # All nodes done — move to re_plan
            pass

        # --- RE_PLAN MODE ---
        replan_result = await _invoke_orchestrator_model(
            mode="re_plan",
            stage_results=results,
            child_scratch_digest=child_digests,
            **base_kwargs,
        )

        action = replan_result.get("action", "compose")

        if action == "compose":
            break
        elif action in ("continue", "cascade"):
            next_nodes = replan_result.get("next_nodes", [])
            if not next_nodes:
                break
            current_nodes = next_nodes
        elif action == "fail":
            rationale = replan_result.get("rationale", "No rationale provided.")
            raise ValueError(f"BRT-ORC-006: orchestrator failed the run: {rationale}")
        else:
            # Unknown action — compose anyway
            break

    if loop_count >= MAX_LOOPS:
        await write_event(
            "error",
            run_id=run_id,
            decision={"error": "BRT-ORC-005: max loop count exceeded"},
            db_path=db_path,
        )

    # --- COMPOSE MODE ---
    compose_result = await _invoke_orchestrator_model(
        mode="compose",
        stage_results=results,
        child_scratch_digest=child_digests,
        **base_kwargs,
    )

    response = compose_result.get("response")
    if not response:
        raise ValueError("BRT-ORC-009: compose mode returned no response")

    return response
