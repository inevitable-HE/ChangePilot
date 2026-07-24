from __future__ import annotations

from collections.abc import Callable, Mapping
from uuid import uuid4

from changepilot.planning.application.mapping import WorkflowDefinitionMapper
from changepilot.planning.domain.failures import PlanValidationError
from changepilot.planning.domain.models import (
    ChangeRequest,
    PlanReady,
    PlanningResult,
    PreparedWorkflow,
)
from changepilot.planning.ports.knowledge import KnowledgeStore
from changepilot.planning.ports.persistence import PlanningRepository


class PlanningService:
    def __init__(
        self,
        *,
        graph: object,
        repository: PlanningRepository,
        knowledge: KnowledgeStore,
        mapper: WorkflowDefinitionMapper,
        identifiers: Callable[[], str] | None = None,
    ) -> None:
        self._graph = graph
        self._repository = repository
        self._knowledge = knowledge
        self._mapper = mapper
        self._identifiers = identifiers or (lambda: str(uuid4()))

    def start(
        self,
        request: ChangeRequest,
        *,
        session_id: str | None = None,
    ) -> PlanningResult:
        selected_id = session_id or self._identifiers()
        self._repository.create_session(selected_id, request)
        return self._run(selected_id, request)

    def answer(
        self,
        session_id: str,
        answers: Mapping[str, object],
    ) -> PlanningResult:
        current = self._repository.get_request(session_id)
        payload = current.model_dump(mode="python")
        for field, value in answers.items():
            if field not in ChangeRequest.model_fields:
                raise ValueError(f"unknown clarification field {field!r}")
            if field in {"success_conditions", "constraints"} and isinstance(value, str):
                value = (value,)
            payload[field] = value
        updated = ChangeRequest.model_validate(payload)
        self._repository.update_request(session_id, updated)
        return self._run(session_id, updated)

    def prepare_workflow(self, session_id: str) -> PreparedWorkflow:
        plan = self._repository.get_latest_plan(session_id)
        latest_result = self._repository.get_latest_result(session_id)
        if not isinstance(latest_result, PlanReady):
            raise PlanValidationError("latest planning result is not ready")
        if plan.version != latest_result.plan.version:
            raise PlanValidationError("latest plan version has changed")
        if plan.knowledge_snapshot_digest != self._knowledge.snapshot().digest:
            raise PlanValidationError(
                "knowledge changed after planning; create a new plan version"
            )
        prepared = self._mapper.prepare(plan, latest_result.evidence)
        self._repository.bind_prepared_workflow(session_id, prepared)
        return prepared

    def _run(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> PlanningResult:
        attempt_no = self._repository.next_attempt_number(session_id)
        plan_version = self._repository.next_plan_version(session_id)
        output = self._graph.invoke(
            {
                "session_id": session_id,
                "request": request,
                "plan_version": plan_version,
            }
        )
        result = output["result"]
        self._repository.save_attempt(
            session_id,
            attempt_no,
            request,
            result,
        )
        if isinstance(result, PlanReady):
            self._repository.save_plan(session_id, result.plan)
        return result
