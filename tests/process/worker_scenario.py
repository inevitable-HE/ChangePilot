from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2]))

from changepilot.workflow.adapters.persistence.schema import metadata
from changepilot.workflow.adapters.persistence.sqlite import SQLiteUnitOfWork, create_sqlite_engine
from changepilot.workflow.adapters.tools.fake import ScriptedLedgerTool
from changepilot.workflow.application.coordinator import Coordinator, StepAttempt
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.tools import (
    ToolExecutionContext,
    ToolExecutionPhase,
    ToolExecutionResult,
    ToolIdempotency,
    ToolRisk,
)
from tests.support.fakes import (
    DeterministicIdentifiers,
    FakeArguments,
    FakeClock,
    FakeOutput,
)


def _descriptor(*, high_risk: bool = False):
    from changepilot.workflow.ports.tools import ToolDescriptor

    return ToolDescriptor(
        name="inspect",
        version="1.0.0",
        input_model=FakeArguments,
        output_model=FakeOutput,
        risk=ToolRisk.HIGH if high_risk else ToolRisk.LOW,
        idempotency=ToolIdempotency.SUPPORTED,
        default_timeout_seconds=30,
        max_timeout_seconds=30,
    )


def _seed(
    database_path: Path,
    *,
    high_risk: bool = False,
    phase: ToolExecutionPhase = ToolExecutionPhase.FORWARD,
    run_state: RunState = RunState.PENDING,
) -> tuple[object, object, object, FakeClock]:
    engine = create_sqlite_engine(database_path)
    metadata.create_all(engine)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    clock = FakeClock()
    registry = ToolRegistry()
    tool = ScriptedLedgerTool(descriptor=_descriptor(high_risk=high_risk), ledger_path=Path("unused"))
    registry.register(tool)
    step_definition = {
        "id": "inspect",
        "tool": {"name": "inspect", "version": "1.0.0"},
        "arguments": {"value": "checked"},
        "risk": "high" if high_risk else "low",
    }
    if phase is ToolExecutionPhase.COMPENSATION:
        step_definition["compensation_tool"] = {
            "name": "inspect",
            "version": "1.0.0",
        }
    definition = WorkflowDefinition.from_mapping(
        {
            "definition_id": "workflow-1",
            "version": 1,
            "steps": [step_definition],
        },
        registry,
    )
    run = WorkflowRun(
        run_id="run-1",
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_digest=definition.digest,
        state=run_state,
        revision=int(run_state is not RunState.PENDING),
    )
    with uow_factory() as uow:
        uow.definitions.add(definition)
        uow.runs.add(run)
        uow.steps.add(
            StepRun(
                run_id="run-1",
                step_id="inspect",
                state=(
                    StepState.SUCCEEDED
                    if phase is ToolExecutionPhase.COMPENSATION
                    else StepState.READY
                ),
                revision=2 if phase is ToolExecutionPhase.COMPENSATION else 1,
                completion_sequence=(
                    1 if phase is ToolExecutionPhase.COMPENSATION else None
                ),
            )
        )
        uow.commit()
    return engine, uow_factory, registry, clock


class _CrashBeforeExecuteTool(ScriptedLedgerTool):
    def execute(self, context, arguments):
        os._exit(91)


class _CrashAfterEffectTool(ScriptedLedgerTool):
    def execute(self, context, arguments):
        super().execute(context, arguments)
        os._exit(91)


def _crash_during_forward(database_path: Path, ledger_path: Path, *, after_effect: bool) -> None:
    _engine, uow_factory, registry, clock = _seed(database_path)
    descriptor = _descriptor()
    tool_type = _CrashAfterEffectTool if after_effect else _CrashBeforeExecuteTool
    tool = tool_type(
        descriptor=descriptor,
        ledger_path=ledger_path,
        execute_script=[ToolExecutionResult(output=FakeOutput(value="applied"), effect_applied=True)],
    )
    registry = ToolRegistry()
    registry.register(tool)
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
    )
    coordinator.run_once()
    coordinator.close(wait=True)
    raise AssertionError("fixture process must have exited")


def _crash_while_waiting_for_approval(database_path: Path, ledger_path: Path) -> None:
    _engine, uow_factory, _registry, clock = _seed(database_path, high_risk=True)
    registry = ToolRegistry()
    registry.register(ScriptedLedgerTool(descriptor=_descriptor(high_risk=True), ledger_path=ledger_path))
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
    )
    assert coordinator.run_once().blocked_reason == "approval_pending"
    os._exit(91)


def _crash_after_compensation_effect(database_path: Path, ledger_path: Path) -> None:
    _engine, uow_factory, registry, clock = _seed(
        database_path,
        phase=ToolExecutionPhase.COMPENSATION,
        run_state=RunState.RUNNING,
    )
    with uow_factory() as uow:
        run = uow.runs.get("run-1")
        compensating, run_event = run.transition(RunState.COMPENSATING, occurred_at=clock.now())
        uow.runs.save(compensating, expected_revision=run.revision)
        uow.attempts.add(
            StepAttempt(
                run_id="run-1",
                step_id="inspect",
                attempt_no=1,
                phase=ToolExecutionPhase.FORWARD.value,
                attempt_id="forward-attempt-1",
                status="success",
                idempotency_key="forward-logical-key",
                started_at=clock.now(),
                effect_applied=True,
                completed_at=clock.now(),
            )
        )
        uow.attempts.add(
            StepAttempt(
                run_id="run-1",
                step_id="inspect",
                attempt_no=1,
                phase=ToolExecutionPhase.COMPENSATION.value,
                attempt_id="attempt-1",
                status="running",
                idempotency_key="logical-key",
                started_at=clock.now(),
            )
        )
        uow.events.append(run_event)
        uow.commit()
    tool = ScriptedLedgerTool(
        descriptor=_descriptor(),
        ledger_path=ledger_path,
        execute_script=[ToolExecutionResult(output=FakeOutput(value="compensated"), effect_applied=True)],
    )
    tool.execute(
        ToolExecutionContext(
            run_id="run-1",
            step_id="inspect",
            attempt_number=1,
            phase=ToolExecutionPhase.COMPENSATION,
            logical_idempotency_key="logical-key",
            deadline=clock._current,
            metadata={},
        ),
        FakeArguments(value="checked"),
    )
    os._exit(91)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenario")
    parser.add_argument("database_path", type=Path)
    parser.add_argument("ledger_path", type=Path)
    args = parser.parse_args()
    if args.scenario == "before-execute":
        _crash_during_forward(args.database_path, args.ledger_path, after_effect=False)
    elif args.scenario == "after-effect-before-commit":
        _crash_during_forward(args.database_path, args.ledger_path, after_effect=True)
    elif args.scenario == "waiting-approval":
        _crash_while_waiting_for_approval(args.database_path, args.ledger_path)
    elif args.scenario == "compensation-after-effect-before-commit":
        _crash_after_compensation_effect(args.database_path, args.ledger_path)
    else:
        raise ValueError(f"unknown scenario: {args.scenario}")


if __name__ == "__main__":
    main()
