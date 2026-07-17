from __future__ import annotations

from dataclasses import FrozenInstanceError, dataclass, fields

import pytest

from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from changepilot.workflow.adapters.persistence.schema import metadata
from changepilot.workflow.adapters.persistence.sqlite import (
    SQLiteUnitOfWork,
    create_sqlite_engine,
)
from changepilot.workflow.application import coordinator as coordinator_module
from changepilot.workflow.application.coordinator import Coordinator
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from tests.support.fakes import (
    DeterministicIdentifiers,
    FakeClock,
    ScriptedTool,
    TrackingUnitOfWorkFactory,
    ToolScripts,
)


class Query:
    def __init__(self, uow_factory) -> None:
        self._uow_factory = uow_factory

    def attempts(self, run_id: str, step_id: str):
        with self._uow_factory() as uow:
            return uow.attempts.list(run_id, step_id=step_id)

    def step(self, run_id: str, step_id: str):
        with self._uow_factory() as uow:
            return uow.steps.get(run_id, step_id)

    def run(self, run_id: str):
        with self._uow_factory() as uow:
            return uow.runs.get(run_id)


@dataclass
class Runtime:
    coordinator: Coordinator
    clock: FakeClock
    tools: ToolScripts
    query: Query
    uow_factory: object


def _make_runtime(
    *,
    concurrency: int = 4,
    step_ids: tuple[str, ...] = ("inspect",),
    track_transactions: bool = False,
) -> Runtime:
    store = MemoryStore()
    raw_uow_factory = lambda: MemoryUnitOfWork(store)
    clock = FakeClock()
    scripts = ToolScripts()
    registry = ToolRegistry()
    tool = scripts.add(ScriptedTool("inspect"))
    registry.register(tool)
    definition = WorkflowDefinition.from_mapping(
        {
            "definition_id": "workflow-1",
            "version": 1,
            "steps": [
                {
                    "id": step_id,
                    "tool": {"name": "inspect", "version": "1.0.0"},
                    "arguments": {"value": "checked"},
                    "retry": {
                        "max_attempts": 3,
                        "initial_backoff_seconds": 1.0,
                    },
                }
                for step_id in step_ids
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
    steps = tuple(
        StepRun(
            run_id="run-1",
            step_id=step_id,
            state=StepState.READY,
            revision=1,
        )
        for step_id in step_ids
    )
    with raw_uow_factory() as uow:
        uow.definitions.add(definition)
        uow.runs.add(run)
        for step in steps:
            uow.steps.add(step)
        uow.commit()

    uow_factory = (
        TrackingUnitOfWorkFactory(raw_uow_factory)
        if track_transactions
        else raw_uow_factory
    )

    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
        concurrency=concurrency,
    )
    return Runtime(
        coordinator=coordinator,
        clock=clock,
        tools=scripts,
        query=Query(uow_factory),
        uow_factory=uow_factory,
    )


@pytest.fixture
def runtime() -> Runtime:
    return _make_runtime()


@pytest.mark.parametrize("concurrency", [0, 17, True, 1.5])
def test_concurrency_must_be_within_one_and_sixteen(concurrency: object) -> None:
    with pytest.raises(ValueError, match="1..16"):
        _make_runtime(concurrency=concurrency)


def test_retryable_failure_creates_new_attempt_with_same_key(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "success"])

    runtime.coordinator.run_once()
    runtime.clock.advance(1.0)
    runtime.coordinator.run_once()

    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1, 2]
    assert [item.attempt_id for item in attempts] == ["id-1", "id-2"]
    assert attempts[0].idempotency_key == attempts[1].idempotency_key


def test_successful_only_step_completes_run(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["success"])

    report = runtime.coordinator.run_once()

    assert report.completed == ("inspect",)
    assert runtime.query.step("run-1", "inspect").state is StepState.SUCCEEDED
    assert runtime.query.run("run-1").state.value == "succeeded"


def test_retry_wait_is_blocked_until_backoff_deadline(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "success"])

    runtime.coordinator.run_once()
    runtime.clock.advance(0.999)
    blocked = runtime.coordinator.run_once()

    assert blocked.blocked_reason == "retry_backoff"
    assert [item.number for item in runtime.query.attempts("run-1", "inspect")] == [1]

    runtime.clock.advance(0.001)
    due = runtime.coordinator.run_once()

    assert due.dispatched == ("inspect",)
    assert [item.number for item in runtime.query.attempts("run-1", "inspect")] == [1, 2]


def test_permanent_failure_fails_run_without_retry(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["permanent", "success"])

    runtime.coordinator.run_once()
    runtime.coordinator.run_once()

    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1]
    assert attempts[0].error_class == "permanent"
    assert runtime.query.step("run-1", "inspect").state is StepState.FAILED
    assert runtime.query.run("run-1").state.value == "failed"


def test_result_unknown_waits_for_recovery_without_retry(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["result_unknown", "success"])

    runtime.coordinator.run_once()
    blocked = runtime.coordinator.run_once()

    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1]
    assert attempts[0].error_class == "result_unknown"
    assert runtime.query.step("run-1", "inspect").state is StepState.RESULT_UNKNOWN
    assert runtime.query.run("run-1").state is not None
    assert runtime.query.run("run-1").state.value == "running"
    assert blocked.blocked_reason == "result_unknown"


def test_nonterminal_run_without_ready_or_in_flight_work_is_diagnostic(
    runtime: Runtime,
) -> None:
    with runtime.uow_factory() as uow:
        run = uow.runs.get("run-1")
        step = uow.steps.get("run-1", "inspect")
        running_run, run_event = run.transition(
            RunState.RUNNING,
            occurred_at=runtime.clock.now(),
        )
        running_step, step_event = step.transition(
            StepState.RUNNING,
            occurred_at=runtime.clock.now(),
        )
        uow.runs.save(running_run, expected_revision=run.revision)
        uow.steps.save(running_step, expected_revision=step.revision)
        uow.events.append(run_event)
        uow.events.append(step_event)
        uow.commit()
    with runtime.uow_factory() as uow:
        step = uow.steps.get("run-1", "inspect")
        failed_step, step_event = step.transition(
            StepState.FAILED,
            occurred_at=runtime.clock.now(),
        )
        uow.steps.save(failed_step, expected_revision=step.revision)
        uow.events.append(step_event)
        uow.commit()

    report = runtime.coordinator.run_once()

    assert report.blocked_reason == "internal_consistency"


def test_dispatch_respects_configured_worker_capacity() -> None:
    runtime = _make_runtime(
        concurrency=2,
        step_ids=("step-d", "step-b", "step-c", "step-a"),
    )

    report = runtime.coordinator.run_once()

    assert report.dispatched == ("step-a", "step-b")
    assert sorted(call.step_id for call in runtime.tools.get("inspect").calls) == [
        "step-a",
        "step-b",
    ]


def test_retry_exhaustion_applies_run_failure_policy(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "retryable", "retryable"])

    runtime.coordinator.run_once()
    runtime.clock.advance(1.0)
    runtime.coordinator.run_once()
    runtime.clock.advance(1.0)
    runtime.coordinator.run_once()

    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1, 2, 3]
    assert [item.error_class for item in attempts] == [
        "retryable",
        "retryable",
        "retryable",
    ]
    assert runtime.query.step("run-1", "inspect").state is StepState.FAILED
    assert runtime.query.run("run-1").state is RunState.FAILED


def test_tool_runs_after_start_commit_and_worker_has_no_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _make_runtime(track_transactions=True)
    tracker = runtime.uow_factory
    observed: list[tuple[int, int, str]] = []
    original_execute = coordinator_module._execute_request

    def guarded_execute(request):
        assert {field.name for field in fields(request)} == {
            "tool",
            "context",
            "arguments",
        }
        with pytest.raises(FrozenInstanceError):
            request.arguments = None
        attempt = runtime.query.attempts("run-1", "inspect")[0]
        observed.append((tracker.active, tracker.commits, attempt.status))
        return original_execute(request)

    monkeypatch.setattr(coordinator_module, "_execute_request", guarded_execute)

    runtime.coordinator.run_once()

    assert observed == [(0, 1, "running")]
    assert tracker.commits == 2


def test_terminal_run_never_dispatches_remaining_ready_steps() -> None:
    runtime = _make_runtime(
        concurrency=1,
        step_ids=("step-a", "step-b"),
    )
    runtime.tools.script("inspect", ["permanent", "success"])

    first = runtime.coordinator.run_once()
    second = runtime.coordinator.run_once()

    assert first.dispatched == ("step-a",)
    assert second.dispatched == ()
    assert second.blocked_reason == "run_terminal"
    assert [call.step_id for call in runtime.tools.get("inspect").calls] == ["step-a"]


def test_successful_attempt_result_round_trips_through_sqlite(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "coordinator.db")
    metadata.create_all(engine)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    registry = ToolRegistry()
    tool = ScriptedTool("inspect")
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
    run = WorkflowRun.new(
        run_id="run-1",
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_digest=definition.digest,
    )
    step = StepRun(
        run_id="run-1",
        step_id="inspect",
        state=StepState.READY,
        revision=1,
    )
    with uow_factory() as uow:
        uow.definitions.add(definition)
        uow.runs.add(run)
        uow.steps.add(step)
        uow.commit()
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=FakeClock(),
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
    )

    coordinator.run_once()

    with uow_factory() as uow:
        attempt = uow.attempts.list("run-1", step_id="inspect")[0]
    assert attempt.result == {"value": "checked"}
