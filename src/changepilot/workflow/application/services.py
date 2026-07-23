from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType
from typing import Callable

from pydantic import BaseModel, ConfigDict, field_validator

from changepilot.workflow.application.tooling import ToolRegistry, redact
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState
from changepilot.workflow.ports.clock import Clock
from changepilot.workflow.ports.identifiers import IdentifierGenerator


class _ImmutableDTO(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        arbitrary_types_allowed=True,
    )


class ErrorDTO(_ImmutableDTO):
    error_class: str
    error_message: str


class StepDTO(_ImmutableDTO):
    step_id: str
    state: str
    revision: int
    completion_sequence: int | None


class PendingApprovalDTO(_ImmutableDTO):
    request_id: str
    step_id: str
    tool_name: str
    tool_version: str
    binding_digest: str
    risk_reasons: tuple[str, ...]
    redacted_arguments: object
    created_at: str
    version: int
    status: str

    @field_validator("redacted_arguments", mode="before")
    @classmethod
    def _freeze_arguments(cls, value: object) -> object:
        return _freeze_value(value)


class RunDTO(_ImmutableDTO):
    run_id: str
    definition_id: str
    definition_version: int
    definition_digest: str
    state: str
    revision: int
    steps: tuple[StepDTO, ...]
    pending_approval: PendingApprovalDTO | None
    original_error: ErrorDTO | None
    compensation_error: ErrorDTO | None


class EventDTO(_ImmutableDTO):
    sequence: int
    run_id: str
    step_id: str | None
    event_type: str
    occurred_at: str
    previous_state: str
    new_state: str
    previous_revision: int
    revision: int
    attempt_id: str | None
    attempt_no: int | None
    phase: str | None
    error_class: str | None
    payload: object | None

    @field_validator("payload", mode="before")
    @classmethod
    def _freeze_payload(cls, value: object) -> object:
        return _freeze_value(value)


class DefinitionConflictError(ValueError):
    """Raised when an existing definition identity has different content."""


class WorkflowService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], object],
        tools: ToolRegistry,
        clock: Clock,
        identifiers: IdentifierGenerator,
    ) -> None:
        self._uow_factory = uow_factory
        self._tools = tools
        self._clock = clock
        self._identifiers = identifiers

    def create_run(self, definition_payload: dict[str, object]) -> str:
        definition = WorkflowDefinition.from_mapping(definition_payload, self._tools)
        run_id = self._identifiers.new()
        run = WorkflowRun.new(
            run_id=run_id,
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_digest=definition.digest,
        )
        event = AuditEvent(
            run_id=run_id,
            step_id=None,
            event_type="workflow.created",
            occurred_at=self._clock.now(),
            previous_state=RunState.PENDING.value,
            new_state=RunState.PENDING.value,
            previous_revision=0,
            revision=0,
            summary={
                "definition_id": definition.definition_id,
                "definition_version": definition.version,
                "definition_digest": definition.digest,
                "step_count": len(definition.steps),
            },
        )
        with self._uow_factory() as uow:
            existing = uow.definitions.get(definition.definition_id, definition.version)
            if existing is None:
                uow.definitions.add(definition)
            elif existing.digest != definition.digest:
                raise DefinitionConflictError(
                    "definition identity already exists with different content: "
                    f"{definition.definition_id}@{definition.version}"
                )
            uow.runs.add(run)
            for step in definition.steps:
                uow.steps.add(StepRun.new(run_id, step.id))
            uow.events.append(event)
            uow.commit()
        return run_id

    def cancel_run(self, run_id: str) -> None:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            cancelled, event = run.transition(
                RunState.CANCELLED,
                occurred_at=self._clock.now(),
            )
            uow.runs.save(cancelled, expected_revision=run.revision)
            uow.events.append(event)
            uow.commit()


class QueryService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], object],
        tools: ToolRegistry,
    ) -> None:
        self._uow_factory = uow_factory
        self._tools = tools

    def get_run(self, run_id: str | None) -> RunDTO:
        if not isinstance(run_id, str) or not run_id:
            raise LookupError(f"unknown run {run_id}")
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                raise LookupError(
                    f"unknown definition {run.definition_id}@{run.definition_version}"
                )
            step_runs = tuple(uow.steps.get(run_id, step.id) for step in definition.steps)
            pending = uow.approvals.pending(run_id)
        if any(step is None for step in step_runs):
            raise RuntimeError(f"run {run_id} is missing step state")
        return RunDTO(
            run_id=run.run_id,
            definition_id=run.definition_id,
            definition_version=run.definition_version,
            definition_digest=run.definition_digest,
            state=run.state.value,
            revision=run.revision,
            steps=tuple(
                StepDTO(
                    step_id=step.step_id,
                    state=step.state.value,
                    revision=step.revision,
                    completion_sequence=step.completion_sequence,
                )
                for step in step_runs
                if step is not None
            ),
            pending_approval=self._pending_approval(pending),
            original_error=_error_dto(run.original_error),
            compensation_error=_error_dto(run.compensation_error),
        )

    def list_events(
        self,
        run_id: str,
        after_sequence: int = 0,
    ) -> tuple[EventDTO, ...]:
        if (
            isinstance(after_sequence, bool)
            or not isinstance(after_sequence, int)
            or after_sequence < 0
        ):
            raise ValueError("after_sequence must be a non-negative integer")
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            entries = uow.events.list(run_id, after_sequence=after_sequence)
        step_by_id = (
            {step.id: step for step in definition.steps}
            if definition is not None
            else {}
        )
        return tuple(
            self._event_dto(entry.sequence, entry.event, step_by_id)
            for entry in entries
        )

    @staticmethod
    def _pending_approval(request: object | None) -> PendingApprovalDTO | None:
        if request is None or getattr(request, "status", None) != "pending":
            return None
        return PendingApprovalDTO(
            request_id=request.id,
            step_id=request.step_id,
            tool_name=request.tool_name,
            tool_version=request.tool_version,
            binding_digest=request.binding_digest,
            risk_reasons=tuple(request.risk_reasons),
            redacted_arguments=redact(request.redacted_arguments),
            created_at=request.created_at,
            version=request.version,
            status=str(request.status),
        )

    def _event_dto(
        self,
        sequence: int,
        event: object,
        step_by_id: Mapping[str, object],
    ) -> EventDTO:
        sensitive_paths: tuple[str, ...] = ()
        step = step_by_id.get(getattr(event, "step_id", None))
        if step is not None:
            reference = (
                step.compensation_tool
                if getattr(event, "phase", None) == "compensation"
                else step.tool
            )
            if reference is not None:
                sensitive_paths = self._tools.descriptor_for(
                    reference.name,
                    reference.version,
                ).sensitive_argument_paths
        return EventDTO(
            sequence=sequence,
            run_id=event.run_id,
            step_id=event.step_id,
            event_type=event.event_type,
            occurred_at=event.occurred_at,
            previous_state=event.previous_state,
            new_state=event.new_state,
            previous_revision=event.previous_revision,
            revision=event.revision,
            attempt_id=event.attempt_id,
            attempt_no=event.attempt_no,
            phase=event.phase,
            error_class=event.error_class,
            payload=redact(event.summary, sensitive_paths=sensitive_paths),
        )


class WorkflowDriver:
    def __init__(self, *, recovery: object, coordinator: object) -> None:
        self._recovery = recovery
        self._coordinator = coordinator
        self._started = False

    def start(self) -> tuple[str, ...]:
        recovered = self._recovery.recover_nonterminal_runs()
        self._started = True
        return recovered

    def run_once(self):
        if not self._started:
            raise RuntimeError("driver must start recovery before coordinator dispatch")
        return self._coordinator.run_once()


def _error_dto(value: Mapping[str, str] | None) -> ErrorDTO | None:
    if value is None:
        return None
    return ErrorDTO(
        error_class=value["error_class"],
        error_message=value["error_message"],
    )


def _freeze_value(value: object) -> object:
    if isinstance(value, BaseModel):
        return _freeze_value(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_value(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_value(item) for item in value)
    return value
