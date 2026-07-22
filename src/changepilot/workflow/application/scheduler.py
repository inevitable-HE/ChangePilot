from __future__ import annotations

from changepilot.workflow.domain.definitions import StepDefinition, WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun
from changepilot.workflow.domain.states import StepState


def ready_steps(
    definition: WorkflowDefinition,
    step_runs: tuple[StepRun, ...],
) -> tuple[StepDefinition, ...]:
    step_run_ids = tuple(step_run.step_id for step_run in step_runs)
    if len(step_run_ids) != len(set(step_run_ids)):
        raise ValueError("duplicate step run IDs")
    if set(step_run_ids) != {step.id for step in definition.steps}:
        raise ValueError("step run IDs must exactly match definition")
    if len({step_run.run_id for step_run in step_runs}) != 1:
        raise ValueError("step runs must belong to exactly one run")

    runs_by_step = {step_run.step_id: step_run for step_run in step_runs}
    depths = _topological_depths(definition)
    ready = (
        step
        for step in definition.steps
        if runs_by_step[step.id].state in {StepState.PENDING, StepState.READY}
        and all(
            runs_by_step[dependency].state is StepState.SUCCEEDED
            for dependency in step.depends_on
        )
    )
    return tuple(sorted(ready, key=lambda step: (depths[step.id], step.id)))


def compensation_order(
    definition: WorkflowDefinition,
    step_runs: tuple[StepRun, ...],
    attempts: tuple[object, ...],
) -> tuple[StepDefinition, ...]:
    """Return compensable forward effects in a deterministic reverse dependency order."""
    step_runs_by_id = {step_run.step_id: step_run for step_run in step_runs}
    if set(step_runs_by_id) != {step.id for step in definition.steps}:
        raise ValueError("step run IDs must exactly match definition")

    successful_effects = {
        attempt.step_id
        for attempt in attempts
        if getattr(attempt, "phase", None) == "forward"
        and getattr(attempt, "status", None) in {"success", "succeeded"}
        and getattr(attempt, "effect_applied", False)
    }
    remaining = {
        step.id: step
        for step in definition.steps
        if step.compensation_tool is not None
        and step_runs_by_id[step.id].state is StepState.SUCCEEDED
        and step.id in successful_effects
    }
    dependents = {
        step_id: {
            candidate.id
            for candidate in remaining.values()
            if step_id in candidate.depends_on
        }
        for step_id in remaining
    }

    ordered: list[StepDefinition] = []
    while remaining:
        leaves = [
            step
            for step_id, step in remaining.items()
            if not (dependents[step_id] & set(remaining))
        ]
        selected = min(
            leaves,
            key=lambda step: (
                -(step_runs_by_id[step.id].completion_sequence or 0),
                step.id,
            ),
        )
        ordered.append(selected)
        del remaining[selected.id]
    return tuple(ordered)


def _topological_depths(definition: WorkflowDefinition) -> dict[str, int]:
    steps_by_id = {step.id: step for step in definition.steps}
    depths: dict[str, int] = {}

    def depth(step_id: str) -> int:
        if step_id not in depths:
            dependencies = steps_by_id[step_id].depends_on
            depths[step_id] = (
                0 if not dependencies else 1 + max(depth(item) for item in dependencies)
            )
        return depths[step_id]

    for step in definition.steps:
        depth(step.id)
    return depths
