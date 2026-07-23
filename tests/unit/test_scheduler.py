from __future__ import annotations

from types import MappingProxyType

import pytest

from changepilot.workflow.application.scheduler import ready_steps
from changepilot.workflow.domain.definitions import (
    RetryPolicy,
    StepDefinition,
    ToolReference,
    WorkflowDefinition,
)
from changepilot.workflow.domain.runs import StepRun
from changepilot.workflow.domain.states import StepState


def _step(step_id: str, *depends_on: str) -> StepDefinition:
    return StepDefinition(
        id=step_id,
        tool=ToolReference(name="inspect", version="1.0.0"),
        arguments=MappingProxyType({}),
        depends_on=depends_on,
        retry=RetryPolicy(),
    )


def _definition(*steps: StepDefinition) -> WorkflowDefinition:
    return WorkflowDefinition(
        definition_id="workflow-1",
        version=1,
        steps=steps,
        digest="digest-1",
    )


def test_ready_steps_are_sorted_by_topological_depth_then_step_id() -> None:
    definition = _definition(
        _step("verify-z", "dependency-done"),
        _step("inspect-service"),
        _step("verify-a", "dependency-done"),
        _step("inspect-db"),
        _step("dependency-done"),
    )
    step_runs = tuple(
        StepRun(
            run_id="run-1",
            step_id=step.id,
            state=(
                StepState.SUCCEEDED
                if step.id == "dependency-done"
                else StepState.PENDING
            ),
        )
        for step in definition.steps
    )

    result = ready_steps(definition, step_runs)

    assert [step.id for step in result] == [
        "inspect-db",
        "inspect-service",
        "verify-a",
        "verify-z",
    ]


def test_step_is_not_ready_until_every_dependency_succeeds() -> None:
    definition = _definition(
        _step("inspect-db"),
        _step("inspect-service"),
        _step("precheck", "inspect-db", "inspect-service"),
    )
    step_runs = (
        StepRun("run-1", "inspect-db", state=StepState.SUCCEEDED),
        StepRun("run-1", "inspect-service", state=StepState.RUNNING),
        StepRun("run-1", "precheck", state=StepState.PENDING),
    )

    assert [step.id for step in ready_steps(definition, step_runs)] == []


def test_ready_steps_rejects_duplicate_step_run_ids() -> None:
    definition = _definition(_step("inspect-db"), _step("inspect-service"))
    step_runs = (
        StepRun("run-1", "inspect-db", state=StepState.PENDING),
        StepRun("run-1", "inspect-db", state=StepState.PENDING),
    )

    with pytest.raises(ValueError, match="duplicate step run IDs"):
        ready_steps(definition, step_runs)


def test_ready_steps_rejects_step_run_set_that_differs_from_definition() -> None:
    definition = _definition(_step("inspect-db"), _step("inspect-service"))
    step_runs = (
        StepRun("run-1", "inspect-db", state=StepState.PENDING),
        StepRun("run-1", "unknown", state=StepState.PENDING),
    )

    with pytest.raises(ValueError, match="step run IDs must exactly match definition"):
        ready_steps(definition, step_runs)


def test_ready_steps_rejects_step_runs_from_multiple_runs() -> None:
    definition = _definition(_step("inspect-db"), _step("inspect-service"))
    step_runs = (
        StepRun("run-1", "inspect-db", state=StepState.PENDING),
        StepRun("run-2", "inspect-service", state=StepState.PENDING),
    )

    with pytest.raises(ValueError, match="one run"):
        ready_steps(definition, step_runs)
