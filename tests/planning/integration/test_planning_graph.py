from __future__ import annotations

from dataclasses import dataclass

from changepilot.planning.adapters.models.mock import MockModelGateway
from changepilot.planning.adapters.persistence import InMemoryPlanningRepository
from changepilot.planning.application.graph import build_planning_graph
from changepilot.planning.application.ingestion import (
    InMemoryKnowledgeStore,
    RunbookIngestionService,
)
from changepilot.planning.application.mapping import WorkflowDefinitionMapper
from changepilot.planning.application.nodes import PlanningNodes
from changepilot.planning.application.retrieval import HybridRetriever
from changepilot.planning.application.services import PlanningService
from changepilot.planning.application.validation import PlanValidator
from changepilot.planning.domain.knowledge import RunbookDocument
from changepilot.planning.domain.failures import (
    ModelBudgetExceeded,
    TransientModelError,
)
from changepilot.planning.domain.models import (
    ChangePlan,
    ChangeRequest,
    EvidenceRef,
    PlanStep,
    PlanToolRef,
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
from tests.support.order_upgrade import OrderUpgradeTool


@dataclass
class _AllChunksIndex:
    store: InMemoryKnowledgeStore

    def lexical_search(self, query: str, *, limit: int) -> tuple[RankedChunk, ...]:
        return tuple(
            RankedChunk(chunk=chunk, score=1.0)
            for chunk in self.store.all_chunks()[:limit]
        )

    def vector_search(self, query: str, *, limit: int) -> tuple[RankedChunk, ...]:
        return ()


@dataclass
class _Harness:
    service: PlanningService
    model: MockModelGateway
    store: InMemoryKnowledgeStore
    evidence_ref: EvidenceRef


def _complete_request() -> ChangeRequest:
    return ChangeRequest(
        service_id="orders",
        current_version="1.0",
        target_version="2.0",
        change_summary="upgrade orders and migrate schema",
        success_conditions=("health check passes",),
    )


def _model_response(payload: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        payload=payload,
        usage=ModelUsage(input_tokens=10, output_tokens=20, total_tokens=30),
        model="mock-planner",
        latency_ms=1,
        finish_reason="stop",
    )


def _plan_payload(
    reference: EvidenceRef,
    *,
    tool: str = "schema.migrate",
) -> dict[str, object]:
    return ChangePlan(
        plan_id="model-controlled",
        version=99,
        goal="upgrade orders",
        steps=(
            PlanStep(
                id="migrate-schema",
                tool=PlanToolRef(name=tool, version="1.0.0"),
                arguments={"value": "migrate"},
                risk=ToolRisk.LOW,
                compensation_tool=PlanToolRef(
                    name="schema.rollback",
                    version="1.0.0",
                ),
                compensation_intent="restore the previous schema",
                rationale="the target version requires a schema change",
                evidence_refs=(reference,),
            ),
        ),
        knowledge_snapshot_digest="model-controlled",
        prompt_version="model-controlled",
        tool_policy_version="model-controlled",
    ).model_dump(mode="json")


def _harness(scripted: list[ModelResponse | Exception]) -> _Harness:
    store = InMemoryKnowledgeStore()
    RunbookIngestionService(store).ingest(
        RunbookDocument(
            document_id="schema-migration",
            version="1",
            source="runbooks/schema-migration.md",
            trust_level=TrustLevel.AUTHORITATIVE,
            content=(
                "# Migration\nSchema writes require approval and a rollback tool."
            ),
            policy_key="schema-migration",
            effective_at="2026-07-24",
        )
    )
    chunk = store.all_chunks()[0]
    reference = EvidenceRef(
        document_id=chunk.document_id,
        document_version=chunk.document_version,
        chunk_id=chunk.chunk_id,
        location=f"Migration: {chunk.start_offset}-{chunk.end_offset}",
        content_digest=chunk.content_digest,
    )
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
    validator = PlanValidator(tools=tools, policies=policies)
    model = MockModelGateway(scripted)
    retriever = HybridRetriever(
        store=store,
        index=_AllChunksIndex(store),
    )
    graph = build_planning_graph(
        PlanningNodes(
            model=model,
            retriever=retriever,
            validator=validator,
            prompt_version="planning-v1",
            tool_policy_version="policy-v1",
        )
    )
    service = PlanningService(
        graph=graph,
        repository=InMemoryPlanningRepository(),
        knowledge=store,
        mapper=WorkflowDefinitionMapper(
            tools=tools,
            validator=validator,
        ),
        identifiers=lambda: "session-1",
    )
    return _Harness(
        service=service,
        model=model,
        store=store,
        evidence_ref=reference,
    )


def test_missing_context_returns_clarification_without_model_call() -> None:
    harness = _harness([])

    result = harness.service.start(
        ChangeRequest(change_summary="upgrade orders")
    )

    assert result.kind == "clarification_required"
    assert {question.field for question in result.questions} == {
        "service_id",
        "current_version",
        "target_version",
        "success_conditions",
    }
    assert harness.model.call_count == 0


def test_valid_model_plan_is_normalized_and_ready() -> None:
    harness = _harness([])
    harness.model._scripted.append(
        _model_response(_plan_payload(harness.evidence_ref))
    )

    result = harness.service.start(_complete_request())

    assert result.kind == "plan_ready"
    assert result.plan.plan_id == "session-1-plan"
    assert result.plan.version == 1
    assert result.plan.steps[0].risk is ToolRisk.HIGH
    assert result.plan.steps[0].approval_required is True
    assert "deterministic risk policy" in result.plan.steps[0].approval_reason


def test_schema_error_is_repaired_once() -> None:
    harness = _harness([])
    harness.model._scripted.extend(
        (
            _model_response({"invalid": "shape"}),
            _model_response(_plan_payload(harness.evidence_ref)),
        )
    )

    result = harness.service.start(_complete_request())

    assert result.kind == "plan_ready"
    assert harness.model.call_count == 2
    assert "validation_errors" in harness.model.requests[1].messages[1].content


def test_policy_error_is_rejected_without_repair() -> None:
    harness = _harness([])
    harness.model._scripted.append(
        _model_response(
            _plan_payload(harness.evidence_ref, tool="unknown.tool")
        )
    )

    result = harness.service.start(_complete_request())

    assert result.kind == "planning_rejected"
    assert "unknown_tool" in result.errors[0]
    assert harness.model.call_count == 1


def test_clarification_answer_resumes_same_session() -> None:
    harness = _harness([])
    first = harness.service.start(
        ChangeRequest(change_summary="upgrade orders")
    )
    assert first.kind == "clarification_required"
    harness.model._scripted.append(
        _model_response(_plan_payload(harness.evidence_ref))
    )

    resumed = harness.service.answer(
        "session-1",
        {
            "service_id": "orders",
            "current_version": "1.0",
            "target_version": "2.0",
            "success_conditions": ("health check passes",),
        },
    )

    assert resumed.kind == "plan_ready"
    assert resumed.plan.version == 1


def test_budget_exhaustion_is_a_structured_terminal_result() -> None:
    harness = _harness([ModelBudgetExceeded("no budget")])

    result = harness.service.start(_complete_request())

    assert result.kind == "budget_exhausted"
    assert harness.model.call_count == 1


def test_model_provider_failure_is_a_structured_terminal_result() -> None:
    harness = _harness([TransientModelError("provider timeout")])

    result = harness.service.start(_complete_request())

    assert result.kind == "planning_rejected"
    assert result.errors == (
        "model_gateway_error: provider timeout",
    )
