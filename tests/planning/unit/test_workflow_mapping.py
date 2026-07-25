from __future__ import annotations

from changepilot.planning.application.mapping import WorkflowDefinitionMapper
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


def test_mapper_produces_definition_accepted_by_runtime() -> None:
    calls: list[str] = []
    tools = ToolRegistry(
        (
            OrderUpgradeTool(
                "schema.migrate",
                calls,
                risk=ToolRisk.HIGH,
            ),
            OrderUpgradeTool("schema.rollback", calls),
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
        ),
    )
    reference = EvidenceRef(
        document_id="migration",
        document_version="1",
        chunk_id="chunk-1",
        location="Migration: 0-10",
        content_digest="digest",
    )
    evidence = RetrievedEvidence(
        reference=reference,
        text="Approval and rollback are required.",
        trust_level=TrustLevel.AUTHORITATIVE,
        hybrid_score=0.5,
    )
    plan = ChangePlan(
        plan_id="order-upgrade",
        version=1,
        goal="upgrade",
        steps=(
            PlanStep(
                id="migrate",
                tool=PlanToolRef(name="schema.migrate", version="1.0.0"),
                arguments={"value": "migrate"},
                compensation_tool=PlanToolRef(
                    name="schema.rollback",
                    version="1.0.0",
                ),
                compensation_intent="restore schema",
                evidence_refs=(reference,),
            ),
        ),
        knowledge_snapshot_digest="knowledge-v1",
        prompt_version="planning-v1",
        tool_policy_version="policy-v1",
    )

    prepared = WorkflowDefinitionMapper(
        tools=tools,
        validator=PlanValidator(tools=tools, policies=policies),
    ).prepare(plan, (evidence,))

    assert prepared.definition_id == "order-upgrade"
    assert prepared.definition_digest
    assert prepared.payload["steps"][0]["risk"] == "high"
