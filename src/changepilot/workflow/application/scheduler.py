from __future__ import annotations

from changepilot.workflow.domain.definitions import StepDefinition, WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun
from changepilot.workflow.domain.states import StepState


def ready_steps(
    definition: WorkflowDefinition,
    step_runs: tuple[StepRun, ...],
) -> tuple[StepDefinition, ...]:
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
