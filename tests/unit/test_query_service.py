from __future__ import annotations

from types import MappingProxyType

import pytest
from pydantic import ValidationError

from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from changepilot.workflow.application.services import (
    QueryService,
    WorkflowDriver,
    WorkflowService,
)
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.failures import InvalidTransition
from tests.support.fakes import DeterministicIdentifiers, FakeClock, ScriptedTool


def _services():
    store = MemoryStore()
    uow_factory = lambda: MemoryUnitOfWork(store)
    registry = ToolRegistry([ScriptedTool("inspect")])
    clock = FakeClock()
    workflow = WorkflowService(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
    )
    return workflow, QueryService(uow_factory=uow_factory, tools=registry), uow_factory


def _definition() -> dict[str, object]:
    return {
        "definition_id": "query-test",
        "version": 1,
        "steps": [
            {
                "id": "inspect",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "safe"},
            }
        ],
    }


def test_create_run_is_atomic_and_query_returns_deeply_immutable_dto() -> None:
    workflow, query, uow_factory = _services()

    run_id = workflow.create_run(_definition())
    result = query.get_run(run_id)

    assert result.run_id == "id-1"
    assert result.state == "pending"
    assert [(step.step_id, step.state) for step in result.steps] == [
        ("inspect", "pending")
    ]
    assert result.pending_approval is None
    assert result.original_error is None
    assert result.compensation_error is None
    with uow_factory() as uow:
        assert uow.definitions.get("query-test", 1) is not None
        assert uow.runs.get(run_id) is not None
        assert uow.steps.get(run_id, "inspect") is not None
        assert [item.sequence for item in uow.events.list(run_id)] == [1]
    with pytest.raises(ValidationError):
        result.state = "running"


def test_event_query_uses_exclusive_cursor_and_frozen_redacted_payload() -> None:
    workflow, query, _uow_factory = _services()
    run_id = workflow.create_run(_definition())
    workflow.cancel_run(run_id)

    all_events = query.list_events(run_id)
    page = query.list_events(run_id, after_sequence=1)

    assert [item.sequence for item in all_events] == [1, 2]
    assert [item.sequence for item in page] == [2]
    assert all_events[0].event_type == "workflow.created"
    assert isinstance(all_events[0].payload, MappingProxyType)
    with pytest.raises(TypeError):
        all_events[0].payload["step_count"] = 99
    with pytest.raises(ValueError, match="after_sequence"):
        query.list_events(run_id, after_sequence=-1)


def test_cancel_run_follows_state_machine_and_is_audited() -> None:
    workflow, query, _uow_factory = _services()
    run_id = workflow.create_run(_definition())

    workflow.cancel_run(run_id)

    assert query.get_run(run_id).state == "cancelled"
    assert query.list_events(run_id)[-1].new_state == "cancelled"
    with pytest.raises(InvalidTransition):
        workflow.cancel_run(run_id)


def test_queries_reject_unknown_run() -> None:
    _workflow, query, _uow_factory = _services()

    with pytest.raises(LookupError, match="unknown run"):
        query.get_run("missing")


def test_driver_requires_recovery_before_coordinator_dispatch() -> None:
    calls: list[str] = []

    class Recovery:
        def recover_nonterminal_runs(self):
            calls.append("recovery")
            return ("run-1",)

    class Coordinator:
        def run_once(self):
            calls.append("coordinator")
            return "report"

    driver = WorkflowDriver(recovery=Recovery(), coordinator=Coordinator())

    with pytest.raises(RuntimeError, match="start"):
        driver.run_once()
    assert driver.start() == ("run-1",)
    assert driver.run_once() == "report"
    assert calls == ["recovery", "coordinator"]
