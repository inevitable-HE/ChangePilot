from __future__ import annotations

from dataclasses import dataclass, replace

from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.failures import InvalidTransition
from changepilot.workflow.domain.states import RunState, StepState


RUN_TRANSITIONS: dict[RunState, frozenset[RunState]] = {
    RunState.PENDING: frozenset(
        {RunState.RUNNING, RunState.WAITING_APPROVAL, RunState.CANCELLED}
    ),
    RunState.RUNNING: frozenset(
        {
            RunState.CANCELLED,
            RunState.WAITING_APPROVAL,
            RunState.SUCCEEDED,
            RunState.FAILED,
            RunState.COMPENSATING,
            RunState.MANUAL_INTERVENTION,
        }
    ),
    RunState.WAITING_APPROVAL: frozenset(
        {
            RunState.RUNNING,
            RunState.CANCELLED,
            RunState.COMPENSATING,
            RunState.MANUAL_INTERVENTION,
        }
    ),
    RunState.COMPENSATING: frozenset(
        {
            RunState.COMPENSATED,
            RunState.MANUAL_INTERVENTION,
        }
    ),
}

STEP_TRANSITIONS: dict[StepState, frozenset[StepState]] = {
    StepState.PENDING: frozenset({StepState.READY}),
    StepState.READY: frozenset({StepState.RUNNING}),
    StepState.RUNNING: frozenset(
        {
            StepState.RETRY_WAIT,
            StepState.SUCCEEDED,
            StepState.FAILED,
            StepState.RESULT_UNKNOWN,
        }
    ),
    StepState.RETRY_WAIT: frozenset({StepState.READY}),
    StepState.RESULT_UNKNOWN: frozenset(
        {
            StepState.READY,
            StepState.SUCCEEDED,
            StepState.MANUAL_INTERVENTION,
        }
    ),
}


def _ensure_transition_allowed(
    aggregate_type: str,
    current_state: RunState | StepState,
    target_state: RunState | StepState,
    transitions: dict[RunState | StepState, frozenset[RunState | StepState]],
) -> None:
    if target_state not in transitions.get(current_state, frozenset()):
        raise InvalidTransition(
            aggregate_type=aggregate_type,
            current_state=current_state.value,
            target_state=target_state.value,
        )


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    run_id: str
    definition_id: str
    definition_version: int
    definition_digest: str
    state: RunState = RunState.PENDING
    revision: int = 0

    @classmethod
    def new(
        cls,
        run_id: str,
        definition_id: str,
        definition_version: int,
        definition_digest: str,
    ) -> "WorkflowRun":
        return cls(
            run_id=run_id,
            definition_id=definition_id,
            definition_version=definition_version,
            definition_digest=definition_digest,
        )

    def transition(
        self,
        target_state: RunState,
        *,
        occurred_at: str,
    ) -> tuple["WorkflowRun", AuditEvent]:
        _ensure_transition_allowed("run", self.state, target_state, RUN_TRANSITIONS)
        next_revision = self.revision + 1
        changed = replace(self, state=target_state, revision=next_revision)
        event = AuditEvent.run_state_changed(
            run_id=self.run_id,
            previous_state=self.state.value,
            new_state=target_state.value,
            occurred_at=occurred_at,
            previous_revision=self.revision,
            revision=next_revision,
        )
        return changed, event


@dataclass(frozen=True, slots=True)
class StepRun:
    run_id: str
    step_id: str
    state: StepState = StepState.PENDING
    revision: int = 0

    @classmethod
    def new(cls, run_id: str, step_id: str) -> "StepRun":
        return cls(run_id=run_id, step_id=step_id)

    def transition(
        self,
        target_state: StepState,
        *,
        occurred_at: str,
    ) -> tuple["StepRun", AuditEvent]:
        _ensure_transition_allowed("step", self.state, target_state, STEP_TRANSITIONS)
        next_revision = self.revision + 1
        changed = replace(self, state=target_state, revision=next_revision)
        event = AuditEvent.step_state_changed(
            run_id=self.run_id,
            step_id=self.step_id,
            previous_state=self.state.value,
            new_state=target_state.value,
            occurred_at=occurred_at,
            previous_revision=self.revision,
            revision=next_revision,
        )
        return changed, event
