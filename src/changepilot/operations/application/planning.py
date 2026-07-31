from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from changepilot.operations.domain.models import (
    ChangeRequestInput,
    DemoScenario,
    PlanningEvidenceView,
    PlanningModelUsageView,
    PlanningStepView,
    PlanningTraceView,
)
from changepilot.planning.adapters.knowledge.sqlite import (
    SQLiteKnowledgeStore,
    initialize_planning_schema,
)
from changepilot.planning.adapters.models.deepseek import (
    DeepSeekConfig,
    DeepSeekModelGateway,
)
from changepilot.planning.adapters.persistence import (
    SQLitePlanningRepository,
)
from changepilot.planning.application.graph import build_planning_graph
from changepilot.planning.application.ingestion import (
    RunbookIngestionService,
    load_runbook,
)
from changepilot.planning.application.mapping import WorkflowDefinitionMapper
from changepilot.planning.application.model_gateway import (
    BudgetedModelGateway,
    InMemoryModelCache,
    InMemoryUsageRecorder,
    ModelBudget,
)
from changepilot.planning.application.nodes import PlanningNodes
from changepilot.planning.application.retrieval import HybridRetriever
from changepilot.planning.application.services import PlanningService
from changepilot.planning.application.validation import PlanValidator
from changepilot.planning.domain.models import (
    BudgetExhausted,
    ChangePlan,
    ChangeRequest,
    ClarificationRequired,
    EvidenceRef,
    PlanReady,
    PlanStep,
    PlanToolRef,
    PlanningRejected,
    RetrievedEvidence,
    TrustLevel,
)
from changepilot.planning.domain.policies import (
    PlanningPolicyCatalog,
    PlanningToolPolicy,
)
from changepilot.planning.ports.models import (
    ModelGateway,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)
from changepilot.workflow.adapters.persistence.sqlite import (
    create_sqlite_engine,
)
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.ports.tools import ToolRisk


_ROOT = Path(__file__).resolve().parents[4]
_PROMPT_VERSION = "operations-planning-v1"
_POLICY_VERSION = "sandbox-policy-v1"


@dataclass(frozen=True, slots=True)
class AgentPlanningOutcome:
    trace: PlanningTraceView
    prepared_payload: dict[str, Any] | None
    clarification_questions: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()


class DeterministicSandboxModelGateway:
    def __init__(
        self,
        *,
        sandbox_id: str,
        database_fingerprint: str,
    ) -> None:
        self._sandbox_id = sandbox_id
        self._database_fingerprint = database_fingerprint

    def generate(self, request: ModelRequest) -> ModelResponse:
        context = json.loads(request.messages[-1].content)
        change = context["request"]
        identity = context["required_plan_identity"]
        evidence = tuple(
            RetrievedEvidence.model_validate(item, strict=False)
            for item in context["evidence"]
        )
        plan = self._build_plan(change, identity, evidence)
        payload = plan.model_dump(mode="json")
        input_tokens = max(1, len(request.messages[-1].content) // 4)
        output_tokens = max(1, len(json.dumps(payload)) // 4)
        return ModelResponse(
            payload=payload,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=input_tokens + output_tokens,
            ),
            model="deterministic-sandbox-planner",
            latency_ms=1,
            finish_reason="stop",
        )

    def _build_plan(
        self,
        change: dict[str, Any],
        identity: dict[str, Any],
        evidence: tuple[RetrievedEvidence, ...],
    ) -> ChangePlan:
        baseline_references = tuple(
            item.reference
            for item in evidence
            if item.reference.document_id == "order-service-upgrade"
            and item.reference.location.startswith("Order service upgrade")
        )
        schema_references = tuple(
            item.reference
            for item in evidence
            if item.reference.document_id == "schema-migration"
        )
        deployment_references = tuple(
            item.reference
            for item in evidence
            if (
                item.reference.document_id == "rollback-policy"
                or (
                    item.reference.document_id == "order-service-upgrade"
                    and item.reference.location.startswith("Deployment")
                )
            )
        )
        common = {"sandbox_id": self._sandbox_id}

        def step(
            step_id: str,
            tool_name: str,
            *,
            depends_on: tuple[str, ...] = (),
            arguments: dict[str, Any] | None = None,
            compensation_tool: str | None = None,
            evidence_refs: tuple[EvidenceRef, ...] = (),
            rationale: str,
            success_condition: str,
        ) -> PlanStep:
            return PlanStep(
                id=step_id,
                tool=PlanToolRef(name=tool_name, version="1.0.0"),
                arguments=arguments or common,
                depends_on=depends_on,
                compensation_tool=(
                    None
                    if compensation_tool is None
                    else PlanToolRef(
                        name=compensation_tool,
                        version="1.0.0",
                    )
                ),
                compensation_intent=(
                    None
                    if compensation_tool is None
                    else f"reverse {step_id} if later validation fails"
                ),
                rationale=rationale,
                success_conditions=(success_condition,),
                evidence_refs=evidence_refs,
            )

        steps = [
            step(
                "inspect-service",
                "service.inspect",
                evidence_refs=baseline_references,
                rationale="capture the deployed service version",
                success_condition="service reports the expected V1 baseline",
            ),
            step(
                "inspect-db",
                "schema.inspect",
                evidence_refs=baseline_references,
                rationale="capture the schema version and database fingerprint",
                success_condition="schema reports a valid V1 baseline",
            ),
            step(
                "precheck",
                "upgrade.precheck",
                depends_on=("inspect-service", "inspect-db"),
                evidence_refs=baseline_references,
                rationale="verify the service and schema can be upgraded safely",
                success_condition="all upgrade prerequisites pass",
            ),
        ]
        if change["target_version"] != change["current_version"]:
            steps.extend(
                (
                    step(
                        "migrate-schema",
                        "schema.migrate",
                        depends_on=("precheck",),
                        arguments={
                            "sandbox_id": self._sandbox_id,
                            "expected_fingerprint": self._database_fingerprint,
                        },
                        compensation_tool="schema.rollback",
                        evidence_refs=schema_references,
                        rationale="apply the versioned V1 to V2 schema migration",
                        success_condition="migration ledger records V2",
                    ),
                    step(
                        "deploy-v2",
                        "service.deploy-v2",
                        depends_on=("migrate-schema",),
                        compensation_tool="service.restore",
                        evidence_refs=deployment_references,
                        rationale="deploy the V2 service contract",
                        success_condition="service reports version V2",
                    ),
                    step(
                        "health-check",
                        "service.health-check",
                        depends_on=("deploy-v2",),
                        evidence_refs=deployment_references,
                        rationale="verify service and database compatibility",
                        success_condition="V2 health contract succeeds",
                    ),
                    step(
                        "smoke-test",
                        "service.smoke-test",
                        depends_on=("health-check",),
                        evidence_refs=deployment_references,
                        rationale="exercise order reads and the V2 contract",
                        success_condition="order smoke test succeeds",
                    ),
                )
            )
        return ChangePlan(
            plan_id=identity["plan_id"],
            version=identity["version"],
            goal=change["change_summary"],
            assumptions=(
                "execution is restricted to the local order-service sandbox",
            ),
            steps=tuple(steps),
            knowledge_snapshot_digest=identity[
                "knowledge_snapshot_digest"
            ],
            prompt_version=identity["prompt_version"],
            tool_policy_version=identity["tool_policy_version"],
        )


def plan_change(
    *,
    root: Path,
    request_id: str,
    request: ChangeRequestInput,
    tools: ToolRegistry,
    sandbox_id: str,
    database_fingerprint: str,
) -> AgentPlanningOutcome:
    root.mkdir(parents=True, exist_ok=True)
    engine = create_sqlite_engine(root / "planning.db")
    initialize_planning_schema(engine)
    knowledge = SQLiteKnowledgeStore(engine)
    ingestion = RunbookIngestionService(knowledge)
    for name in (
        "order-service-upgrade.md",
        "schema-migration.md",
        "rollback-policy.md",
    ):
        ingestion.ingest(load_runbook(_ROOT / "examples" / "runbooks" / name))

    policies = _planning_policies()
    validator = PlanValidator(tools=tools, policies=policies)
    usage = InMemoryUsageRecorder()
    mode = os.getenv(
        "CHANGEPILOT_PLANNER_MODE",
        "deterministic_mock",
    ).strip()
    inner: ModelGateway
    if mode == "deepseek":
        inner = DeepSeekModelGateway(DeepSeekConfig.from_env())
    elif mode == "deterministic_mock":
        inner = DeterministicSandboxModelGateway(
            sandbox_id=sandbox_id,
            database_fingerprint=database_fingerprint,
        )
    else:
        raise ValueError(
            "CHANGEPILOT_PLANNER_MODE must be deterministic_mock or deepseek"
        )
    model = BudgetedModelGateway(
        inner=inner,
        budget=ModelBudget(
            max_calls=2,
            max_total_tokens=12_000,
            max_estimated_cost_microunits=50_000,
        ),
        cache=InMemoryModelCache(),
        usage=usage,
        cache_namespace=(
            f"{_PROMPT_VERSION}:{knowledge.snapshot().digest}:{request_id}"
        ),
        max_retries=1,
    )
    service = PlanningService(
        graph=build_planning_graph(
            PlanningNodes(
                model=model,
                retriever=HybridRetriever(
                    store=knowledge,
                    index=knowledge,
                ),
                validator=validator,
                prompt_version=_PROMPT_VERSION,
                tool_policy_version=policies.version,
                tool_catalog=_tool_catalog(
                    tools,
                    sandbox_id=sandbox_id,
                    database_fingerprint=database_fingerprint,
                ),
                retrieval_hints=_retrieval_hints(request.scenario),
            )
        ),
        repository=SQLitePlanningRepository(engine),
        knowledge=knowledge,
        mapper=WorkflowDefinitionMapper(
            tools=tools,
            validator=validator,
        ),
    )
    planning_request = ChangeRequest(
        service_id=request.service_id,
        current_version=request.current_version,
        target_version=request.target_version,
        change_summary=request.change_summary,
        success_conditions=request.success_conditions,
        constraints=request.constraints,
    )
    try:
        result = service.start(planning_request, session_id=request_id)
        prepared_payload = None
        if isinstance(result, PlanReady):
            prepared_payload = service.prepare_workflow(
                request_id
            ).payload
        trace = _planning_trace(
            request_id=request_id,
            request=planning_request,
            result=result,
            mode=mode,
            records=usage.records,
        )
        if isinstance(result, ClarificationRequired):
            return AgentPlanningOutcome(
                trace=trace,
                prepared_payload=None,
                clarification_questions=tuple(
                    question.prompt for question in result.questions
                ),
            )
        if isinstance(result, PlanningRejected):
            return AgentPlanningOutcome(
                trace=trace,
                prepared_payload=None,
                errors=result.errors,
            )
        if isinstance(result, BudgetExhausted):
            return AgentPlanningOutcome(
                trace=trace,
                prepared_payload=None,
                errors=("model budget exhausted",),
            )
        return AgentPlanningOutcome(
            trace=trace,
            prepared_payload=prepared_payload,
        )
    finally:
        engine.dispose()


def _retrieval_hints(scenario: DemoScenario) -> tuple[str, ...]:
    if scenario is DemoScenario.READINESS:
        return (
            "inspect current service database state",
            "precheck after both inspections",
        )
    return (
        "order service upgrade schema migration deployment",
        "human approval rollback compensation health smoke test",
    )


def _planning_policies() -> PlanningPolicyCatalog:
    return PlanningPolicyCatalog(
        version=_POLICY_VERSION,
        policies=(
            PlanningToolPolicy(
                tool=PlanToolRef(
                    name="schema.migrate",
                    version="1.0.0",
                ),
                minimum_risk=ToolRisk.HIGH,
                side_effecting=True,
                compensation_required=True,
                minimum_trust=TrustLevel.INTERNAL,
            ),
            PlanningToolPolicy(
                tool=PlanToolRef(
                    name="service.deploy-v2",
                    version="1.0.0",
                ),
                minimum_risk=ToolRisk.MEDIUM,
                side_effecting=True,
                compensation_required=True,
                minimum_trust=TrustLevel.INTERNAL,
            ),
        ),
    )


def _tool_catalog(
    tools: ToolRegistry,
    *,
    sandbox_id: str,
    database_fingerprint: str,
) -> tuple[dict[str, object], ...]:
    catalog = []
    for descriptor in tools.descriptors():
        runtime_arguments: dict[str, object] = {"sandbox_id": sandbox_id}
        if descriptor.name in {"schema.migrate", "schema.rollback"}:
            runtime_arguments["expected_fingerprint"] = database_fingerprint
        catalog.append(
            {
                "name": descriptor.name,
                "version": descriptor.version,
                "risk": descriptor.risk.value,
                "input_schema": descriptor.input_model.model_json_schema(),
                "runtime_arguments": runtime_arguments,
            }
        )
    return tuple(catalog)


def _planning_trace(
    *,
    request_id: str,
    request: ChangeRequest,
    result: object,
    mode: str,
    records: list[object],
) -> PlanningTraceView:
    plan = result.plan if isinstance(result, PlanReady) else None
    evidence = result.evidence if isinstance(result, PlanReady) else ()
    if plan is not None:
        cited_chunk_ids = {
            reference.chunk_id
            for step in plan.steps
            for reference in step.evidence_refs
        }
        evidence = tuple(
            item
            for item in evidence
            if item.reference.chunk_id in cited_chunk_ids
        )
    calls = len(records)
    stages = [
        "normalize_request",
        "check_required_context",
    ]
    if not isinstance(result, ClarificationRequired):
        stages.append("retrieve_knowledge")
    if calls:
        stages.extend(("generate_plan", "validate_plan"))
    if calls > 1:
        stages.extend(("repair_plan", "validate_repaired_plan"))
    if isinstance(result, PlanReady):
        validation_status = "passed"
    elif isinstance(result, ClarificationRequired):
        validation_status = "needs_input"
    else:
        validation_status = "rejected"
    return PlanningTraceView(
        session_id=request_id,
        mode=mode,
        result=result.kind,
        model=records[-1].model if records else "not_called",
        prompt_version=_PROMPT_VERSION,
        tool_policy_version=_POLICY_VERSION,
        knowledge_snapshot_digest=(
            plan.knowledge_snapshot_digest if plan is not None else ""
        ),
        query=" ".join(
            item
            for item in (
                request.service_id,
                request.change_summary,
                " ".join(request.constraints),
            )
            if item
        ),
        plan_version=plan.version if plan is not None else None,
        validation_status=validation_status,
        repair_count=max(0, calls - 1),
        stages=tuple(stages),
        evidence=tuple(_evidence_view(item) for item in evidence),
        steps=(
            tuple(
                PlanningStepView(
                    step_id=step.id,
                    rationale=step.rationale,
                    validation_intent=" · ".join(step.success_conditions),
                    evidence_refs=tuple(
                        reference.chunk_id
                        for reference in step.evidence_refs
                    ),
                )
                for step in plan.steps
            )
            if plan is not None
            else ()
        ),
        model_usage=PlanningModelUsageView(
            calls=calls,
            input_tokens=sum(record.input_tokens for record in records),
            output_tokens=sum(record.output_tokens for record in records),
            total_tokens=sum(record.total_tokens for record in records),
            estimated_cost_microunits=sum(
                record.estimated_cost_microunits for record in records
            ),
            cache_hits=sum(1 for record in records if record.cache_hit),
        ),
    )


def _evidence_view(item: RetrievedEvidence) -> PlanningEvidenceView:
    return PlanningEvidenceView(
        document_id=item.reference.document_id,
        document_version=item.reference.document_version,
        chunk_id=item.reference.chunk_id,
        location=item.reference.location,
        trust_level=item.trust_level.value,
        lexical_rank=item.lexical_rank,
        vector_rank=item.vector_rank,
        hybrid_score=item.hybrid_score,
        excerpt=" ".join(item.text.split())[:240],
    )
