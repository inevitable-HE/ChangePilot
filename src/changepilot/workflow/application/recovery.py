from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from typing import Callable

from pydantic import BaseModel

from changepilot.workflow.application.coordinator import StepAttempt
from changepilot.workflow.application.scheduler import compensation_order
from changepilot.workflow.application.tooling import ToolRegistry, redact
from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.clock import Clock
from changepilot.workflow.ports.tools import (
    RecoveryQuery,
    ToolExecutionPhase,
    ToolIdempotency,
    ToolProbeStatus,
)


_TERMINAL_RUN_STATES = frozenset(
    {
        RunState.SUCCEEDED,
        RunState.FAILED,
        RunState.COMPENSATED,
        RunState.CANCELLED,
        RunState.MANUAL_INTERVENTION,
    }
)


class RecoveryService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], object],
        tools: ToolRegistry,
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._tools = tools
        self._clock = clock

    def recover_nonterminal_runs(self) -> tuple[str, ...]:
        with self._uow_factory() as uow:
            run_ids = tuple(
                run.run_id
                for run in uow.runs.list()
                if run.state not in _TERMINAL_RUN_STATES
            )

        recovered: list[str] = []
        for run_id in run_ids:
            if self._recover_run(run_id):
                recovered.append(run_id)
        return tuple(recovered)

    def _recover_run(self, run_id: str) -> bool:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None or run.state in _TERMINAL_RUN_STATES:
                return False
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                raise LookupError(
                    f"unknown definition {run.definition_id}@{run.definition_version}"
                )
            pending = tuple(
                (step, self._unresolved_attempt(uow, run_id, step.id))
                for step in definition.steps
            )

        changed = False
        for step, attempt in pending:
            if attempt is None:
                continue
            self._recover_attempt(run_id, step.id, attempt)
            changed = True
        return changed

    @staticmethod
    def _unresolved_attempt(uow: object, run_id: str, step_id: str) -> StepAttempt | None:
        step = uow.steps.get(run_id, step_id)
        if step is None:
            raise LookupError(f"unknown step {run_id}:{step_id}")
        attempts = uow.attempts.list(run_id, step_id=step_id)
        if not attempts:
            return None
        unresolved = [attempt for attempt in attempts if attempt.status == "running"]
        if unresolved:
            return max(
                unresolved,
                key=lambda item: (item.attempt_no, item.phase),
            )
        latest = max(attempts, key=lambda item: (item.attempt_no, item.phase))
        if step.state is StepState.RESULT_UNKNOWN:
            return latest
        return None

    def _recover_attempt(self, run_id: str, step_id: str, expected_attempt: StepAttempt) -> None:
        with self._uow_factory() as uow:
            run = uow.runs.get(run_id)
            if run is None:
                raise LookupError(f"unknown run {run_id}")
            definition = uow.definitions.get(run.definition_id, run.definition_version)
            if definition is None:
                raise LookupError(
                    f"unknown definition {run.definition_id}@{run.definition_version}"
                )
            step_definition = next(step for step in definition.steps if step.id == step_id)
            step_run = uow.steps.get(run_id, step_id)
            if step_run is None:
                raise LookupError(f"unknown step {run_id}:{step_id}")
            attempt = next(
                item
                for item in uow.attempts.list(run_id, step_id=step_id)
                if item.attempt_no == expected_attempt.attempt_no
                and item.phase == expected_attempt.phase
            )
            occurred_at = self._clock.now()
            probe_result = self._probe(step_definition, attempt)
            if probe_result is None:
                self._persist_manual_intervention(
                    uow, run, step_run, attempt, occurred_at=occurred_at
                )
            elif probe_result.status is ToolProbeStatus.FOUND:
                self._persist_applied(
                    uow,
                    run,
                    definition,
                    definition.steps,
                    step_run,
                    attempt,
                    output=probe_result.output,
                    occurred_at=occurred_at,
                )
            elif probe_result.status is ToolProbeStatus.NOT_FOUND:
                self._persist_not_applied(
                    uow, run, step_run, attempt, occurred_at=occurred_at
                )
            else:
                self._persist_manual_intervention(
                    uow, run, step_run, attempt, occurred_at=occurred_at
                )
            uow.commit()

    def _probe(self, step_definition: object, attempt: StepAttempt):
        phase = ToolExecutionPhase(attempt.phase)
        tool_reference = (
            step_definition.tool
            if phase is ToolExecutionPhase.FORWARD
            else step_definition.compensation_tool
        )
        if tool_reference is None:
            return None
        tool = self._tools.resolve(
            tool_reference.name,
            tool_reference.version,
        )
        if tool.descriptor.idempotency is ToolIdempotency.NONE:
            return None
        try:
            deadline = _parse_timestamp(self._clock.now()) + timedelta(
                seconds=tool.descriptor.default_timeout_seconds
            )
            result = tool.probe(
                RecoveryQuery(
                    run_id=attempt.run_id,
                    step_id=attempt.step_id,
                    phase=phase,
                    logical_idempotency_key=attempt.idempotency_key,
                    deadline=deadline,
                    metadata={},
                )
            )
            if result.status is ToolProbeStatus.FOUND:
                self._tools.validate_output(
                    tool_reference.name,
                    tool_reference.version,
                    result.output,
                )
            return result
        except Exception:
            return None

    def _persist_applied(
        self,
        uow: object,
        run: object,
        definition: object,
        all_steps: tuple[object, ...],
        step_run: object,
        attempt: StepAttempt,
        *,
        output: BaseModel | None,
        occurred_at: str,
    ) -> None:
        phase = ToolExecutionPhase(attempt.phase)
        tool_reference = (
            next(step.tool for step in all_steps if step.id == attempt.step_id)
            if phase is ToolExecutionPhase.FORWARD
            else next(step.compensation_tool for step in all_steps if step.id == attempt.step_id)
        )
        if tool_reference is None:
            raise RuntimeError(f"step {attempt.step_id} has no compensation tool")
        completed_attempt = replace(
            attempt,
            status="succeeded",
            effect_applied=True,
            completed_at=occurred_at,
            next_attempt_at=None,
            result=redact(
                output,
                sensitive_paths=self._tools.descriptor_for(
                    tool_reference.name,
                    tool_reference.version,
                ).sensitive_argument_paths,
            ),
            error_class=None,
            error_message=None,
        )
        uow.attempts.save(completed_attempt)
        if phase is ToolExecutionPhase.FORWARD:
            completed_step, step_event = step_run.transition(
                StepState.SUCCEEDED,
                occurred_at=occurred_at,
            )
            uow.steps.save(completed_step, expected_revision=step_run.revision)
            uow.events.append(step_event)
        else:
            completed_step = step_run
        changed_run = self._run_after_applied(
            uow,
            run,
            definition,
            all_steps,
            completed_step,
            occurred_at,
            phase=phase,
        )
        if changed_run is not None:
            uow.runs.save(changed_run[0], expected_revision=run.revision)
            uow.events.append(changed_run[1])
        uow.events.append(
            _recovery_event(
                event_type="recovery.applied",
                step=completed_step,
                attempt=attempt,
                occurred_at=occurred_at,
                decision="applied",
            )
        )

    @staticmethod
    def _run_after_applied(
        uow: object,
        run: object,
        definition: object,
        all_steps: tuple[object, ...],
        completed_step: object,
        occurred_at: str,
        *,
        phase: ToolExecutionPhase,
    ):
        if run.state is RunState.COMPENSATING and phase is ToolExecutionPhase.COMPENSATION:
            step_runs = tuple(uow.steps.get(run.run_id, step.id) for step in all_steps)
            completed_compensation_ids = {
                attempt.step_id
                for attempt in uow.attempts.list(run.run_id)
                if attempt.phase == ToolExecutionPhase.COMPENSATION.value
                and attempt.status in {"success", "succeeded"}
            }
            pending = [
                step
                for step in compensation_order(
                    definition,
                    tuple(item for item in step_runs if item is not None),
                    uow.attempts.list(run.run_id),
                )
                if step.id not in completed_compensation_ids
            ]
            if not pending:
                return run.transition(RunState.COMPENSATED, occurred_at=occurred_at)
        if run.state is RunState.COMPENSATING and phase is ToolExecutionPhase.FORWARD:
            return run.transition(RunState.COMPENSATED, occurred_at=occurred_at)
        if run.state is RunState.RUNNING and all(
            step.id == completed_step.step_id
            or uow.steps.get(run.run_id, step.id).state is StepState.SUCCEEDED
            for step in all_steps
        ):
            return run.transition(RunState.SUCCEEDED, occurred_at=occurred_at)
        return None

    @staticmethod
    def _persist_not_applied(
        uow: object,
        run: object,
        step_run: object,
        attempt: StepAttempt,
        *,
        occurred_at: str,
    ) -> None:
        phase = ToolExecutionPhase(attempt.phase)
        if phase is ToolExecutionPhase.FORWARD:
            ready_step, step_event = step_run.transition(
                StepState.READY,
                occurred_at=occurred_at,
            )
            uow.steps.save(ready_step, expected_revision=step_run.revision)
            uow.events.append(step_event)
        else:
            ready_step = step_run
        uow.attempts.save(
            replace(
                attempt,
                status="not_applied",
                completed_at=occurred_at,
                next_attempt_at=None,
            )
        )
        uow.events.append(
            _recovery_event(
                event_type="recovery.not_applied",
                step=ready_step,
                attempt=attempt,
                occurred_at=occurred_at,
                decision="not_applied",
            )
        )

    @staticmethod
    def _persist_manual_intervention(
        uow: object,
        run: object,
        step_run: object,
        attempt: StepAttempt,
        *,
        occurred_at: str,
    ) -> None:
        phase = ToolExecutionPhase(attempt.phase)
        if phase is ToolExecutionPhase.FORWARD:
            manual_step, step_event = step_run.transition(
                StepState.MANUAL_INTERVENTION,
                occurred_at=occurred_at,
            )
            uow.steps.save(manual_step, expected_revision=step_run.revision)
            uow.events.append(step_event)
        else:
            manual_step = step_run
        manual_run, run_event = run.transition(
            RunState.MANUAL_INTERVENTION,
            occurred_at=occurred_at,
        )
        if phase is ToolExecutionPhase.COMPENSATION:
            manual_run = replace(
                manual_run,
                compensation_error={
                    "error_class": "result_unknown",
                    "error_message": "compensation result cannot be proven",
                },
            )
        uow.runs.save(manual_run, expected_revision=run.revision)
        uow.attempts.save(
            replace(
                attempt,
                status="manual_intervention",
                completed_at=occurred_at,
                next_attempt_at=None,
            )
        )
        uow.events.append(run_event)
        uow.events.append(
            _recovery_event(
                event_type="recovery.manual_intervention",
                step=manual_step,
                attempt=attempt,
                occurred_at=occurred_at,
                decision="manual_intervention",
            )
        )


def _recovery_event(
    *,
    event_type: str,
    step: object,
    attempt: StepAttempt,
    occurred_at: str,
    decision: str,
) -> AuditEvent:
    return AuditEvent(
        run_id=attempt.run_id,
        step_id=attempt.step_id,
        event_type=event_type,
        occurred_at=occurred_at,
        previous_state=step.state.value,
        new_state=step.state.value,
        previous_revision=step.revision,
        revision=step.revision,
        attempt_id=attempt.attempt_id,
        attempt_no=attempt.attempt_no,
        phase=attempt.phase,
        summary={
            "decision": decision,
            "idempotency_key": attempt.idempotency_key,
        },
    )


def _parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("clock must provide timezone-aware timestamps")
    return parsed
