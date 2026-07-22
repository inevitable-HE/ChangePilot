from __future__ import annotations

from dataclasses import dataclass

from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from changepilot.workflow.application.coordinator import Coordinator
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.tools import ToolExecutionPhase
from tests.support.fakes import DeterministicIdentifiers, FakeClock, ScriptedTool


@dataclass
class Runtime:
    coordinator: Coordinator
    uow_factory: object
    forward: ScriptedTool
    verify: ScriptedTool
    compensation: ScriptedTool


def _runtime(*, compensation_outcome: str = "success") -> Runtime:
    store = MemoryStore()
    uow_factory = lambda: MemoryUnitOfWork(store)
    clock = FakeClock()
    forward = ScriptedTool("prepare")
    verify = ScriptedTool("verify")
    compensation = ScriptedTool("prepare.undo")
    forward.script(["success"])
    verify.script(["permanent"])
    compensation.script([compensation_outcome])
    registry = ToolRegistry()
    for tool in (forward, verify, compensation):
        registry.register(tool)
    definition = WorkflowDefinition.from_mapping(
        {
            "definition_id": "workflow-1",
            "version": 1,
            "steps": [
                {
                    "id": "prepare",
                    "tool": {"name": "prepare", "version": "1.0.0"},
                    "arguments": {"value": "prepared"},
                    "compensation_tool": {
                        "name": "prepare.undo",
                        "version": "1.0.0",
                    },
                },
                {
                    "id": "verify",
                    "tool": {"name": "verify", "version": "1.0.0"},
                    "arguments": {"value": "verified"},
                    "depends_on": ["prepare"],
                },
            ],
        },
        registry,
    )
    run = WorkflowRun.new(
        run_id="run-1",
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_digest=definition.digest,
    )
    with uow_factory() as uow:
        uow.definitions.add(definition)
        uow.runs.add(run)
        uow.steps.add(StepRun("run-1", "prepare", StepState.READY, 1))
        uow.steps.add(StepRun("run-1", "verify"))
        uow.commit()
    return Runtime(
        coordinator=Coordinator(
            uow_factory=uow_factory,
            tools=registry,
            clock=clock,
            identifiers=DeterministicIdentifiers(),
            run_id_selector=lambda: "run-1",
            concurrency=1,
        ),
        uow_factory=uow_factory,
        forward=forward,
        verify=verify,
        compensation=compensation,
    )


def _run_to_compensation(runtime: Runtime) -> None:
    runtime.coordinator.run_once()
    assert runtime.forward.wait_for_calls(1)
    runtime.coordinator.run_once()
    assert runtime.verify.wait_for_calls(1)
    runtime.coordinator.run_once()


def _attempts(runtime: Runtime, step_id: str):
    with runtime.uow_factory() as uow:
        return uow.attempts.list("run-1", step_id=step_id)


def test_permanent_failure_compensates_with_an_isolated_phase_and_key() -> None:
    runtime = _runtime()
    try:
        _run_to_compensation(runtime)
        with runtime.uow_factory() as uow:
            assert uow.runs.get("run-1").state is RunState.COMPENSATING

        runtime.coordinator.run_once()
        assert runtime.compensation.wait_for_calls(1)
        runtime.coordinator.run_once()

        attempts = _attempts(runtime, "prepare")
        forward = next(item for item in attempts if item.phase == "forward")
        compensation = next(item for item in attempts if item.phase == "compensation")
        with runtime.uow_factory() as uow:
            run = uow.runs.get("run-1")
        assert run.state is RunState.COMPENSATED
        assert forward.idempotency_key != compensation.idempotency_key
        assert compensation.attempt_no == 1
        assert compensation.status == "success"
        assert runtime.compensation.calls[0].phase is ToolExecutionPhase.COMPENSATION
    finally:
        runtime.coordinator.close(wait=True)


def test_failed_compensation_preserves_both_errors_for_manual_intervention() -> None:
    runtime = _runtime(compensation_outcome="permanent")
    try:
        _run_to_compensation(runtime)
        runtime.coordinator.run_once()
        assert runtime.compensation.wait_for_calls(1)
        runtime.coordinator.run_once()

        with runtime.uow_factory() as uow:
            run = uow.runs.get("run-1")
        assert run.state is RunState.MANUAL_INTERVENTION
        assert run.original_error == {
            "error_class": "permanent",
            "error_message": "tool execution failed",
        }
        assert run.compensation_error == {
            "error_class": "permanent",
            "error_message": "tool execution failed",
        }
    finally:
        runtime.coordinator.close(wait=True)
