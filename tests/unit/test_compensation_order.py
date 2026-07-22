from __future__ import annotations

from changepilot.workflow.application.coordinator import StepAttempt
from changepilot.workflow.application.scheduler import compensation_order
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun
from changepilot.workflow.domain.states import StepState
from changepilot.workflow.ports.tools import ToolExecutionPhase
from tests.support.fakes import ScriptedTool


def _definition() -> WorkflowDefinition:
    registry = ToolRegistry()
    for name in ("forward", "undo"):
        registry.register(ScriptedTool(name))
    return WorkflowDefinition.from_mapping(
        {
            "definition_id": "workflow-1",
            "version": 1,
            "steps": [
                {
                    "id": "a",
                    "tool": {"name": "forward", "version": "1.0.0"},
                    "arguments": {"value": "a"},
                    "compensation_tool": {"name": "undo", "version": "1.0.0"},
                },
                {
                    "id": "b",
                    "tool": {"name": "forward", "version": "1.0.0"},
                    "arguments": {"value": "b"},
                    "depends_on": ["a"],
                    "compensation_tool": {"name": "undo", "version": "1.0.0"},
                },
                {
                    "id": "c",
                    "tool": {"name": "forward", "version": "1.0.0"},
                    "arguments": {"value": "c"},
                    "compensation_tool": {"name": "undo", "version": "1.0.0"},
                },
            ],
        },
        registry,
    )


def _attempt(step_id: str, *, effect_applied: bool = True) -> StepAttempt:
    return StepAttempt(
        run_id="run-1",
        step_id=step_id,
        attempt_no=1,
        phase=ToolExecutionPhase.FORWARD.value,
        attempt_id=f"attempt-{step_id}",
        status="success",
        idempotency_key=f"forward-{step_id}",
        started_at="2026-07-17T00:00:00+00:00",
        effect_applied=effect_applied,
        completed_at="2026-07-17T00:00:01+00:00",
    )


def test_compensation_order_is_reverse_topological_then_reverse_completion_order() -> None:
    definition = _definition()
    steps = (
        StepRun("run-1", "a", StepState.SUCCEEDED, 2, completion_sequence=1),
        StepRun("run-1", "b", StepState.SUCCEEDED, 2, completion_sequence=3),
        StepRun("run-1", "c", StepState.SUCCEEDED, 2, completion_sequence=2),
    )

    ordered = compensation_order(
        definition,
        steps,
        tuple(_attempt(step_id) for step_id in ("a", "b", "c")),
    )

    assert [step.id for step in ordered] == ["b", "c", "a"]


def test_compensation_order_skips_unsuccessful_or_effect_free_steps() -> None:
    definition = _definition()
    steps = (
        StepRun("run-1", "a", StepState.SUCCEEDED, 2, completion_sequence=1),
        StepRun("run-1", "b", StepState.SUCCEEDED, 2, completion_sequence=2),
        StepRun("run-1", "c", StepState.FAILED, 2, completion_sequence=None),
    )

    ordered = compensation_order(
        definition,
        steps,
        (
            _attempt("a"),
            _attempt("b", effect_applied=False),
            _attempt("c"),
        ),
    )

    assert [step.id for step in ordered] == ["a"]
