from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Annotated, Any, Literal, TypeAlias

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from changepilot.workflow.ports.tools import ToolRisk


class PlanningBoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class TrustLevel(StrEnum):
    UNTRUSTED = "untrusted"
    INTERNAL = "internal"
    AUTHORITATIVE = "authoritative"


class ChangeRequest(PlanningBoundaryModel):
    service_id: str | None = None
    current_version: str | None = None
    target_version: str | None = None
    change_summary: str
    success_conditions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()

    @field_validator(
        "service_id",
        "current_version",
        "target_version",
        "change_summary",
    )
    @classmethod
    def _reject_blank_strings(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must be non-empty when provided")
        return value

    @field_validator("success_conditions", "constraints")
    @classmethod
    def _reject_blank_items(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not item.strip() for item in value):
            raise ValueError("items must be non-empty strings")
        return value


class EvidenceRef(PlanningBoundaryModel):
    document_id: str
    document_version: str
    chunk_id: str
    location: str
    content_digest: str


class RetrievedEvidence(PlanningBoundaryModel):
    reference: EvidenceRef
    text: str
    trust_level: TrustLevel
    lexical_rank: int | None = None
    vector_rank: int | None = None
    hybrid_score: float
    conflict: bool = False


class PlanToolRef(PlanningBoundaryModel):
    name: str
    version: str


class PlanStep(PlanningBoundaryModel):
    id: str
    tool: PlanToolRef
    arguments: dict[str, Any] = Field(default_factory=dict)
    depends_on: tuple[str, ...] = ()
    risk: ToolRisk = ToolRisk.LOW
    approval_required: bool = False
    approval_reason: str | None = None
    rationale: str = ""
    risk_reasons: tuple[str, ...] = ()
    success_conditions: tuple[str, ...] = ()
    compensation_tool: PlanToolRef | None = None
    compensation_intent: str | None = None
    evidence_refs: tuple[EvidenceRef, ...] = ()

    @field_validator("id", "compensation_intent", "approval_reason")
    @classmethod
    def _reject_blank_optional(cls, value: str | None) -> str | None:
        if value is not None and not value.strip():
            raise ValueError("value must be non-empty when provided")
        return value


class ChangePlan(PlanningBoundaryModel):
    plan_id: str
    version: int = Field(ge=1)
    goal: str
    assumptions: tuple[str, ...] = ()
    steps: tuple[PlanStep, ...]
    knowledge_snapshot_digest: str
    prompt_version: str
    tool_policy_version: str

    @model_validator(mode="after")
    def _require_unique_step_ids(self) -> ChangePlan:
        step_ids = [step.id for step in self.steps]
        if len(step_ids) != len(set(step_ids)):
            raise ValueError("step ids must be unique")
        return self

    @property
    def content_digest(self) -> str:
        payload = self.model_dump(mode="json")
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


class ClarificationQuestion(PlanningBoundaryModel):
    question_id: str
    field: str
    prompt: str
    blocking: bool = True


class ClarificationRequired(PlanningBoundaryModel):
    kind: Literal["clarification_required"] = "clarification_required"
    session_id: str
    questions: tuple[ClarificationQuestion, ...]


class PlanReady(PlanningBoundaryModel):
    kind: Literal["plan_ready"] = "plan_ready"
    session_id: str
    plan: ChangePlan
    evidence: tuple[RetrievedEvidence, ...]


class PlanningRejected(PlanningBoundaryModel):
    kind: Literal["planning_rejected"] = "planning_rejected"
    session_id: str
    errors: tuple[str, ...]


class BudgetExhausted(PlanningBoundaryModel):
    kind: Literal["budget_exhausted"] = "budget_exhausted"
    session_id: str
    calls_used: int
    total_tokens: int


PlanningResult: TypeAlias = Annotated[
    ClarificationRequired | PlanReady | PlanningRejected | BudgetExhausted,
    Field(discriminator="kind"),
]


class PreparedWorkflow(PlanningBoundaryModel):
    plan_id: str
    plan_version: int
    knowledge_snapshot_digest: str
    definition_id: str
    definition_version: int
    definition_digest: str
    payload: dict[str, Any]
