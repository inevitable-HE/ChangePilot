from __future__ import annotations

import importlib
import sys
from dataclasses import replace
from enum import StrEnum
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


OCCURRED_AT = "2026-07-16T00:00:00Z"
RUN_STATE_NAMES = (
    "pending",
    "running",
    "waiting_approval",
    "succeeded",
    "failed",
    "compensating",
    "compensated",
    "cancelled",
    "manual_intervention",
)
STEP_STATE_NAMES = (
    "pending",
    "ready",
    "running",
    "retry_wait",
    "succeeded",
    "failed",
    "result_unknown",
    "manual_intervention",
)
ERROR_CLASS_NAMES = (
    "retryable",
    "permanent",
    "result_unknown",
    "internal_consistency",
)
WORKFLOW_ALLOWED = {
    "pending": frozenset({"running", "waiting_approval", "cancelled"}),
    "running": frozenset(
        {
            "cancelled",
            "waiting_approval",
            "succeeded",
            "failed",
            "compensating",
            "manual_intervention",
        }
    ),
    "waiting_approval": frozenset({"running", "cancelled", "compensating"}),
    "compensating": frozenset({"compensated", "manual_intervention"}),
}
STEP_ALLOWED = {
    "pending": frozenset({"ready"}),
    "ready": frozenset({"running"}),
    "running": frozenset({"retry_wait", "succeeded", "failed", "result_unknown"}),
    "retry_wait": frozenset({"ready"}),
    "result_unknown": frozenset({"ready", "succeeded", "manual_intervention"}),
}


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing implementation module: {exc.name}")


def _load_state_machine_modules():
    states = _load_module("changepilot.workflow.domain.states")
    runs = _load_module("changepilot.workflow.domain.runs")
    events = _load_module("changepilot.workflow.domain.events")
    failures = _load_module("changepilot.workflow.domain.failures")
    return states, runs, events, failures


def _member_by_value(enum_cls: type[StrEnum], value: str) -> StrEnum:
    return next(member for member in enum_cls if member.value == value)


def _workflow_run_in_state(runs_module, states_module, state_name: str):
    run = runs_module.WorkflowRun.new(
        run_id="run-1",
        definition_id="definition-1",
        definition_version=3,
        definition_digest="digest-1",
    )
    return replace(
        run,
        state=_member_by_value(states_module.RunState, state_name),
        revision=4,
    )


def _step_run_in_state(runs_module, states_module, state_name: str):
    step = runs_module.StepRun.new(run_id="run-1", step_id="inspect")
    return replace(
        step,
        state=_member_by_value(states_module.StepState, state_name),
        revision=2,
    )


def test_run_states_are_exact_str_enum_members() -> None:
    states, _runs, _events, _failures = _load_state_machine_modules()

    assert issubclass(states.RunState, StrEnum)
    assert [member.value for member in states.RunState] == list(RUN_STATE_NAMES)


def test_step_states_are_exact_str_enum_members() -> None:
    states, _runs, _events, _failures = _load_state_machine_modules()

    assert issubclass(states.StepState, StrEnum)
    assert [member.value for member in states.StepState] == list(STEP_STATE_NAMES)


def test_error_classes_cover_retry_classification_contract() -> None:
    _states, _runs, _events, failures = _load_state_machine_modules()

    assert issubclass(failures.ErrorClass, StrEnum)
    assert [member.value for member in failures.ErrorClass] == list(ERROR_CLASS_NAMES)


def test_workflow_run_new_preserves_definition_identity() -> None:
    states, runs, _events, _failures = _load_state_machine_modules()

    run = runs.WorkflowRun.new(
        run_id="run-1",
        definition_id="definition-1",
        definition_version=7,
        definition_digest="digest-7",
    )

    assert run.run_id == "run-1"
    assert run.definition_id == "definition-1"
    assert run.definition_version == 7
    assert run.definition_digest == "digest-7"
    assert run.state is states.RunState.PENDING
    assert run.revision == 0


def test_step_run_new_starts_pending() -> None:
    states, runs, _events, _failures = _load_state_machine_modules()

    step = runs.StepRun.new(run_id="run-1", step_id="inspect")

    assert step.run_id == "run-1"
    assert step.step_id == "inspect"
    assert step.state is states.StepState.PENDING
    assert step.revision == 0


@pytest.mark.parametrize(
    ("source_name", "target_name"),
    [
        (source_name, target_name)
        for source_name, targets in WORKFLOW_ALLOWED.items()
        for target_name in sorted(targets)
    ],
)
def test_workflow_run_accepts_only_explicit_allowed_edges(
    source_name: str,
    target_name: str,
) -> None:
    states, runs, _events, _failures = _load_state_machine_modules()
    original = _workflow_run_in_state(runs, states, source_name)
    target = _member_by_value(states.RunState, target_name)

    changed, event = original.transition(target, occurred_at=OCCURRED_AT)

    assert changed is not original
    assert original.state.value == source_name
    assert changed.state is target
    assert original.revision == 4
    assert changed.revision == 5
    assert event.run_id == "run-1"
    assert event.step_id is None
    assert event.event_type == "run.state_changed"
    assert event.previous_state == source_name
    assert event.new_state == target_name
    assert event.previous_revision == 4
    assert event.revision == 5
    assert event.occurred_at == OCCURRED_AT


@pytest.mark.parametrize(
    ("source_name", "target_name"),
    [
        (source_name, target_name)
        for source_name in RUN_STATE_NAMES
        for target_name in RUN_STATE_NAMES
        if target_name not in WORKFLOW_ALLOWED.get(source_name, frozenset())
    ],
)
def test_workflow_run_rejects_every_edge_not_in_transition_table(
    source_name: str,
    target_name: str,
) -> None:
    states, runs, _events, failures = _load_state_machine_modules()
    original = _workflow_run_in_state(runs, states, source_name)
    target = _member_by_value(states.RunState, target_name)

    with pytest.raises(failures.InvalidTransition) as excinfo:
        original.transition(target, occurred_at=OCCURRED_AT)

    assert source_name in str(excinfo.value)
    assert target_name in str(excinfo.value)
    assert original.state.value == source_name
    assert original.revision == 4


@pytest.mark.parametrize(
    ("source_name", "target_name"),
    [
        (source_name, target_name)
        for source_name, targets in STEP_ALLOWED.items()
        for target_name in sorted(targets)
    ],
)
def test_step_run_accepts_only_explicit_allowed_edges(
    source_name: str,
    target_name: str,
) -> None:
    states, runs, _events, _failures = _load_state_machine_modules()
    original = _step_run_in_state(runs, states, source_name)
    target = _member_by_value(states.StepState, target_name)

    changed, event = original.transition(target, occurred_at=OCCURRED_AT)

    assert changed is not original
    assert original.state.value == source_name
    assert changed.state is target
    assert original.revision == 2
    assert changed.revision == 3
    assert event.run_id == "run-1"
    assert event.step_id == "inspect"
    assert event.event_type == "step.state_changed"
    assert event.previous_state == source_name
    assert event.new_state == target_name
    assert event.previous_revision == 2
    assert event.revision == 3
    assert event.occurred_at == OCCURRED_AT


@pytest.mark.parametrize(
    ("source_name", "target_name"),
    [
        (source_name, target_name)
        for source_name in STEP_STATE_NAMES
        for target_name in STEP_STATE_NAMES
        if target_name not in STEP_ALLOWED.get(source_name, frozenset())
    ],
)
def test_step_run_rejects_every_edge_not_in_transition_table(
    source_name: str,
    target_name: str,
) -> None:
    states, runs, _events, failures = _load_state_machine_modules()
    original = _step_run_in_state(runs, states, source_name)
    target = _member_by_value(states.StepState, target_name)

    with pytest.raises(failures.InvalidTransition) as excinfo:
        original.transition(target, occurred_at=OCCURRED_AT)

    assert source_name in str(excinfo.value)
    assert target_name in str(excinfo.value)
    assert original.state.value == source_name
    assert original.revision == 2
