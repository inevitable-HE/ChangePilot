from __future__ import annotations

from enum import StrEnum

from pydantic import Field, field_validator

from changepilot.sandbox.domain.models import SandboxBoundaryModel


class FaultType(StrEnum):
    BEFORE_EFFECT = "before_effect"
    AFTER_EFFECT = "after_effect"
    PERMANENT_FAILURE = "permanent_failure"


class FaultSpec(SandboxBoundaryModel):
    tool_name: str
    call_number: int = Field(gt=0)
    fault_type: FaultType

    @field_validator("tool_name")
    @classmethod
    def _reject_blank_tool_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("tool_name must be non-empty")
        return value


class FaultConfiguration(SandboxBoundaryModel):
    specs: tuple[FaultSpec, ...] = ()
    call_counts: dict[str, int] = Field(default_factory=dict)
