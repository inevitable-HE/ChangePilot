from __future__ import annotations

from changepilot.planning.application.validation import PlanValidator
from changepilot.planning.domain.models import (
    ChangePlan,
    EvidenceRef,
    PlanStep,
    PlanToolRef,
    RetrievedEvidence,
    TrustLevel,
)
from changepilot.planning.domain.policies import (
    PlanningPolicyCatalog,
    PlanningToolPolicy,
)
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.ports.tools import ToolRisk
from tests.support.order_upgrade import OrderUpgradeTool


def _runtime() -> tuple[ToolRegistry, PlanValidator, EvidenceRef, RetrievedEvidence]:
    calls: list[str] = []
    tools = ToolRegistry(
        (
            OrderUpgradeTool(
                "schema.migrate",
                calls,
                risk=ToolRisk.HIGH,
                effect_applied=True,
            ),
            OrderUpgradeTool("schema.rollback", calls),
            OrderUpgradeTool("service.inspect", calls),
        )
    )
    policies = PlanningPolicyCatalog(
        version="policy-v1",
        policies=(
            PlanningToolPolicy(
                tool=PlanToolRef(name="schema.migrate", version="1.0.0"),
                minimum_risk=ToolRisk.HIGH,
                side_effecting=True,
                compensation_required=True,
                minimum_trust=TrustLevel.INTERNAL,
            ),
            PlanningToolPolicy(
                tool=PlanToolRef(name="service.inspect", version="1.0.0"),
                minimum_risk=ToolRisk.LOW,
                side_effecting=False,
                compensation_required=False,
                minimum_trust=TrustLevel.UNTRUSTED,
            ),
        ),
    )
    reference = EvidenceRef(
        document_id="migration",
        document_version="1",
        chunk_id="chunk-1",
        location="Migration: 0-20",
        content_digest="digest",
    )
    evidence = RetrievedEvidence(
        reference=reference,
        text="Schema writes require approval and rollback.",
        trust_level=TrustLevel.AUTHORITATIVE,
        hybrid_score=0.5,
    )
    return tools, PlanValidator(tools=tools, policies=policies), reference, evidence


def _plan(
    reference: EvidenceRef,
    *,
    tool: str = "schema.migrate",
    compensation: bool = True,
    evidence_refs: tuple[EvidenceRef, ...] | None = None,
    depends_on: tuple[str, ...] = (),
) -> ChangePlan:
    return ChangePlan(
        plan_id="order-upgrade",
        version=1,
        goal="upgrade orders",
        steps=(
            PlanStep(
                id="migrate",
                tool=PlanToolRef(name=tool, version="1.0.0"),
                arguments={"value": "migrate"},
                depends_on=depends_on,
                risk=ToolRisk.LOW,
                compensation_tool=(
                    PlanToolRef(name="schema.rollback", version="1.0.0")
                    if compensation
                    else None
                ),
                compensation_intent="restore schema" if compensation else None,
                evidence_refs=(
                    (reference,)
                    if evidence_refs is None
                    else evidence_refs
                ),
            ),
        ),
        knowledge_snapshot_digest="knowledge-v1",
        prompt_version="planning-v1",
        tool_policy_version="policy-v1",
    )


def test_validator_promotes_risk_and_requires_approval() -> None:
    _, validator, reference, evidence = _runtime()

    report = validator.validate(_plan(reference), (evidence,))

    assert report.valid is True
    step = report.normalized_plan.steps[0]
    assert step.risk is ToolRisk.HIGH
    assert step.approval_required is True


def test_unknown_tool_is_nonrepairable_policy_error() -> None:
    _, validator, reference, evidence = _runtime()

    report = validator.validate(
        _plan(reference, tool="unknown.tool"),
        (evidence,),
    )

    assert report.errors[0].code == "unknown_tool"
    assert report.errors[0].repairable is False


def test_missing_compensation_is_rejected() -> None:
    _, validator, reference, evidence = _runtime()

    report = validator.validate(
        _plan(reference, compensation=False),
        (evidence,),
    )

    assert {issue.code for issue in report.errors} == {"missing_compensation"}


def test_untrusted_evidence_cannot_authorize_side_effect() -> None:
    _, validator, reference, evidence = _runtime()
    untrusted = evidence.model_copy(
        update={"trust_level": TrustLevel.UNTRUSTED}
    )

    report = validator.validate(_plan(reference), (untrusted,))

    assert "insufficient_trusted_evidence" in {
        issue.code for issue in report.errors
    }


def test_stale_evidence_reference_is_rejected() -> None:
    _, validator, reference, evidence = _runtime()
    stale = reference.model_copy(update={"content_digest": "old"})

    report = validator.validate(
        _plan(reference, evidence_refs=(stale,)),
        (evidence,),
    )

    assert "invalid_evidence_reference" in {
        issue.code for issue in report.errors
    }
