from __future__ import annotations

from dataclasses import dataclass, replace

import pytest

from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from changepilot.workflow.application.approvals import approval_recovery_required
from changepilot.workflow.application.coordinator import StepAttempt
from changepilot.workflow.application.recovery import RecoveryService
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.tools import (
    ToolExecutionPhase,
    ToolIdempotency,
    ToolProbeResult,
    ToolProbeStatus,
)
from tests.support.fakes import (
    DeterministicIdentifiers,
    FakeClock,
    FakeOutput,
    ScriptedTool,
)


class ProbeTool(ScriptedTool):
    def __init__(self, outcome: ToolProbeResult, *, probeable: bool = True) -> None:
        super().__init__("inspect")
        self.outcome = outcome
        self.probe_calls = []
        self.execute_calls = 0
        if not probeable:
            self.descriptor = self.descriptor.model_copy(
                update={"idempotency": ToolIdempotency.NONE}
            )

    def execute(self, context, arguments):
        self.execute_calls += 1
        return super().execute(context, arguments)

    def probe(self, query):
        self.probe_calls.append(query)
        return self.outcome


@dataclass
class Runtime:
    service: RecoveryService
    tool: ProbeTool
    uow_factory: object
    clock: FakeClock


def _make_runtime(
    *,
    probe_result: ToolProbeResult,
    step_state: StepState = StepState.RUNNING,
    run_state: RunState = RunState.RUNNING,
    attempt_status: str = "running",
    attempt_phase: ToolExecutionPhase = ToolExecutionPhase.FORWARD,
    probeable: bool = True,
) -> Runtime:
    store = MemoryStore()
    uow_factory = lambda: MemoryUnitOfWork(store)
    clock = FakeClock()
    tool = ProbeTool(probe_result, probeable=probeable)
    registry = ToolRegistry()
    registry.register(tool)
    definition = WorkflowDefinition.from_mapping(
        {
            "definition_id": "workflow-1",
            "version": 1,
            "steps": [
                {
                    "id": "inspect",
                    "tool": {"name": "inspect", "version": "1.0.0"},
                    "arguments": {"value": "checked"},
                }
            ],
        },
        registry,
    )
    run = WorkflowRun(
        run_id="run-1",
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_digest=definition.digest,
        state=run_state,
        revision=1,
    )
    step = StepRun(
        run_id="run-1",
        step_id="inspect",
        state=step_state,
        revision=1,
    )
    attempt = StepAttempt(
        run_id="run-1",
        step_id="inspect",
        attempt_no=1,
        phase=attempt_phase.value,
        attempt_id="attempt-1",
        status=attempt_status,
        idempotency_key="logical-key",
        started_at=clock.now(),
        error_class=("result_unknown" if step_state is StepState.RESULT_UNKNOWN else None),
    )
    with uow_factory() as uow:
        uow.definitions.add(definition)
        uow.runs.add(run)
        uow.steps.add(step)
        uow.attempts.add(attempt)
        uow.commit()
    return Runtime(
        service=RecoveryService(uow_factory=uow_factory, tools=registry, clock=clock),
        tool=tool,
        uow_factory=uow_factory,
        clock=clock,
    )


def _state(runtime: Runtime):
    with runtime.uow_factory() as uow:
        return (
            uow.runs.get("run-1"),
            uow.steps.get("run-1", "inspect"),
            uow.attempts.list("run-1", step_id="inspect"),
            tuple(entry.event for entry in uow.events.list("run-1")),
        )


def test_recovery_persists_found_effect_without_reexecuting() -> None:
    runtime = _make_runtime(
        probe_result=ToolProbeResult(
            status=ToolProbeStatus.FOUND,
            output=FakeOutput(value="already-applied"),
        )
    )

    assert runtime.service.recover_nonterminal_runs() == ("run-1",)

    run, step, attempts, events = _state(runtime)
    assert run.state is RunState.SUCCEEDED
    assert step.state is StepState.SUCCEEDED
    assert attempts[0].status == "succeeded"
    assert attempts[0].effect_applied is True
    assert attempts[0].result == {"value": "already-applied"}
    assert runtime.tool.execute_calls == 0
    assert [query.logical_idempotency_key for query in runtime.tool.probe_calls] == [
        "logical-key"
    ]
    assert events[-1].event_type == "recovery.applied"


def test_recovery_returns_not_applied_attempt_to_ready_and_preserves_history() -> None:
    runtime = _make_runtime(
        probe_result=ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)
    )

    runtime.service.recover_nonterminal_runs()

    run, step, attempts, events = _state(runtime)
    assert run.state is RunState.RUNNING
    assert step.state is StepState.READY
    assert attempts[0].status == "not_applied"
    assert attempts[0].completed_at == runtime.clock.now()
    assert runtime.tool.execute_calls == 0
    assert events[-1].event_type == "recovery.not_applied"


@pytest.mark.parametrize(
    ("probeable", "probe_result"),
    [
        (True, ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)),
        (False, ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)),
    ],
)
def test_recovery_requires_manual_intervention_when_outcome_cannot_be_proven(
    probeable: bool,
    probe_result: ToolProbeResult,
) -> None:
    runtime = _make_runtime(probe_result=probe_result, probeable=probeable)

    runtime.service.recover_nonterminal_runs()

    run, step, attempts, events = _state(runtime)
    assert run.state is RunState.MANUAL_INTERVENTION
    assert step.state is StepState.MANUAL_INTERVENTION
    assert attempts[0].status == "manual_intervention"
    assert len(runtime.tool.probe_calls) == int(probeable)
    assert events[-1].event_type == "recovery.manual_intervention"


def test_safe_recovery_clears_current_unknown_barrier_without_erasing_history() -> None:
    runtime = _make_runtime(
        probe_result=ToolProbeResult(status=ToolProbeStatus.NOT_FOUND),
        step_state=StepState.RESULT_UNKNOWN,
        attempt_status="failed",
    )

    runtime.service.recover_nonterminal_runs()

    run, step, attempts, _events = _state(runtime)
    assert run.state is RunState.RUNNING
    assert step.state is StepState.READY
    assert attempts[0].error_class == "result_unknown"
    assert attempts[0].status == "not_applied"
    with runtime.uow_factory() as uow:
        assert approval_recovery_required(uow, run) is False


def test_recovery_finishes_found_compensation_effect() -> None:
    runtime = _make_runtime(
        probe_result=ToolProbeResult(
            status=ToolProbeStatus.FOUND,
            output=FakeOutput(value="compensated"),
        ),
        run_state=RunState.COMPENSATING,
        attempt_phase=ToolExecutionPhase.COMPENSATION,
    )

    runtime.service.recover_nonterminal_runs()

    run, step, attempts, events = _state(runtime)
    assert run.state is RunState.COMPENSATED
    assert step.state is StepState.SUCCEEDED
    assert attempts[0].status == "succeeded"
    assert events[-1].event_type == "recovery.applied"
