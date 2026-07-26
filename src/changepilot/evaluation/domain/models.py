from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from changepilot.operations.domain.models import ChangeRequestInput


class EvaluationBoundaryModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class MockPlan(EvaluationBoundaryModel):
    schema_valid: bool
    tools: tuple[str, ...]
    high_risk_tools: tuple[str, ...]
    evidence_refs: tuple[str, ...]


class EvaluationExpectation(EvaluationBoundaryModel):
    required_tools: tuple[str, ...]
    forbidden_tools: tuple[str, ...]
    high_risk_tools: tuple[str, ...]
    required_evidence: tuple[str, ...]
    terminal_state: str
    requires_approval: bool = True
    expected_migration_calls: int = Field(default=1, ge=0)


class EvaluationCase(EvaluationBoundaryModel):
    case_id: str
    split: Literal["development", "holdout"]
    request: ChangeRequestInput
    mock_plan: MockPlan
    expectation: EvaluationExpectation


class EvaluationDataset(EvaluationBoundaryModel):
    dataset_id: str
    version: str
    metrics_version: str
    prompt_version: str
    knowledge_version: str
    random_seed: int
    thresholds: dict[str, float]
    cases: tuple[EvaluationCase, ...]

    @model_validator(mode="after")
    def _validate_dataset(self) -> EvaluationDataset:
        if not self.cases:
            raise ValueError("evaluation dataset must contain at least one case")
        case_ids = [case.case_id for case in self.cases]
        if len(case_ids) != len(set(case_ids)):
            raise ValueError("evaluation case ids must be unique")
        if any(not expected for expected in self.thresholds):
            raise ValueError("metric threshold names must be non-empty")
        return self


class UsageMetrics(EvaluationBoundaryModel):
    model: str
    calls: int = Field(ge=0)
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)
    cache_hits: int = Field(ge=0)
    estimated_cost: float = Field(ge=0)


class CaseResult(EvaluationBoundaryModel):
    case_id: str
    split: str
    passed: bool
    metrics: dict[str, float]
    latency_ms: int = Field(ge=0)
    usage: UsageMetrics
    errors: tuple[str, ...] = ()


class EvaluationMetadata(EvaluationBoundaryModel):
    generated_at: datetime | None = None
    code_version: str
    dataset_id: str
    dataset_version: str
    metrics_version: str
    prompt_version: str
    knowledge_version: str
    model: str
    random_seed: int
    environment: dict[str, Any]


class EvaluationReport(EvaluationBoundaryModel):
    report_version: str = "1.0"
    run_id: str
    passed: bool
    metadata: EvaluationMetadata
    case_results: tuple[CaseResult, ...]
    aggregate_metrics: dict[str, float]
    thresholds: dict[str, float]
    regressions: tuple[str, ...]
    baseline_delta: dict[str, float] = Field(default_factory=dict)
