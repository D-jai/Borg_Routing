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

from pydantic import BaseModel, field_validator, model_validator

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
    context: str = ""
    state_query: Optional[str] = None
    side_effects: list[str] = []
    tool_requests: list[ToolRequest] = []
    dependency_input: dict = {}
    output_schema: dict
    max_output_tokens: int = 1000
    trigger_reason: str = "initial_plan"
    skill_id: Optional[str] = None
    skill_version: Optional[int] = None
    skill_args: Optional[dict] = None

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
    scratch_writes: list[SideEffectAction] = []
    scratch_cleanup: list[SideEffectAction] = []
