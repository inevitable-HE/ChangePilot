from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)


class OperationsBoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class DemoScenario(StrEnum):
    SUCCESS = "success"
    COMPENSATION = "compensation"
    RECOVERY = "recovery"


class RequestStatus(StrEnum):
    CLARIFICATION_REQUIRED = "clarification_required"
    PLAN_READY = "plan_ready"
    REJECTED = "rejected"


class ChangeRequestInput(OperationsBoundaryModel):
    service_id: str | None = None
    current_version: str | None = None
    target_version: str | None = None
    change_summary: str
    success_conditions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    scenario: DemoScenario = DemoScenario.SUCCESS

    @model_validator(mode="before")
    @classmethod
    def _normalize_json_arrays(cls, value: object) -> object:
        if not isinstance(value, dict):
            return value
        normalized = dict(value)
        for field in ("success_conditions", "constraints"):
            if isinstance(normalized.get(field), list):
                normalized[field] = tuple(normalized[field])
        if isinstance(normalized.get("scenario"), str):
            normalized["scenario"] = DemoScenario(normalized["scenario"])
        return normalized

    @field_validator(
        "service_id",
        "current_version",
        "target_version",
        "change_summary",
    )
    @classmethod
    def _reject_blank(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must be non-empty when provided")
        return value


class ChangeRequestView(OperationsBoundaryModel):
    request_id: str
    status: RequestStatus
    submitted_at: str
    request: ChangeRequestInput
    run_id: str | None = None
    clarification_questions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class PlanToolView(OperationsBoundaryModel):
    name: str
    version: str


class PlanStepView(OperationsBoundaryModel):
    step_id: str
    tool: PlanToolView
    depends_on: tuple[str, ...]
    risk: str
    approval_required: bool
    arguments: dict[str, Any]
    compensation_tool: PlanToolView | None = None
    rationale: str
    validation_intent: str
    evidence_refs: tuple[str, ...]


class PlanView(OperationsBoundaryModel):
    run_id: str
    definition_id: str
    definition_version: int
    definition_digest: str
    steps: tuple[PlanStepView, ...]


class RunSummary(OperationsBoundaryModel):
    request_id: str
    run_id: str
    definition_id: str
    definition_version: int
    state: str
    revision: int
    pending_approval: bool
    last_event_sequence: int


class RunSnapshot(OperationsBoundaryModel):
    summary: RunSummary
    steps: tuple[dict[str, Any], ...]
    pending_approval: dict[str, Any] | None
    original_error: dict[str, Any] | None
    compensation_error: dict[str, Any] | None


class GuidanceStageView(OperationsBoundaryModel):
    stage_id: Literal[
        "request",
        "plan",
        "precheck",
        "approval",
        "execution",
        "verification",
        "outcome",
    ]
    state: Literal["complete", "current", "pending", "attention"]


class RunGuidance(OperationsBoundaryModel):
    run_id: str
    goal: str
    service_id: str
    current_version: str
    target_version: str
    success_conditions: tuple[str, ...]
    constraints: tuple[str, ...]
    scenario: DemoScenario
    headline_code: str
    next_action_code: str
    report_available: bool
    stages: tuple[GuidanceStageView, ...]
    safety_controls: tuple[str, ...]


class ApprovalCommand(OperationsBoundaryModel):
    decision: Literal["approved", "rejected"]
    expected_version: int = Field(ge=0)
    binding_digest: str
    actor: str
    reason: str

    @field_validator("binding_digest", "actor")
    @classmethod
    def _require_non_empty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("value must be non-empty")
        return value


class RecoveryCommand(OperationsBoundaryModel):
    expected_run_revision: int = Field(ge=0)


class EventPage(OperationsBoundaryModel):
    run_id: str
    after_sequence: int
    next_cursor: int
    events: tuple[dict[str, Any], ...]


class ReportFormat(StrEnum):
    JSON = "json"
    MARKDOWN = "markdown"
