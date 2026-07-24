from __future__ import annotations

from pydantic import Field

from changepilot.planning.domain.models import (
    ChangePlan,
    PlanStep,
    PlanningBoundaryModel,
    RetrievedEvidence,
    TrustLevel,
)
from changepilot.planning.domain.policies import (
    PlanningPolicyCatalog,
    PlanningToolPolicy,
)
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.validation import find_cycle
from changepilot.workflow.ports.tools import ToolRisk


_RISK_ORDER = {
    ToolRisk.LOW: 0,
    ToolRisk.MEDIUM: 1,
    ToolRisk.HIGH: 2,
}
_TRUST_ORDER = {
    TrustLevel.UNTRUSTED: 0,
    TrustLevel.INTERNAL: 1,
    TrustLevel.AUTHORITATIVE: 2,
}


class ValidationIssue(PlanningBoundaryModel):
    code: str
    message: str
    step_id: str | None = None
    repairable: bool = False


class ValidationReport(PlanningBoundaryModel):
    valid: bool
    normalized_plan: ChangePlan | None = None
    errors: tuple[ValidationIssue, ...] = ()


class PlanValidator:
    def __init__(
        self,
        *,
        tools: ToolRegistry,
        policies: PlanningPolicyCatalog,
    ) -> None:
        self._tools = tools
        self._policies = policies

    def validate(
        self,
        plan: ChangePlan,
        evidence: tuple[RetrievedEvidence, ...],
    ) -> ValidationReport:
        errors: list[ValidationIssue] = []
        evidence_by_chunk = {
            item.reference.chunk_id: item
            for item in evidence
        }
        dependencies = {
            step.id: step.depends_on
            for step in plan.steps
        }
        known_step_ids = set(dependencies)
        for step in plan.steps:
            for dependency in step.depends_on:
                if dependency not in known_step_ids:
                    errors.append(
                        self._issue(
                            "unknown_dependency",
                            f"unknown dependency {dependency!r}",
                            step.id,
                        )
                    )
        if not any(issue.code == "unknown_dependency" for issue in errors):
            cycle = find_cycle(dependencies)
            if cycle is not None:
                errors.append(
                    self._issue(
                        "dependency_cycle",
                        f"cycle detected: {', '.join(cycle)}",
                        cycle[0],
                    )
                )

        normalized_steps: list[PlanStep] = []
        for step in plan.steps:
            normalized_steps.append(
                self._validate_step(
                    step,
                    evidence_by_chunk,
                    errors,
                )
            )

        if plan.tool_policy_version != self._policies.version:
            errors.append(
                self._issue(
                    "stale_tool_policy",
                    "plan tool policy version is stale",
                )
            )
        if errors:
            return ValidationReport(valid=False, errors=tuple(errors))
        normalized = plan.model_copy(update={"steps": tuple(normalized_steps)})
        return ValidationReport(valid=True, normalized_plan=normalized)

    def _validate_step(
        self,
        step: PlanStep,
        evidence_by_chunk: dict[str, RetrievedEvidence],
        errors: list[ValidationIssue],
    ) -> PlanStep:
        if not self._tools.contains(step.tool.name, step.tool.version):
            errors.append(
                self._issue(
                    "unknown_tool",
                    f"unknown tool {step.tool.name}@{step.tool.version}",
                    step.id,
                )
            )
            return step
        try:
            self._tools.validate_arguments(
                step.tool.name,
                step.tool.version,
                step.arguments,
            )
        except ValueError as exc:
            errors.append(
                self._issue(
                    "invalid_arguments",
                    str(exc),
                    step.id,
                )
            )

        descriptor = self._tools.descriptor_for(
            step.tool.name,
            step.tool.version,
        )
        policy = self._policies.get(step.tool.name, step.tool.version)
        final_risk = _max_risk(
            step.risk,
            descriptor.risk,
            policy.minimum_risk if policy is not None else descriptor.risk,
        )
        approval_required = step.approval_required or final_risk is ToolRisk.HIGH
        risk_reasons = tuple(
            dict.fromkeys(
                (
                    *step.risk_reasons,
                    *(
                        ("runtime tool descriptor marks this operation high risk",)
                        if descriptor.risk is ToolRisk.HIGH
                        else ()
                    ),
                    *(
                        ("planning tool policy raises the minimum risk",)
                        if policy is not None
                        and _RISK_ORDER[policy.minimum_risk]
                        > _RISK_ORDER[step.risk]
                        else ()
                    ),
                )
            )
        )
        approval_reason = step.approval_reason
        if approval_required and not approval_reason:
            approval_reason = "deterministic risk policy requires approval"

        if policy is not None:
            self._validate_compensation(step, policy, errors)
            self._validate_evidence(
                step,
                policy,
                evidence_by_chunk,
                errors,
            )
        else:
            self._validate_citations(step, evidence_by_chunk, errors)

        return step.model_copy(
            update={
                "risk": final_risk,
                "approval_required": approval_required,
                "approval_reason": approval_reason,
                "risk_reasons": risk_reasons,
            }
        )

    def _validate_compensation(
        self,
        step: PlanStep,
        policy: PlanningToolPolicy,
        errors: list[ValidationIssue],
    ) -> None:
        if not policy.compensation_required:
            return
        if step.compensation_tool is None or not step.compensation_intent:
            errors.append(
                self._issue(
                    "missing_compensation",
                    "tool policy requires a compensation tool and intent",
                    step.id,
                )
            )
            return
        if not self._tools.contains(
            step.compensation_tool.name,
            step.compensation_tool.version,
        ):
            errors.append(
                self._issue(
                    "unknown_compensation_tool",
                    "compensation tool is not registered",
                    step.id,
                )
            )

    def _validate_evidence(
        self,
        step: PlanStep,
        policy: PlanningToolPolicy,
        evidence_by_chunk: dict[str, RetrievedEvidence],
        errors: list[ValidationIssue],
    ) -> None:
        valid_trust = False
        for reference in step.evidence_refs:
            item = evidence_by_chunk.get(reference.chunk_id)
            if item is None or item.reference != reference:
                errors.append(
                    self._issue(
                        "invalid_evidence_reference",
                        f"evidence {reference.chunk_id!r} is absent or stale",
                        step.id,
                    )
                )
                continue
            if item.conflict:
                errors.append(
                    self._issue(
                        "conflicting_evidence",
                        f"evidence {reference.chunk_id!r} is conflicted",
                        step.id,
                    )
                )
            if _TRUST_ORDER[item.trust_level] >= _TRUST_ORDER[policy.minimum_trust]:
                valid_trust = True
        if policy.side_effecting and not valid_trust:
            errors.append(
                self._issue(
                    "insufficient_trusted_evidence",
                    "side-effecting step lacks sufficiently trusted evidence",
                    step.id,
                )
            )

    def _validate_citations(
        self,
        step: PlanStep,
        evidence_by_chunk: dict[str, RetrievedEvidence],
        errors: list[ValidationIssue],
    ) -> None:
        for reference in step.evidence_refs:
            item = evidence_by_chunk.get(reference.chunk_id)
            if item is None or item.reference != reference:
                errors.append(
                    self._issue(
                        "invalid_evidence_reference",
                        f"evidence {reference.chunk_id!r} is absent or stale",
                        step.id,
                    )
                )

    @staticmethod
    def _issue(
        code: str,
        message: str,
        step_id: str | None = None,
    ) -> ValidationIssue:
        return ValidationIssue(
            code=code,
            message=message,
            step_id=step_id,
            repairable=False,
        )


def _max_risk(*values: ToolRisk) -> ToolRisk:
    return max(values, key=_RISK_ORDER.__getitem__)
