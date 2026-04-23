"""ModelInfo — canonical Pydantic model for a model in the capability matrix.

Error codes:
- BRT-REG-001: invalid source value
- BRT-REG-002: negative pricing value
"""
from typing import Literal, Optional

from pydantic import BaseModel, field_validator, model_validator


class Pricing(BaseModel):
    input_per_token: float
    output_per_token: float

    @field_validator("input_per_token", "output_per_token")
    @classmethod
    def no_negative_pricing(cls, v: float) -> float:
        if v < 0:
            raise ValueError("BRT-REG-002: pricing values must be non-negative")
        return v


VALID_SOURCES = {"lmstudio", "ollama", "openrouter", "custom_key"}


class ModelInfo(BaseModel):
    model_id: str
    source: Literal["lmstudio", "ollama", "openrouter", "custom_key"]
    endpoint: Optional[str] = None
    context_length: int
    pricing: Optional[Pricing] = None
    throughput_tps: Optional[float] = None
    task_scores: dict[str, float] = {}
    specialty_tags: list[str] = []
    params_b: Optional[float] = None
    publisher: Optional[str] = None
    notes: Optional[str] = None
    checked: bool = False

    @model_validator(mode="before")
    @classmethod
    def validate_source(cls, values: dict) -> dict:
        src = values.get("source")
        if src is not None and src not in VALID_SOURCES:
            raise ValueError(
                f"BRT-REG-001: invalid source '{src}'. "
                f"Must be one of {sorted(VALID_SOURCES)}"
            )
        return values
