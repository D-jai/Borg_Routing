"""Envelope schema — WrappedTask (outbound) + InboundEnvelope (inbound).

Core invariant (BRT-ENV-001): InboundEnvelope.status MUST be 'resolved' or
'failed'. The value 'unresolved' is explicitly rejected by the schema.

Error codes:
- BRT-ENV-001: Invalid status value (must be 'resolved' or 'failed')
- BRT-ENV-002: Invalid envelope type
- BRT-ENV-003: current_depth exceeds max_depth
- BRT-ENV-004: instruction is empty or whitespace-only
- BRT-ENV-005: Invalid SideEffectAction — unknown action value
- BRT-ENV-006: ToolRequest.tool is empty or whitespace-only (BRT-WEB-01)
"""
from typing import Literal, Optional
from uuid import UUID

from pydantic import BaseModel, PrivateAttr, field_validator, model_validator

VALID_ENVELOPE_TYPES = {"orchestrate", "answerer", "apply_to_each", "state_op"}
VALID_SIDE_EFFECT_ACTIONS = {"write", "delete"}


class ToolRequest(BaseModel):
    """A single tool invocation requested by the orchestrator on behalf of a leaf.

    The executor runs the tool BEFORE calling the answerer model and injects
    the result into ``dependency_input.tool_results[<tool>]`` so the model
    sees the output in its prompt. No in-model tool-call loops — see
    BRT-WEB-01 / "Option B+" in Bug Report.md.
    """
    tool: str
    args: dict = {}

    @field_validator("tool")
    @classmethod
    def _non_empty(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("BRT-ENV-006: ToolRequest.tool must be non-empty")
        return v.strip()


class SideEffectAction(BaseModel):
    table: str
    action: Literal["write", "delete"]
    row_id: Optional[str] = None
    ids: Optional[list[str]] = None

    @field_validator("action", mode="before")
    @classmethod
    def validate_action(cls, v: str) -> str:
        if v not in VALID_SIDE_EFFECT_ACTIONS:
            raise ValueError(
                f"BRT-ENV-005: invalid action '{v}'. "
                f"Must be one of {sorted(VALID_SIDE_EFFECT_ACTIONS)}"
            )
        return v


class WrappedTask(BaseModel):
    envelope_id: UUID
    run_id: UUID
    parent_run_id: Optional[UUID] = None
    subtask_id: UUID
    type: Literal["orchestrate", "answerer", "apply_to_each", "state_op"]
    current_depth: int
    max_depth: int
    level_path: str
    model_endpoint: Optional[str] = None
    model_name: Optional[str] = None
    instruction: str
    # Law 1 — the operator's verbatim instruction for the whole run. The
    # engine stamps it on every task at every depth so the governance block
    # of each model call shows what the operator asked, never a step's own
    # text. None only for tasks built outside the engine; the executor then
    # falls back to ``instruction``.
    governance_instruction: Optional[str] = None
    context: str = ""
    state_query: Optional[str] = None
    side_effects: list[str] = []
    tool_requests: list[ToolRequest] = []
    dependency_input: dict = {}
    output_schema: dict
    # BRT-ANS-009 — Per-envelope token budget request. The executor uses
    # this as a FLOOR: effective_max = max(ANSWERER_MAX_TOKENS env, this).
    # Plan nodes that need more headroom (e.g. "produce a Go impl + 3
    # tests") can declare a higher cap; the orchestrator stamps it onto
    # the WrappedTask via _build_task_from_node. Default 4000 keeps small
    # answers cheap while leaving room for non-trivial JSON outputs
    # without immediately tripping finish_reason=length.
    max_output_tokens: int = 4000
    trigger_reason: str = "initial_plan"
    # The plan node this envelope runs (set by the engine); the Run page's
    # diagram maps envelopes to plan nodes by it. None for tasks built elsewhere.
    node_id: Optional[str] = None
    skill_id: Optional[str] = None
    skill_version: Optional[int] = None
    skill_args: Optional[dict] = None
    # On-premise lane (docs/design/on_premise_lane.md section 5). Set by the
    # runtime, never by the planner: "raw" when the task reads document
    # content or the unguarded output of a raw step. A raw task is routed to
    # local models only and its output passes the output guard.
    data_scope: Literal["raw", "derived"] = "derived"
    # Document or part ids this task reads ("doc_001", "doc_001#2"). Ids only:
    # the text is loaded inside the executing task, right before its own
    # (local) request is built.
    documents: list[str] = []

    @field_validator("type", mode="before")
    @classmethod
    def validate_type(cls, v: str) -> str:
        if v not in VALID_ENVELOPE_TYPES:
            raise ValueError(
                f"BRT-ENV-002: invalid envelope type '{v}'. "
                f"Must be one of {sorted(VALID_ENVELOPE_TYPES)}"
            )
        return v

    @field_validator("instruction")
    @classmethod
    def validate_instruction(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("BRT-ENV-004: instruction must not be empty")
        return v

    @model_validator(mode="after")
    def validate_depth(self) -> "WrappedTask":
        if self.current_depth > self.max_depth:
            raise ValueError(
                f"BRT-ENV-003: current_depth ({self.current_depth}) exceeds "
                f"max_depth ({self.max_depth})"
            )
        return self


class InboundEnvelope(BaseModel):
    envelope_id: UUID
    subtask_id: UUID
    status: Literal["resolved", "failed"]
    confidence: Optional[float] = None
    output: dict
    failure_reason: Optional[str] = None
    tokens_used: dict = {}
    # Dollars for the calls behind this envelope (and, for an orchestrate or
    # apply_to_each node, behind everything it spawned). None = not set; the
    # engine then prices ``tokens_used`` against the node's model.
    dollars: Optional[float] = None
    baseline_dollars: Optional[float] = None
    scratch_writes: list[SideEffectAction] = []
    scratch_cleanup: list[SideEffectAction] = []
    # BRT-WEB-02 — raw tool invocation results (e.g. web_search citations)
    # that fed into this answerer. Carried back so the orchestrator's
    # compose step can re-cite from raw citations even if the answerer's
    # summary was thin, lossy, or hallucinated. Shape:
    #   {"web_search": [{"title":..., "url":..., "snippet":...}, ...], ...}
    # Empty dict when no tools were invoked.
    tool_results: dict = {}
    # On-premise lane: True when a raw envelope's output passed the output
    # guard; a step reading an unguarded raw output is raw itself. None for
    # an envelope the guard never saw (any derived step).
    lane_guarded: Optional[bool] = None
    # On-premise lane: True when ``failure_reason`` was written by the model
    # (the envelope was built from its JSON), not by the runtime. Never
    # serialised; the output guard then shows BRT-LANE-004 upward.
    _reason_from_model: bool = PrivateAttr(default=False)
    # On-premise lane: the data_scope of the task that produced this envelope
    # ("raw" or "derived"), and its document ids label. Set by the engine;
    # never serialised. Decides what a prompt shows of a failure.
    _data_scope: Optional[str] = PrivateAttr(default=None)
    _doc_label: Optional[str] = PrivateAttr(default=None)
    # The node type of the task that produced it (planner ruling 21: compose
    # row markers place the rows of apply_to_each and list state_op steps).
    # Set by the engine; never serialised.
    _node_type: Optional[str] = PrivateAttr(default=None)
    # apply_to_each: the item indexes whose failure reason the model itself
    # wrote (ruling 21: shown as BRT-ANS-010). Never serialised.
    _model_reason_indexes: set = PrivateAttr(default_factory=set)
    # Planner ruling 35b: a step refused for an unknown node type carries the
    # node as the plan wrote it (JSON, cut; built from the plan only), shown to
    # re_plan beside the failure, never to compose. Never serialised.
    _plan_node: Optional[str] = PrivateAttr(default=None)
