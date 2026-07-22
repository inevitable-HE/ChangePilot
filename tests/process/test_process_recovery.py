from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from changepilot.workflow.adapters.persistence.schema import metadata
from changepilot.workflow.adapters.persistence.sqlite import SQLiteUnitOfWork, create_sqlite_engine
from changepilot.workflow.adapters.tools.fake import ScriptedLedgerTool
from changepilot.workflow.application.coordinator import Coordinator
from changepilot.workflow.application.recovery import RecoveryService
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.tools import ToolExecutionResult
from tests.process.worker_scenario import _descriptor
from tests.support.fakes import DeterministicIdentifiers, FakeClock, FakeOutput


FIXTURE = Path(__file__).with_name("worker_scenario.py")


def _restart(database_path: Path, ledger_path: Path):
    engine = create_sqlite_engine(database_path)
    metadata.create_all(engine)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    clock = FakeClock()
    tool = ScriptedLedgerTool(
        descriptor=_descriptor(),
        ledger_path=ledger_path,
        execute_script=[
            ToolExecutionResult(output=FakeOutput(value="applied"), effect_applied=True)
        ],
    )
    registry = ToolRegistry()
    registry.register(tool)
    service = RecoveryService(uow_factory=uow_factory, tools=registry, clock=clock)
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
    )
    return uow_factory, tool, service, coordinator


def _crash(scenario: str, database_path: Path, ledger_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(FIXTURE), scenario, str(database_path), str(ledger_path)],
        cwd=Path(__file__).parents[2],
        check=False,
    )
    assert result.returncode == 91


@pytest.mark.parametrize(
    ("scenario", "expected_statuses"),
    [
        ("before-execute", ["not_applied", "success"]),
        ("after-effect-before-commit", ["succeeded"]),
    ],
)
def test_forward_crash_restarts_probe_before_any_repeat_effect(
    tmp_path: Path,
    scenario: str,
    expected_statuses: list[str],
) -> None:
    database_path = tmp_path / "workflow.db"
    ledger_path = tmp_path / "effects.jsonl"
    _crash(scenario, database_path, ledger_path)
    uow_factory, tool, service, coordinator = _restart(database_path, ledger_path)

    service.recover_nonterminal_runs()
    coordinator.run_once()
    coordinator.close(wait=True)

    with uow_factory() as uow:
        step = uow.steps.get("run-1", "inspect")
        attempts = uow.attempts.list("run-1", step_id="inspect")
    assert step.state is StepState.SUCCEEDED
    assert [attempt.status for attempt in attempts] == expected_statuses
    assert len(ledger_path.read_text(encoding="utf-8").splitlines()) == 1


def test_waiting_approval_restart_reuses_the_existing_request(tmp_path: Path) -> None:
    database_path = tmp_path / "workflow.db"
    ledger_path = tmp_path / "effects.jsonl"
    _crash("waiting-approval", database_path, ledger_path)
    engine = create_sqlite_engine(database_path)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    clock = FakeClock()
    tool = ScriptedLedgerTool(descriptor=_descriptor(high_risk=True), ledger_path=ledger_path)
    registry = ToolRegistry()
    registry.register(tool)
    service = RecoveryService(uow_factory=uow_factory, tools=registry, clock=clock)
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
    )

    service.recover_nonterminal_runs()
    coordinator.run_once()
    with uow_factory() as uow:
        assert uow.runs.get("run-1").state is RunState.WAITING_APPROVAL
        assert len(uow.approvals.list("run-1")) == 1
    coordinator.close(wait=True)


def test_compensation_crash_reconciles_the_persisted_effect(tmp_path: Path) -> None:
    database_path = tmp_path / "workflow.db"
    ledger_path = tmp_path / "effects.jsonl"
    _crash("compensation-after-effect-before-commit", database_path, ledger_path)
    uow_factory, tool, service, coordinator = _restart(database_path, ledger_path)

    service.recover_nonterminal_runs()
    with uow_factory() as uow:
        assert uow.runs.get("run-1").state is RunState.COMPENSATED
        assert uow.steps.get("run-1", "inspect").state is StepState.SUCCEEDED
    assert tool.effect_count("logical-key") == 1
    coordinator.close(wait=True)
