from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from changepilot.planning.adapters.models.mock import MockModelGateway
from changepilot.planning.adapters.persistence import InMemoryPlanningRepository
from changepilot.planning.application.graph import build_planning_graph
from changepilot.planning.application.ingestion import (
    InMemoryKnowledgeStore,
    RunbookIngestionService,
    load_runbook,
)
from changepilot.planning.application.mapping import WorkflowDefinitionMapper
from changepilot.planning.application.nodes import PlanningNodes
from changepilot.planning.application.retrieval import HybridRetriever
from changepilot.planning.application.services import PlanningService
from changepilot.planning.application.validation import PlanValidator
from changepilot.planning.domain.models import (
    ChangePlan,
    ChangeRequest,
    PlanStep,
    PlanToolRef,
    RetrievedEvidence,
    TrustLevel,
)
from changepilot.planning.domain.policies import (
    PlanningPolicyCatalog,
    PlanningToolPolicy,
)
from changepilot.planning.ports.knowledge import RankedChunk
from changepilot.planning.ports.models import ModelResponse, ModelUsage
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.ports.tools import ToolRisk


ROOT = Path(__file__).resolve().parents[2]


@dataclass
class AllChunksIndex:
    store: InMemoryKnowledgeStore

    def lexical_search(self, query: str, *, limit: int) -> tuple[RankedChunk, ...]:
        return tuple(
            RankedChunk(chunk=chunk, score=1.0)
            for chunk in self.store.all_chunks()[:limit]
        )

    def vector_search(self, query: str, *, limit: int) -> tuple[RankedChunk, ...]:
        return ()


def ingest_example_runbooks(store: InMemoryKnowledgeStore) -> None:
    ingestion = RunbookIngestionService(store)
    for name in (
        "order-service-upgrade.md",
        "schema-migration.md",
        "rollback-policy.md",
    ):
        ingestion.ingest(load_runbook(ROOT / "examples" / "runbooks" / name))


def order_upgrade_request() -> ChangeRequest:
    return ChangeRequest(
        service_id="orders",
        current_version="1.0",
        target_version="2.0",
        change_summary="upgrade the order service and apply its schema migration",
        success_conditions=(
            "health check passes",
            "order creation smoke test passes",
        ),
    )


def order_upgrade_plan_payload(
    evidence: tuple[RetrievedEvidence, ...],
) -> dict[str, object]:
    references = tuple(item.reference for item in evidence)

    def step(
        step_id: str,
        tool: str,
        *,
        depends_on: tuple[str, ...] = (),
        compensation_tool: str | None = None,
        rationale: str,
    ) -> PlanStep:
        return PlanStep(
            id=step_id,
            tool=PlanToolRef(name=tool, version="1.0.0"),
            arguments={"value": step_id},
            depends_on=depends_on,
            compensation_tool=(
                None
                if compensation_tool is None
                else PlanToolRef(name=compensation_tool, version="1.0.0")
            ),
            compensation_intent=(
                None
                if compensation_tool is None
                else f"compensate {step_id}"
            ),
            rationale=rationale,
            evidence_refs=references,
        )

    return ChangePlan(
        plan_id="model-plan",
        version=1,
        goal="upgrade orders from 1.0 to 2.0",
        steps=(
            step(
                "inspect-service",
                "service.inspect",
                rationale="capture current service state",
            ),
            step(
                "inspect-db",
                "schema.inspect",
                rationale="capture current schema state",
            ),
            step(
                "precheck",
                "upgrade.precheck",
                depends_on=("inspect-service", "inspect-db"),
                rationale="verify upgrade prerequisites",
            ),
            step(
                "migrate-schema",
                "schema.migrate",
                depends_on=("precheck",),
                compensation_tool="schema.rollback",
                rationale="apply the schema required by version 2.0",
            ),
            step(
                "deploy-v2",
                "service.deploy-v2",
                depends_on=("migrate-schema",),
                compensation_tool="service.restore",
                rationale="deploy the target service version",
            ),
            step(
                "health-check",
                "service.health-check",
                depends_on=("deploy-v2",),
                rationale="verify service health",
            ),
            step(
                "smoke-test",
                "service.smoke-test",
                depends_on=("health-check",),
                rationale="verify order creation",
            ),
        ),
        knowledge_snapshot_digest="model-controlled",
        prompt_version="model-controlled",
        tool_policy_version="model-controlled",
    ).model_dump(mode="json")


def model_response(payload: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        payload=payload,
        usage=ModelUsage(input_tokens=100, output_tokens=200, total_tokens=300),
        model="mock-planner",
        latency_ms=1,
        finish_reason="stop",
    )


def make_planning_service(
    *,
    store: InMemoryKnowledgeStore,
    tools: ToolRegistry,
    policies: PlanningPolicyCatalog,
    model: MockModelGateway,
) -> PlanningService:
    validator = PlanValidator(tools=tools, policies=policies)
    return PlanningService(
        graph=build_planning_graph(
            PlanningNodes(
                model=model,
                retriever=HybridRetriever(
                    store=store,
                    index=AllChunksIndex(store),
                ),
                validator=validator,
                prompt_version="planning-v1",
                tool_policy_version=policies.version,
            )
        ),
        repository=InMemoryPlanningRepository(),
        knowledge=store,
        mapper=WorkflowDefinitionMapper(
            tools=tools,
            validator=validator,
        ),
        identifiers=lambda: "order-upgrade-session",
    )


def order_upgrade_policies() -> PlanningPolicyCatalog:
    return PlanningPolicyCatalog(
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
                tool=PlanToolRef(name="service.deploy-v2", version="1.0.0"),
                minimum_risk=ToolRisk.MEDIUM,
                side_effecting=True,
                compensation_required=True,
                minimum_trust=TrustLevel.INTERNAL,
            ),
        ),
    )
