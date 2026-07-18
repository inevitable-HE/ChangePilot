from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel

from changepilot.workflow.application.approvals import (
    ApprovalRequest,
    approval_recovery_required,
    approval_binding_digest,
    canonical_json_value,
)
from changepilot.workflow.application.scheduler import ready_steps
from changepilot.workflow.application.tooling import (
    ToolRegistry,
    logical_idempotency_key,
    redact,
)
from changepilot.workflow.domain.definitions import StepDefinition, WorkflowDefinition
from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.domain.runs import StepRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.clock import Clock
from changepilot.workflow.ports.identifiers import IdentifierGenerator
from changepilot.workflow.ports.tools import (
    Tool,
    ToolExecutionContext,
    ToolExecutionPhase,
    ToolRisk,
)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    run_id: str
    step_id: str
    attempt_no: int
    status: str
    effect_applied: bool = False
    result: object | None = None
    error_class: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CoordinationReport:
    run_id: str | None
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    blocked_reason: str | None = None


class OutcomePersistenceError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class StepAttempt:
    run_id: str
    step_id: str
    attempt_no: int
    phase: str
    attempt_id: str
    status: str
    idempotency_key: str
    started_at: str
    effect_applied: bool = False
    completed_at: str | None = None
    next_attempt_at: str | None = None
    result: object | None = None
    error_class: str | None = None
    error_message: str | None = None

    @property
    def number(self) -> int:
        return self.attempt_no


@dataclass(frozen=True, slots=True)
class _ExecutionRequest:
    tool: Tool
    context: ToolExecutionContext
    arguments: BaseModel


@dataclass(frozen=True, slots=True)
class _PreparedAttempt:
    step: StepDefinition
    attempt: StepAttempt
    request: _ExecutionRequest
    sensitive_paths: tuple[str, ...]


@dataclass(slots=True)
class _OwnedFuture:
    request: _ExecutionRequest
    future: Future[ExecutionOutcome]
    sensitive_paths: tuple[str, ...]
    outcome_persisted: bool = False


class Coordinator:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], object],
        tools: ToolRegistry,
        clock: Clock,
        identifiers: IdentifierGenerator,
        run_id_selector: Callable[[], str | None],
        concurrency: int = 4,
    ) -> None:
        if (
            isinstance(concurrency, bool)
            or not isinstance(concurrency, int)
            or not 1 <= concurrency <= 16
        ):
            raise ValueError("concurrency must be within 1..16")
        self._uow_factory = uow_factory
        self._tools = tools
        self._clock = clock
        self._identifiers = identifiers
        self._run_id_selector = run_id_selector
        self._concurrency = concurrency
        self._executor = ThreadPoolExecutor(
            max_workers=concurrency,
            thread_name_prefix="changepilot-tool",
        )
        self._owned_futures: dict[Future[ExecutionOutcome], _OwnedFuture] = {}
        self._closed = False

    def run_once(self) -> CoordinationReport:
        if self._closed:
            return CoordinationReport(run_id=None, blocked_reason="coordinator_closed")

        completed, collection_failed = self._collect_outcomes()

        run_id = self._run_id_selector()
        if run_id is None:
            return CoordinationReport(run_id=None, blocked_reason="no_run_selected")

        with self._uow_factory() as uow:
            selected_run = uow.runs.get(run_id)
        if (
            selected_run is not None
            and selected_run.state is RunState.WAITING_APPROVAL
            and self._has_persisted_recovery_work(run_id)
        ):
            return CoordinationReport(
                run_id=run_id,
                completed=_completed_for_run(completed, run_id),
                blocked_reason="approval_recovery_required",
            )
        if selected_run is not None and selected_run.state in _TERMINAL_RUN_STATES:
            return CoordinationReport(
                run_id=run_id,
                completed=_completed_for_run(completed, run_id),
                blocked_reason="run_terminal",
            )
        if selected_run is not None and selected_run.state not in _FORWARD_RUN_STATES:
            return CoordinationReport(
                run_id=run_id,
                completed=_completed_for_run(completed, run_id),
                blocked_reason="run_not_forward",
            )
        if collection_failed:
            return CoordinationReport(
                run_id=run_id,
                completed=_completed_for_run(completed, run_id),
                blocked_reason=ErrorClass.INTERNAL_CONSISTENCY.value,
            )

        self._promote_due_retries(run_id)
        self._promote_dependency_ready_steps(run_id)
        definition, step_runs = self._load_definition_and_steps(run_id)
        available_capacity = self._available_capacity()
        ready_candidates = tuple(
            step
            for step in ready_steps(definition, step_runs)
            if _step_run(step_runs, step.id).state is StepState.READY
        )
        protected_step = next(
            (step for step in ready_candidates if self._requires_approval(step)),
            None,
        )
        if protected_step is not None:
            approval_block = self._handle_approval_barrier(
                run_id,
                definition,
                protected_step,
            )
            if approval_block is not None:
                return CoordinationReport(
                    run_id=run_id,
                    completed=_completed_for_run(completed, run_id),
                    blocked_reason=approval_block,
                )
            ready_candidates = (protected_step,)
        candidates = ready_candidates[:available_capacity]
        if not candidates:
            with self._uow_factory() as uow:
                run = uow.runs.get(run_id)
            if run is not None and run.state in _TERMINAL_RUN_STATES:
                blocked_reason = "run_terminal"
            elif ready_candidates and available_capacity == 0:
                blocked_reason = "capacity"
            elif any(step_run.state is StepState.RETRY_WAIT for step_run in step_runs):
                blocked_reason = "retry_backoff"
            elif any(step_run.state is StepState.RESULT_UNKNOWN for step_run in step_runs):
                blocked_reason = "result_unknown"
            elif any(step_run.state is StepState.RUNNING for step_run in step_runs):
                blocked_reason = "in_flight"
            else:
                blocked_reason = ErrorClass.INTERNAL_CONSISTENCY.value
            return CoordinationReport(
                run_id=run_id,
                completed=_completed_for_run(completed, run_id),
                blocked_reason=blocked_reason,
            )

        try:
            prepared_attempts = tuple(
                self._prepare_attempt(run_id, step) for step in candidates
            )
        except Exception:
            return CoordinationReport(
                run_id=run_id,
                completed=_completed_for_run(completed, run_id),
                blocked_reason=ErrorClass.INTERNAL_CONSISTENCY.value,
            )

        dispatched: list[str] = []
        for prepared in prepared_attempts:
            self._commit_attempt_started(prepared)
            dispatched.append(prepared.step.id)
            try:
                future = self._executor.submit(_execute_request, prepared.request)
            except Exception:
                self._persist_outcome(
                    _failed_outcome(
                        prepared.request,
                        error_class=ErrorClass.RETRYABLE,
                        safe_message="tool submission failed",
                    ),
                    sensitive_paths=prepared.sensitive_paths,
                )
                with self._uow_factory() as uow:
                    run = uow.runs.get(run_id)
                if run is not None and run.state in _TERMINAL_RUN_STATES:
                    break
                continue
            self._owned_futures[future] = _OwnedFuture(
                request=prepared.request,
                future=future,
                sensitive_paths=prepared.sensitive_paths,
            )

        just_completed, collection_failed = self._collect_outcomes()
        completed += just_completed

        return CoordinationReport(
            run_id=run_id,
            dispatched=tuple(dispatched),
            completed=_completed_for_run(completed, run_id),
            blocked_reason=(
                ErrorClass.INTERNAL_CONSISTENCY.value if collection_failed else None
            ),
        )

    def close(self, *, wait: bool = False) -> None:
        if self._closed and not wait:
            return

        _, persistence_failed = self._collect_outcomes()
        if persistence_failed:
            raise OutcomePersistenceError("outcome persistence failed during close")
        if wait:
            self._executor.shutdown(wait=True, cancel_futures=False)
            _, persistence_failed = self._collect_outcomes()
            if persistence_failed:
                raise OutcomePersistenceError("outcome persistence failed during close")
        else:
            for owned in tuple(self._owned_futures.values()):
                if owned.outcome_persisted:
                    continue
                if owned.future.done():
                    outcome = _outcome_from_future(owned)
                elif owned.future.cancel():
                    outcome = _failed_outcome(
                        owned.request,
                        error_class=ErrorClass.RETRYABLE,
                        safe_message="tool submission cancelled",
                    )
                else:
                    outcome = _failed_outcome(
                        owned.request,
                        error_class=ErrorClass.RESULT_UNKNOWN,
                        safe_message="tool result unknown",
                    )
                try:
                    self._persist_outcome(
                        outcome,
                        sensitive_paths=owned.sensitive_paths,
                    )
                except Exception as exc:
                    raise OutcomePersistenceError(
                        "outcome persistence failed during close"
                    ) from exc
                owned.outcome_persisted = True
                if owned.future.done():
                    self._owned_futures.pop(owned.future, None)
            self._executor.shutdown(wait=False, cancel_futures=True)
        self._closed = True

    def _promote_due_retries(self, run_id: str) -> None:
        now = _parse_timestamp(self._clock.now())
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None or run.state in _TERMINAL_RUN_STATES:
                return
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                return
            changed = False
            for step in definition.steps:
                step_run = uow.steps.get(run_id, step.id)
                if step_run is None or step_run.state is not StepState.RETRY_WAIT:
                    continue
                attempts = uow.attempts.list(run_id, step_id=step.id)
                if not attempts or attempts[-1].next_attempt_at is None:
                    continue
                if _parse_timestamp(attempts[-1].next_attempt_at) > now:
                    continue
                ready, event = step_run.transition(
                    StepState.READY,
                    occurred_at=self._clock.now(),
                )
                uow.steps.save(ready, expected_revision=step_run.revision)
                uow.events.append(event)
                changed = True
            if changed:
                uow.commit()

    def _promote_dependency_ready_steps(self, run_id: str) -> None:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None or run.state in _TERMINAL_RUN_STATES:
                return
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                return
            step_runs = tuple(uow.steps.get(run_id, step.id) for step in definition.steps)
            concrete_runs = tuple(item for item in step_runs if item is not None)
            changed = False
            for step in ready_steps(definition, concrete_runs):
                step_run = _step_run(concrete_runs, step.id)
                if step_run.state is not StepState.PENDING:
                    continue
                ready, event = step_run.transition(
                    StepState.READY,
                    occurred_at=self._clock.now(),
                )
                uow.steps.save(ready, expected_revision=step_run.revision)
                uow.events.append(event)
                changed = True
            if changed:
                uow.commit()

    def _load_definition_and_steps(
        self,
        run_id: str,
    ) -> tuple[WorkflowDefinition, tuple[StepRun, ...]]:
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
        if any(step_run is None for step_run in step_runs):
            raise RuntimeError(f"run {run_id} is missing step state")
        return definition, tuple(step_runs)

    def _available_capacity(self) -> int:
        active = sum(
            not owned.future.done()
            for owned in self._owned_futures.values()
        )
        return max(0, self._concurrency - active)

    def _requires_approval(self, step: StepDefinition) -> bool:
        descriptor = self._tools.descriptor_for(step.tool.name, step.tool.version)
        return step.risk.casefold() == "high" or descriptor.risk is ToolRisk.HIGH

    def _approval_snapshot(
        self,
        definition: WorkflowDefinition,
        step: StepDefinition,
    ) -> tuple[object, str, tuple[str, ...]]:
        descriptor = self._tools.descriptor_for(step.tool.name, step.tool.version)
        canonical_arguments = canonical_json_value(step.arguments)
        redacted_arguments = redact(
            canonical_arguments,
            sensitive_paths=descriptor.sensitive_argument_paths,
        )
        binding_digest = approval_binding_digest(
            definition.digest,
            step.id,
            step.tool.name,
            step.tool.version,
            redacted_arguments,
        )
        risk_reasons = tuple(
            reason
            for reason, applies in (
                ("step_risk_high", step.risk.casefold() == "high"),
                ("tool_risk_high", descriptor.risk is ToolRisk.HIGH),
            )
            if applies
        )
        return redacted_arguments, binding_digest, risk_reasons

    def _handle_approval_barrier(
        self,
        run_id: str,
        definition: WorkflowDefinition,
        step: StepDefinition,
    ) -> str | None:
        _arguments, binding_digest, _reasons = self._approval_snapshot(definition, step)
        if self._has_unpersisted_outcome(run_id):
            return "approval_draining"
        if self._has_persisted_recovery_work(run_id):
            return "approval_recovery_required"
        with self._uow_factory() as uow:
            approvals = uow.approvals.list(run_id)
        if any(
            request.status == "approved"
            and request.matches(
                definition_digest=definition.digest,
                step_id=step.id,
                tool_name=step.tool.name,
                tool_version=step.tool.version,
                binding_digest=binding_digest,
            )
            for request in approvals
        ):
            return None
        return self._establish_approval_barrier(run_id, step.id)

    def _has_unpersisted_outcome(self, run_id: str) -> bool:
        return any(
            owned.request.context.run_id == run_id and not owned.outcome_persisted
            for owned in self._owned_futures.values()
        )

    def _has_persisted_recovery_work(self, run_id: str) -> bool:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                raise LookupError(
                    f"unknown definition {run.definition_id}@{run.definition_version}"
                )
            return self._uow_has_persisted_recovery_work(uow, run, definition)

    @staticmethod
    def _uow_has_persisted_recovery_work(
        uow: object,
        run: object,
        definition: WorkflowDefinition,
    ) -> bool:
        if approval_recovery_required(uow, run):
            return True
        if any(
            uow.steps.get(run.run_id, step.id).state is StepState.RUNNING
            for step in definition.steps
        ):
            return True
        return any(
            getattr(attempt, "status", None) == "running"
            for attempt in uow.attempts.list(run.run_id)
        )

    def _establish_approval_barrier(self, run_id: str, step_id: str) -> str | None:
        occurred_at = self._clock.now()
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                raise LookupError(
                    f"unknown definition {run.definition_id}@{run.definition_version}"
                )
            if self._uow_has_persisted_recovery_work(uow, run, definition):
                return "approval_recovery_required"
            step = next(item for item in definition.steps if item.id == step_id)
            redacted_arguments, binding_digest, risk_reasons = self._approval_snapshot(
                definition,
                step,
            )
            approvals = uow.approvals.list(run_id)
            matching_approved = next(
                (
                    request
                    for request in approvals
                    if request.status == "approved"
                    and request.matches(
                        definition_digest=definition.digest,
                        step_id=step.id,
                        tool_name=step.tool.name,
                        tool_version=step.tool.version,
                        binding_digest=binding_digest,
                    )
                ),
                None,
            )
            if matching_approved is not None:
                return None

            matching_pending = None
            for request in approvals:
                if request.status not in {"pending", "approved"}:
                    continue
                if request.matches(
                    definition_digest=definition.digest,
                    step_id=step.id,
                    tool_name=step.tool.name,
                    tool_version=step.tool.version,
                    binding_digest=binding_digest,
                ):
                    if request.status == "pending":
                        matching_pending = request
                    continue
                invalidated = replace(
                    request,
                    version=request.version + 1,
                    status="invalidated",
                    decision=None,
                    actor=None,
                    reason=None,
                    decided_at=occurred_at,
                )
                uow.approvals.save(
                    invalidated,
                    expected_version=request.version,
                )
                uow.events.append(
                    AuditEvent.approval_changed(
                        run_id=run_id,
                        step_id=request.step_id,
                        event_type="approval.invalidated",
                        occurred_at=occurred_at,
                        previous_state=run.state.value,
                        new_state=run.state.value,
                        previous_revision=run.revision,
                        revision=run.revision,
                        request_id=request.id,
                        binding_digest=request.binding_digest,
                        status="invalidated",
                    )
                )

            changed_run = run
            if run.state is not RunState.WAITING_APPROVAL:
                changed_run, run_event = run.transition(
                    RunState.WAITING_APPROVAL,
                    occurred_at=occurred_at,
                )
                uow.runs.save(changed_run, expected_revision=run.revision)
                uow.events.append(run_event)

            if matching_pending is None:
                matching_pending = ApprovalRequest(
                    id=self._identifiers.new(),
                    run_id=run_id,
                    definition_digest=definition.digest,
                    step_id=step.id,
                    tool_name=step.tool.name,
                    tool_version=step.tool.version,
                    redacted_arguments=redacted_arguments,
                    binding_digest=binding_digest,
                    risk_reasons=risk_reasons,
                    created_at=occurred_at,
                )
                uow.approvals.add(matching_pending)
                uow.events.append(
                    AuditEvent.approval_changed(
                        run_id=run_id,
                        step_id=step.id,
                        event_type="approval.created",
                        occurred_at=occurred_at,
                        previous_state=run.state.value,
                        new_state=changed_run.state.value,
                        previous_revision=run.revision,
                        revision=changed_run.revision,
                        request_id=matching_pending.id,
                        binding_digest=binding_digest,
                        status="pending",
                        redacted_arguments=redacted_arguments,
                    )
                )
            uow.commit()
        return "approval_pending"

    def _collect_outcomes(self) -> tuple[tuple[ExecutionOutcome, ...], bool]:
        now = _parse_timestamp(self._clock.now())
        persisted: list[ExecutionOutcome] = []
        persistence_failed = False
        for future, owned in tuple(self._owned_futures.items()):
            if owned.outcome_persisted:
                if future.done():
                    _consume_late_future(future)
                    del self._owned_futures[future]
                continue

            if future.done():
                outcome = _outcome_from_future(owned)
            elif now >= owned.request.context.deadline:
                outcome = _failed_outcome(
                    owned.request,
                    error_class=ErrorClass.RESULT_UNKNOWN,
                    safe_message="tool result unknown",
                )
            else:
                continue

            try:
                self._persist_outcome(
                    outcome,
                    sensitive_paths=owned.sensitive_paths,
                )
            except Exception:
                persistence_failed = True
                continue

            persisted.append(outcome)
            owned.outcome_persisted = True
            if future.done():
                del self._owned_futures[future]
        return tuple(persisted), persistence_failed

    def _prepare_attempt(
        self,
        run_id: str,
        step: StepDefinition,
    ) -> _PreparedAttempt:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            step_run = uow.steps.get(run_id, step.id)
            if step_run is None:
                raise LookupError(f"unknown step {run_id}:{step.id}")
            attempts = uow.attempts.list(run_id, step_id=step.id)
        tool = self._tools.resolve(step.tool.name, step.tool.version)
        arguments = self._tools.coerce_arguments(
            step.tool.name,
            step.tool.version,
            step.arguments,
        )
        attempt_no = max((attempt.attempt_no for attempt in attempts), default=0) + 1
        key = logical_idempotency_key(
            run_id=run_id,
            step_id=step.id,
            tool_name=step.tool.name,
            tool_version=step.tool.version,
            phase=ToolExecutionPhase.FORWARD,
        )
        started_at = self._clock.now()
        deadline = _parse_timestamp(started_at) + timedelta(
            seconds=tool.descriptor.default_timeout_seconds
        )
        request = _ExecutionRequest(
            tool=tool,
            context=ToolExecutionContext(
                run_id=run_id,
                step_id=step.id,
                attempt_number=attempt_no,
                phase=ToolExecutionPhase.FORWARD,
                logical_idempotency_key=key,
                deadline=deadline,
                metadata={},
            ),
            arguments=arguments,
        )
        return _PreparedAttempt(
            step=step,
            attempt=StepAttempt(
                run_id=run_id,
                step_id=step.id,
                attempt_no=attempt_no,
                phase=ToolExecutionPhase.FORWARD.value,
                attempt_id=self._identifiers.new(),
                status="running",
                idempotency_key=key,
                started_at=started_at,
            ),
            request=request,
            sensitive_paths=tool.descriptor.sensitive_argument_paths,
        )

    def _commit_attempt_started(self, prepared: _PreparedAttempt) -> None:
        attempt = prepared.attempt
        with self._uow_factory() as uow:
            run = uow.runs.get(attempt.run_id)
            if run is None:
                raise LookupError(f"unknown run {attempt.run_id}")
            if run.state is RunState.PENDING:
                running_run, run_event = run.transition(
                    RunState.RUNNING,
                    occurred_at=attempt.started_at,
                )
                uow.runs.save(running_run, expected_revision=run.revision)
                uow.events.append(run_event)
            elif run.state is not RunState.RUNNING:
                raise RuntimeError("run is not in a forward execution state")

            step_run = uow.steps.get(attempt.run_id, attempt.step_id)
            if step_run is None:
                raise LookupError(f"unknown step {attempt.run_id}:{attempt.step_id}")
            running_step, step_event = step_run.transition(
                StepState.RUNNING,
                occurred_at=attempt.started_at,
            )
            uow.steps.save(running_step, expected_revision=step_run.revision)
            uow.attempts.add(attempt)
            uow.events.append(step_event)
            uow.events.append(
                AuditEvent.tool_attempt_started(
                    run_id=attempt.run_id,
                    step_id=attempt.step_id,
                    attempt_id=attempt.attempt_id,
                    attempt_no=attempt.attempt_no,
                    phase=attempt.phase,
                    occurred_at=attempt.started_at,
                    state=running_step.state.value,
                    revision=running_step.revision,
                )
            )
            uow.commit()

    def _persist_outcome(
        self,
        outcome: ExecutionOutcome,
        *,
        sensitive_paths: tuple[str, ...],
    ) -> None:
        with self._uow_factory() as uow:
            run = uow.runs.get(outcome.run_id)
            if run is None:
                raise LookupError(f"unknown run {outcome.run_id}")
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                raise LookupError(
                    f"unknown definition {run.definition_id}@{run.definition_version}"
                )
            step_definition = next(
                step for step in definition.steps if step.id == outcome.step_id
            )
            step_run = uow.steps.get(outcome.run_id, outcome.step_id)
            if step_run is None:
                raise LookupError(f"unknown step {outcome.run_id}:{outcome.step_id}")
            attempt = next(
                item
                for item in uow.attempts.list(outcome.run_id, step_id=outcome.step_id)
                if item.attempt_no == outcome.attempt_no
            )
            completed_at = self._clock.now()
            if outcome.status == "success":
                next_state = StepState.SUCCEEDED
                next_attempt_at = None
            elif (
                outcome.error_class == ErrorClass.RETRYABLE.value
                and outcome.attempt_no < step_definition.retry.max_attempts
            ):
                next_state = StepState.RETRY_WAIT
                next_attempt_at = (
                    _parse_timestamp(completed_at)
                    + timedelta(seconds=step_definition.retry.initial_backoff_seconds)
                ).isoformat()
            elif outcome.error_class == ErrorClass.RESULT_UNKNOWN.value:
                next_state = StepState.RESULT_UNKNOWN
                next_attempt_at = None
            else:
                next_state = StepState.FAILED
                next_attempt_at = None

            changed_step, event = step_run.transition(
                next_state,
                occurred_at=completed_at,
            )
            redacted_result = redact(
                outcome.result,
                sensitive_paths=sensitive_paths,
            )
            completed_attempt = replace(
                attempt,
                status=outcome.status,
                effect_applied=outcome.effect_applied,
                completed_at=completed_at,
                next_attempt_at=next_attempt_at,
                result=redacted_result,
                error_class=outcome.error_class,
                error_message=outcome.error_message,
            )
            uow.steps.save(changed_step, expected_revision=step_run.revision)
            uow.attempts.save(completed_attempt)
            uow.events.append(event)
            uow.events.append(
                AuditEvent.tool_attempt_completed(
                    run_id=outcome.run_id,
                    step_id=outcome.step_id,
                    attempt_id=attempt.attempt_id,
                    attempt_no=attempt.attempt_no,
                    phase=attempt.phase,
                    occurred_at=completed_at,
                    state=changed_step.state.value,
                    revision=changed_step.revision,
                    error_class=outcome.error_class,
                    summary=_outcome_summary(outcome, redacted_result),
                )
            )
            if (
                run.state is RunState.RUNNING
                and next_state is StepState.SUCCEEDED
                and all(
                step.id == outcome.step_id
                or uow.steps.get(outcome.run_id, step.id).state is StepState.SUCCEEDED
                for step in definition.steps
                )
            ):
                succeeded_run, run_event = run.transition(
                    RunState.SUCCEEDED,
                    occurred_at=completed_at,
                )
                uow.runs.save(succeeded_run, expected_revision=run.revision)
                uow.events.append(run_event)
            elif run.state is RunState.RUNNING and next_state is StepState.FAILED:
                failed_run, run_event = run.transition(
                    RunState.FAILED,
                    occurred_at=completed_at,
                )
                uow.runs.save(failed_run, expected_revision=run.revision)
                uow.events.append(run_event)
            uow.commit()


def _execute_request(request: _ExecutionRequest) -> ExecutionOutcome:
    context = request.context
    try:
        result = request.tool.execute(context, request.arguments)
    except Exception as exc:
        error_class = _classify_tool_exception(exc)
        return _failed_outcome(
            request,
            error_class=error_class,
            safe_message=(
                "tool result unknown"
                if error_class is ErrorClass.RESULT_UNKNOWN
                else "tool execution failed"
            ),
        )
    return ExecutionOutcome(
        run_id=context.run_id,
        step_id=context.step_id,
        attempt_no=context.attempt_number,
        status="success",
        effect_applied=result.effect_applied,
        result=result.output,
    )


def _outcome_from_future(owned: _OwnedFuture) -> ExecutionOutcome:
    try:
        return owned.future.result()
    except Exception as exc:
        error_class = _classify_tool_exception(exc)
        return _failed_outcome(
            owned.request,
            error_class=error_class,
            safe_message=(
                "tool result unknown"
                if error_class is ErrorClass.RESULT_UNKNOWN
                else "tool execution failed"
            ),
        )


def _failed_outcome(
    request: _ExecutionRequest,
    *,
    error_class: ErrorClass,
    safe_message: str,
) -> ExecutionOutcome:
    context = request.context
    return ExecutionOutcome(
        run_id=context.run_id,
        step_id=context.step_id,
        attempt_no=context.attempt_number,
        status="failed",
        error_class=error_class.value,
        error_message=safe_message,
    )


def _classify_tool_exception(exc: Exception) -> ErrorClass:
    if isinstance(exc, DomainError):
        return exc.error_class
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return ErrorClass.RESULT_UNKNOWN
    return ErrorClass.PERMANENT


def _consume_late_future(future: Future[ExecutionOutcome]) -> None:
    try:
        future.result()
    except Exception:
        return


def _outcome_summary(outcome: ExecutionOutcome, redacted_result: object) -> object:
    if outcome.status == "success":
        return {
            "status": "success",
            "effect_applied": outcome.effect_applied,
            "result": redacted_result,
        }
    return {"status": "failed", "error_class": outcome.error_class}


def _completed_for_run(
    outcomes: tuple[ExecutionOutcome, ...],
    run_id: str,
) -> tuple[str, ...]:
    return tuple(
        outcome.step_id
        for outcome in outcomes
        if outcome.run_id == run_id and outcome.status == "success"
    )


def _step_run(step_runs: tuple[StepRun, ...], step_id: str) -> StepRun:
    return next(step_run for step_run in step_runs if step_run.step_id == step_id)


def _parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


_TERMINAL_RUN_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.COMPENSATED,
        RunState.CANCELLED,
        RunState.MANUAL_INTERVENTION,
    }
)

_FORWARD_RUN_STATES = frozenset({RunState.PENDING, RunState.RUNNING})
