"""apply_to_each — runtime expansion of an apply_to_each WrappedTask.

The orchestrator writes ONE apply_to_each node; this module expands it to
N child answerer envelopes at runtime, runs them concurrently (bounded by
concurrency_limit), aggregates results, and returns a single InboundEnvelope.

Failure semantics (DESIGN_PRINCIPLES.md §9): best-effort.
- Successful results → results[]
- Failures → failures[]
- The apply_to_each node ALWAYS resolves (failures are data, not catastrophe).
  The orchestrator reads the aggregate and decides what to do.

Error codes:
- BRT-APE-001: items_ref not resolvable to a list
- BRT-APE-002: child envelope dispatch failed (individual failures go to failures[])
- BRT-APE-003: concurrency_limit invalid
- BRT-APE-004: max_depth would be exceeded by child envelopes
- BRT-APE-005: no items to process (empty list, a ref given as an empty
               list, or a ref naming a step that failed)
- BRT-APE-006: the step's inputs_ref / items_ref names no step of this run
               (or is neither a step id nor a list) and it has no inline
               items; failed before any model call (planner ruling 1a).
               On a raw (lane) step both are shown upward as code+doc only.
"""
import asyncio
import uuid
from typing import Optional

from src.envelopes.schema import InboundEnvelope, WrappedTask


_NAME_MAX = 40   # a step name shown in a failure text is cut to this many characters


def _short(name: object) -> str:
    return str(name)[:_NAME_MAX]


def unresolved_ref_message(name: object) -> str:
    """BRT-APE-006 failure text for a ref that names no step.

    The guidance comes first and the name last, cut to 40 characters, so the
    whole text fits the 200-character cap on what re_plan is shown.
    """
    return (
        "BRT-APE-006: a list from the user's message belongs in `items`; "
        "`inputs_ref`/`items_ref` must name a step of this run, not a parent's. "
        f"Not a step id: '{_short(name)}'"
    )


def bad_ref_type_message(key: str, value: object) -> str:
    """BRT-APE-006 failure text for a ref that is neither a string nor a list."""
    return (
        f"BRT-APE-006: `{key}` must be a step id or a list of items, got "
        f"{type(value).__name__}; this step has no items and made no model call."
    )


def empty_ref_list_message(key: str) -> str:
    """BRT-APE-005 failure text for a ref given as an empty list."""
    return (
        f"BRT-APE-005: `{key}` is an empty list, so this step has no items and "
        f"made no model call."
    )


def failed_ref_message(name: object) -> str:
    """BRT-APE-005 failure text for a ref naming a step that failed."""
    return (
        "BRT-APE-005: the step this one takes its items from failed, so it has "
        f"no items and made no model call. Step: '{_short(name)}'"
    )


def _resolve_items(task: WrappedTask) -> list:
    """Resolve dependency_input to a list of items.

    Checks dependency_input['items'] then dependency_input['inputs_ref'].
    Empty lists raise BRT-APE-005 so phantom zero-item fan-outs surface in
    the run trace instead of silently resolving to a no-op.

    Raises:
        ValueError: BRT-APE-001 if items cannot be resolved.
        ValueError: BRT-APE-005 if the resolved items list is empty.
        ValueError: BRT-APE-006 if the step's ref names no step and it has
            no items of its own.
    """
    ref_failure = task.dependency_input.get("ref_failure")
    if ref_failure and not task.dependency_input.get("items"):
        raise ValueError(str(ref_failure))
    unresolved = task.dependency_input.get("unresolved_ref")
    if unresolved and not task.dependency_input.get("items"):
        raise ValueError(unresolved_ref_message(unresolved))

    # Direct items list (preferred — already resolved by state_op, or by
    # _build_task_from_node when inputs_ref pointed at a prior node).
    items = task.dependency_input.get("items")
    if items is not None:
        if not isinstance(items, list):
            raise ValueError(
                f"BRT-APE-001: dependency_input['items'] must be a list, "
                f"got {type(items).__name__} for envelope {task.envelope_id}"
            )
        if not items:
            # Only signal BRT-APE-005 if no alternate list shape is present
            inputs_ref_inline = task.dependency_input.get("inputs_ref")
            if not (isinstance(inputs_ref_inline, list) and inputs_ref_inline):
                raise ValueError(
                    f"BRT-APE-005: no items to process for apply_to_each "
                    f"envelope {task.envelope_id}. Check that inputs_ref "
                    f"points at an upstream node with a non-empty items/"
                    f"results list, or populate node['items'] directly."
                )
            # inputs_ref given as a list is the items (ruling 1a review).
            return inputs_ref_inline
        return items

    # inputs_ref as a list of strings (simple inline items)
    inputs_ref = task.dependency_input.get("inputs_ref")
    if isinstance(inputs_ref, list):
        if not inputs_ref:
            raise ValueError(
                f"BRT-APE-005: no items to process for apply_to_each "
                f"envelope {task.envelope_id}. inputs_ref list is empty."
            )
        return inputs_ref

    raise ValueError(
        f"BRT-APE-001: cannot resolve items for apply_to_each envelope "
        f"{task.envelope_id}. dependency_input must contain 'items' (list) "
        f"or 'inputs_ref' (list)."
    )


def _make_child_task(
    parent: WrappedTask,
    item: object,
    index: int,
    level_suffix: str,
) -> WrappedTask:
    """Create a child answerer WrappedTask for one item.

    On-premise lane: a child inherits its parent's ``data_scope``. An item
    that is a document (part) id becomes the child's ``documents``; its
    context names the id only, and the executor loads the text.
    """
    if isinstance(item, dict):
        item_context = str(item)
    else:
        item_context = str(item)

    documents: list[str] = []
    if parent.data_scope == "raw":
        from src.lane.context import current_lane  # noqa: PLC0415

        lane = current_lane()
        if lane is not None and lane.is_part_id(item):
            documents = [item]
            item_context = f"Document id: {item}"

    child_level = parent.level_path + level_suffix

    return WrappedTask(
        envelope_id=uuid.uuid4(),
        run_id=parent.run_id,
        parent_run_id=None,
        subtask_id=uuid.uuid4(),
        type="answerer",
        current_depth=parent.current_depth + 1,
        max_depth=parent.max_depth,
        level_path=child_level,
        model_endpoint=parent.model_endpoint,
        model_name=parent.model_name,
        instruction=parent.instruction,
        governance_instruction=parent.governance_instruction,
        context=item_context,
        output_schema=parent.output_schema,
        max_output_tokens=parent.max_output_tokens,
        trigger_reason=f"apply_to_each:{parent.envelope_id}:item_{index}",
        skill_id=parent.skill_id,
        skill_version=parent.skill_version,
        data_scope=parent.data_scope,
        documents=documents,
    )


def _failure_reason(child: WrappedTask, reason: object,
                    env: Optional[InboundEnvelope] = None,
                    exc: Optional[BaseException] = None) -> object:
    """A raw child's failure as seen upward: code plus document id only.

    Model-written text is never parsed for a code, in any mode: a failure
    the model reported is BRT-LANE-004 (raw_local/all_local) or BRT-ANS-010
    (by_reference). In ``by_reference`` (no output guard ran) a raw child's
    failure is shown by the non-raw rules: ``{"code", "message"}`` with the
    first 200 characters, unless the runtime built a code + doc failure.
    """
    if child.data_scope != "raw":
        return reason
    from src.lane.errors import RawStepError  # noqa: PLC0415

    if isinstance(exc, RawStepError):
        # by_reference: raised while the item ran (a LaneError such as
        # BRT-LANE-007, or a runtime error): code + doc ids + 200 characters.
        return exc.failure
    from src.lane.context import (  # noqa: PLC0415
        RAW_FAILURE_DEFAULT, failure_of, lane_failure_code, raw_failure_code,
        raw_failures_hidden,
    )

    # A failure the runtime built (output guard or lane refusal) keeps its
    # code and document ids in every mode.
    code = lane_failure_code(env) if env is not None else None
    if code:
        return failure_of(code, list(child.documents))
    from_model = env is not None and env._reason_from_model
    if raw_failures_hidden():
        # Otherwise the reason is a runtime exception: its leading code or a
        # fixed one. A model-written reason is never parsed.
        code = RAW_FAILURE_DEFAULT if from_model else raw_failure_code(reason)
        return failure_of(code, list(child.documents))
    text = str(reason or "")
    code = "BRT-ANS-010" if from_model else raw_failure_code(text, "BRT-ORC-004")
    return {"code": code, "message": text[:200]}


async def _dispatch_child(
    task: WrappedTask,
    db_path: str,
) -> InboundEnvelope:
    """Dispatch a single child task through the executor."""
    from src.agents.executor import execute
    return await execute(task, db_path=db_path)


async def _raw_ref_failure(task: WrappedTask, exc: Exception, db_path: str) -> InboundEnvelope:
    """A raw step's BRT-APE-005/006: the lane's code+doc failure shape."""
    from src.lane.context import failed_envelope, raw_failure_code  # noqa: PLC0415
    from src.logging.writer import write_event  # noqa: PLC0415

    code = raw_failure_code(exc, "BRT-APE-005")
    try:
        await write_event(
            "error",
            run_id=task.run_id,
            envelope_id=task.envelope_id,
            decision={"code": code, "msg": "raw step has no items", "error": str(exc)[:2000]},
            db_path=db_path,
        )
    except Exception:
        pass
    return failed_envelope(task, code)


async def expand_and_run(
    task: WrappedTask,
    db_path: str = "data/borg_route.db",
) -> InboundEnvelope:
    """Expand an apply_to_each task to N child envelopes, run concurrently, aggregate.

    Returns a resolved InboundEnvelope with:
        output = {
            "results": [{"index": int, "item": ..., "output": dict, "confidence": float}],
            "failures": [{"index": int, "item": ..., "reason": str}],
            "total": int,
            "success_count": int,
            "failure_count": int,
        }

    Raises:
        ValueError: BRT-APE-001 if items cannot be resolved.
        ValueError: BRT-APE-003 if concurrency_limit is invalid.
        ValueError: BRT-APE-004 if depth limit would be exceeded.
    """
    # Validate depth
    if task.current_depth >= task.max_depth:
        raise ValueError(
            f"BRT-APE-004: apply_to_each cannot spawn children — "
            f"current_depth ({task.current_depth}) >= max_depth ({task.max_depth})"
        )

    # Resolve items. BRT-APE-01 — empty-items used to resolve to an envelope
    # with 0 results (phantom success). Now _resolve_items raises BRT-APE-005
    # which we surface as a failed envelope (the orchestrator sees the code
    # and can decide whether to re_plan/fail).
    try:
        items = _resolve_items(task)
    except ValueError as exc:
        if "BRT-APE-005" in str(exc) or "BRT-APE-006" in str(exc):
            if task.data_scope == "raw":
                # On-premise lane: a raw step's failure is seen upward as a
                # code plus document ids only (design note 5.4); the message
                # stays in the local trace.
                return await _raw_ref_failure(task, exc, db_path)
            return InboundEnvelope(
                envelope_id=task.envelope_id,
                subtask_id=task.subtask_id,
                status="failed",
                output={
                    "results": [],
                    "failures": [],
                    "total": 0,
                    "success_count": 0,
                    "failure_count": 0,
                    # re_plan reads a step's output, not its failure_reason:
                    # the reason goes here too so the planner can act on it.
                    "failure": str(exc),
                },
                failure_reason=str(exc),
            )
        raise

    # Concurrency limit
    raw_limit = task.dependency_input.get("concurrency_limit", 20)
    try:
        concurrency_limit = int(raw_limit)
        if concurrency_limit < 1:
            raise ValueError()
    except (ValueError, TypeError):
        raise ValueError(
            f"BRT-APE-003: invalid concurrency_limit '{raw_limit}' for "
            f"envelope {task.envelope_id}"
        )

    # Decision 17-Nov-2026: the fan-out is capped at the connection pool's
    # design size (MAX_CONCURRENCY, a quarter of the pool), so one step can
    # never exhaust the shared pool (BRT-ANS-011).
    from src.lane.outbound import MAX_CONCURRENCY  # noqa: PLC0415

    notes: list[str] = []
    if concurrency_limit > MAX_CONCURRENCY:
        note = f"concurrency_limit {concurrency_limit} capped at {MAX_CONCURRENCY}"
        notes.append(note)
        concurrency_limit = MAX_CONCURRENCY
        from src.logging.writer import write_event  # noqa: PLC0415

        try:
            await write_event(
                "error",
                run_id=task.run_id,
                envelope_id=task.envelope_id,
                decision={
                    "code": "BRT-APE-CAPPED",
                    "severity": "warning",
                    "msg": note,
                },
                db_path=db_path,
            )
        except Exception:
            pass
    semaphore = asyncio.Semaphore(concurrency_limit)
    results = []
    failures = []
    # Tokens and dollars of every item call, summed onto this envelope.
    totals = {"input": 0, "output": 0, "dollars": 0.0, "baseline": 0.0}
    guarded = [0]   # raw items whose output passed the lane's output guard

    async def _run_one(index: int, item: object) -> None:
        suffix = _level_suffix(index)
        child = _make_child_task(task, item, index, suffix)
        async with semaphore:
            try:
                env = await _dispatch_child(child, db_path)
                totals["input"] += (env.tokens_used or {}).get("input") or 0
                totals["output"] += (env.tokens_used or {}).get("output") or 0
                totals["dollars"] += env.dollars or 0.0
                totals["baseline"] += env.baseline_dollars or 0.0
                if env.status == "resolved":
                    results.append({
                        "index": index,
                        "item": item,
                        "output": env.output,
                        "confidence": env.confidence,
                    })
                    if env.lane_guarded:
                        guarded[0] += 1
                else:
                    failures.append({
                        "index": index,
                        "item": item,
                        "reason": _failure_reason(child, env.failure_reason or "status=failed", env),
                        "model_id": child.model_name,
                    })
            except Exception as exc:
                failures.append({
                    "index": index,
                    "item": item,
                    "reason": _failure_reason(child, f"BRT-APE-002: {exc}", exc=exc),
                    "model_id": child.model_name,
                })

    await asyncio.gather(*[_run_one(i, item) for i, item in enumerate(items)])

    # Sort results by index for deterministic ordering
    results.sort(key=lambda r: r["index"])
    failures.sort(key=lambda r: r["index"])

    aggregate = {
        "results": results,
        "failures": failures,
        "total": len(items),
        "success_count": len(results),
        "failure_count": len(failures),
    }
    if notes:
        aggregate["notes"] = notes
    return InboundEnvelope(
        envelope_id=task.envelope_id,
        subtask_id=task.subtask_id,
        status="resolved",
        output=aggregate,
        tokens_used={"input": totals["input"], "output": totals["output"]},
        dollars=totals["dollars"],
        baseline_dollars=totals["baseline"],
        # A raw aggregate is guard-stamped only when every item passed the
        # guard; one withheld item keeps the steps that read it raw.
        lane_guarded=(
            (not failures and guarded[0] == len(items)) if task.data_scope == "raw" else None
        ),
    )


def _level_suffix(index: int) -> str:
    """Generate a short alphabetic suffix for child level_paths.

    0→a, 1→b, ..., 25→z, 26→aa, 27→ab, ...
    """
    chars = []
    n = index
    while True:
        chars.append(chr(ord("a") + (n % 26)))
        n = n // 26 - 1
        if n < 0:
            break
    return "".join(reversed(chars))
