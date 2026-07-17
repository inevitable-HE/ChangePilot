from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel

from changepilot.workflow.application.scheduler import ready_steps
from changepilot.workflow.application.tooling import (
    ToolRegistry,
    logical_idempotency_key,
    redact,
)
from changepilot.workflow.domain.definitions import StepDefinition, WorkflowDefinition
from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.domain.runs import StepRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.clock import Clock
from changepilot.workflow.ports.identifiers import IdentifierGenerator
from changepilot.workflow.ports.tools import (
    Tool,
    ToolExecutionContext,
    ToolExecutionPhase,
)


@dataclass(frozen=True, slots=True)
class ExecutionOutcome:
    run_id: str
    step_id: str
    attempt_no: int
    status: str
    result: object | None = None
    error_class: str | None = None
    error_message: str | None = None


@dataclass(frozen=True, slots=True)
class CoordinationReport:
    run_id: str | None
    dispatched: tuple[str, ...] = ()
    completed: tuple[str, ...] = ()
    blocked_reason: str | None = None


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

    def run_once(self) -> CoordinationReport:
        run_id = self._run_id_selector()
        if run_id is None:
            return CoordinationReport(run_id=None, blocked_reason="no_run_selected")

        with self._uow_factory() as uow:
            selected_run = uow.runs.get(run_id)
        if selected_run is not None and selected_run.state in _TERMINAL_RUN_STATES:
            return CoordinationReport(run_id=run_id, blocked_reason="run_terminal")

        self._promote_due_retries(run_id)
        self._promote_dependency_ready_steps(run_id)
        definition, step_runs = self._load_definition_and_steps(run_id)
        candidates = tuple(
            step
            for step in ready_steps(definition, step_runs)
            if _step_run(step_runs, step.id).state is StepState.READY
        )[: self._concurrency]
        if not candidates:
            with self._uow_factory() as uow:
                run = uow.runs.get(run_id)
            if run is not None and run.state in _TERMINAL_RUN_STATES:
                blocked_reason = "run_terminal"
            elif any(step_run.state is StepState.RETRY_WAIT for step_run in step_runs):
                blocked_reason = "retry_backoff"
            elif any(step_run.state is StepState.RESULT_UNKNOWN for step_run in step_runs):
                blocked_reason = "result_unknown"
            elif any(step_run.state is StepState.RUNNING for step_run in step_runs):
                blocked_reason = "in_flight"
            else:
                blocked_reason = ErrorClass.INTERNAL_CONSISTENCY.value
            return CoordinationReport(run_id=run_id, blocked_reason=blocked_reason)

        requests = tuple(self._start_attempt(run_id, step) for step in candidates)
        with ThreadPoolExecutor(max_workers=self._concurrency) as executor:
            futures = tuple(executor.submit(_execute_request, request) for request in requests)
            outcomes = tuple(future.result() for future in futures)

        for outcome in outcomes:
            self._persist_outcome(outcome)

        return CoordinationReport(
            run_id=run_id,
            dispatched=tuple(step.id for step in candidates),
            completed=tuple(
                outcome.step_id for outcome in outcomes if outcome.status == "success"
            ),
        )

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

    def _start_attempt(self, run_id: str, step: StepDefinition) -> _ExecutionRequest:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            if run.state is RunState.PENDING:
                running_run, run_event = run.transition(
                    RunState.RUNNING,
                    occurred_at=self._clock.now(),
                )
                uow.runs.save(running_run, expected_revision=run.revision)
                uow.events.append(run_event)

            step_run = uow.steps.get(run_id, step.id)
            if step_run is None:
                raise LookupError(f"unknown step {run_id}:{step.id}")
            running_step, step_event = step_run.transition(
                StepState.RUNNING,
                occurred_at=self._clock.now(),
            )
            attempts = uow.attempts.list(run_id, step_id=step.id)
            attempt_no = max((attempt.attempt_no for attempt in attempts), default=0) + 1
            key = logical_idempotency_key(
                run_id=run_id,
                step_id=step.id,
                tool_name=step.tool.name,
                tool_version=step.tool.version,
                phase=ToolExecutionPhase.FORWARD,
            )
            started_at = self._clock.now()
            attempt = StepAttempt(
                run_id=run_id,
                step_id=step.id,
                attempt_no=attempt_no,
                phase=ToolExecutionPhase.FORWARD.value,
                attempt_id=self._identifiers.new(),
                status="running",
                idempotency_key=key,
                started_at=started_at,
            )
            uow.steps.save(running_step, expected_revision=step_run.revision)
            uow.attempts.add(attempt)
            uow.events.append(step_event)
            uow.commit()

        tool = self._tools.resolve(step.tool.name, step.tool.version)
        arguments = self._tools.coerce_arguments(
            step.tool.name,
            step.tool.version,
            step.arguments,
        )
        deadline = _parse_timestamp(started_at) + timedelta(
            seconds=tool.descriptor.default_timeout_seconds
        )
        return _ExecutionRequest(
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

    def _persist_outcome(self, outcome: ExecutionOutcome) -> None:
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
            completed_attempt = replace(
                attempt,
                status=outcome.status,
                completed_at=completed_at,
                next_attempt_at=next_attempt_at,
                result=redact(
                    outcome.result,
                    sensitive_paths=self._tools.descriptor_for(
                        step_definition.tool.name,
                        step_definition.tool.version,
                    ).sensitive_argument_paths,
                ),
                error_class=outcome.error_class,
                error_message=outcome.error_message,
            )
            uow.steps.save(changed_step, expected_revision=step_run.revision)
            uow.attempts.save(completed_attempt)
            uow.events.append(event)
            if next_state is StepState.SUCCEEDED and all(
                step.id == outcome.step_id
                or uow.steps.get(outcome.run_id, step.id).state is StepState.SUCCEEDED
                for step in definition.steps
            ):
                succeeded_run, run_event = run.transition(
                    RunState.SUCCEEDED,
                    occurred_at=completed_at,
                )
                uow.runs.save(succeeded_run, expected_revision=run.revision)
                uow.events.append(run_event)
            elif next_state is StepState.FAILED:
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
    except DomainError as exc:
        return ExecutionOutcome(
            run_id=context.run_id,
            step_id=context.step_id,
            attempt_no=context.attempt_number,
            status="failed",
            error_class=exc.error_class.value,
            error_message=exc.message,
        )
    return ExecutionOutcome(
        run_id=context.run_id,
        step_id=context.step_id,
        attempt_no=context.attempt_number,
        status="success",
        result=result.output,
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
