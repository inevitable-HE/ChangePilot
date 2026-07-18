from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, dataclass, fields
from threading import Event, Lock, Thread

import pytest

from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from changepilot.workflow.adapters.persistence.schema import metadata
from changepilot.workflow.adapters.persistence.sqlite import (
    SQLiteUnitOfWork,
    create_sqlite_engine,
)
from changepilot.workflow.application import coordinator as coordinator_module
from changepilot.workflow.application.coordinator import (
    Coordinator,
    ExecutionOutcome,
    OutcomePersistenceError,
)
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.tools import ToolExecutionResult
from tests.support.fakes import (
    BlockingTool,
    ConcurrencyTrackingTool,
    DeterministicIdentifiers,
    FailOnCommitUnitOfWorkFactory,
    FakeClock,
    FakeOutput,
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

    def events(self, run_id: str):
        with self._uow_factory() as uow:
            return tuple(entry.event for entry in uow.events.list(run_id))


@dataclass
class Runtime:
    coordinator: Coordinator
    clock: FakeClock
    tools: ToolScripts
    query: Query
    uow_factory: object
    registry: ToolRegistry


def _make_runtime(
    *,
    concurrency: int = 4,
    step_ids: tuple[str, ...] = ("inspect",),
    track_transactions: bool = False,
    tool: ScriptedTool | None = None,
    run_id_selector=None,
    uow_factory_wrapper=None,
    max_attempts: int = 3,
) -> Runtime:
    store = MemoryStore()
    raw_uow_factory = lambda: MemoryUnitOfWork(store)
    clock = FakeClock()
    scripts = ToolScripts()
    registry = ToolRegistry()
    selected_tool = scripts.add(tool or ScriptedTool("inspect"))
    registry.register(selected_tool)
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
                        "max_attempts": max_attempts,
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

    if uow_factory_wrapper is not None:
        uow_factory = uow_factory_wrapper(raw_uow_factory)
    elif track_transactions:
        uow_factory = TrackingUnitOfWorkFactory(raw_uow_factory)
    else:
        uow_factory = raw_uow_factory

    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=run_id_selector or (lambda: "run-1"),
        concurrency=concurrency,
    )
    return Runtime(
        coordinator=coordinator,
        clock=clock,
        tools=scripts,
        query=Query(uow_factory),
        uow_factory=uow_factory,
        registry=registry,
    )


@pytest.fixture
def runtime() -> Runtime:
    active_runtime = _make_runtime()
    yield active_runtime
    active_runtime.coordinator.close(wait=True)


def _dispatch_and_collect(runtime: Runtime, *, expected_call_count: int):
    dispatched = runtime.coordinator.run_once()
    assert runtime.tools.get("inspect").wait_for_calls(expected_call_count)
    collected = runtime.coordinator.run_once()
    return dispatched, collected


@pytest.mark.parametrize("concurrency", [0, 17, True, 1.5])
def test_concurrency_must_be_within_one_and_sixteen(concurrency: object) -> None:
    with pytest.raises(ValueError, match="1..16"):
        _make_runtime(concurrency=concurrency)


def test_retryable_failure_creates_new_attempt_with_same_key(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "success"])

    _dispatch_and_collect(runtime, expected_call_count=1)
    runtime.clock.advance(1.0)
    _dispatch_and_collect(runtime, expected_call_count=2)

    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1, 2]
    assert [item.attempt_id for item in attempts] == ["id-1", "id-2"]
    assert attempts[0].idempotency_key == attempts[1].idempotency_key


def test_successful_only_step_completes_run(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["success"])

    dispatched, collected = _dispatch_and_collect(runtime, expected_call_count=1)

    assert dispatched.completed + collected.completed == ("inspect",)
    assert runtime.query.step("run-1", "inspect").state is StepState.SUCCEEDED
    assert runtime.query.run("run-1").state.value == "succeeded"


def test_retry_wait_is_blocked_until_backoff_deadline(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "success"])

    _dispatch_and_collect(runtime, expected_call_count=1)
    runtime.clock.advance(0.999)
    blocked = runtime.coordinator.run_once()

    assert blocked.blocked_reason == "retry_backoff"
    assert [item.number for item in runtime.query.attempts("run-1", "inspect")] == [1]

    runtime.clock.advance(0.001)
    due = runtime.coordinator.run_once()
    assert runtime.tools.get("inspect").wait_for_calls(2)
    runtime.coordinator.run_once()

    assert due.dispatched == ("inspect",)
    assert [item.number for item in runtime.query.attempts("run-1", "inspect")] == [1, 2]


def test_permanent_failure_fails_run_without_retry(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["permanent", "success"])

    _dispatch_and_collect(runtime, expected_call_count=1)
    runtime.coordinator.run_once()

    attempts = runtime.query.attempts("run-1", "inspect")
    assert [item.number for item in attempts] == [1]
    assert attempts[0].error_class == "permanent"
    assert runtime.query.step("run-1", "inspect").state is StepState.FAILED
    assert runtime.query.run("run-1").state.value == "failed"


def test_result_unknown_waits_for_recovery_without_retry(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["result_unknown", "success"])

    _, blocked = _dispatch_and_collect(runtime, expected_call_count=1)

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
    assert all(
        len(runtime.query.attempts("run-1", step_id)) == 1
        for step_id in ("step-a", "step-b")
    )
    assert all(
        runtime.query.attempts("run-1", step_id) == ()
        for step_id in ("step-c", "step-d")
    )
    runtime.coordinator.close(wait=True)


def test_retry_exhaustion_applies_run_failure_policy(runtime: Runtime) -> None:
    runtime.tools.script("inspect", ["retryable", "retryable", "retryable"])

    _dispatch_and_collect(runtime, expected_call_count=1)
    runtime.clock.advance(1.0)
    _dispatch_and_collect(runtime, expected_call_count=2)
    runtime.clock.advance(1.0)
    _dispatch_and_collect(runtime, expected_call_count=3)

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
    executed = Event()
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
        try:
            return original_execute(request)
        finally:
            executed.set()

    monkeypatch.setattr(coordinator_module, "_execute_request", guarded_execute)

    runtime.coordinator.run_once()
    assert executed.wait(timeout=1)
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
    assert runtime.tools.get("inspect").wait_for_calls(1)
    runtime.coordinator.run_once()
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
    assert tool.wait_for_calls(1)
    coordinator.run_once()

    with uow_factory() as uow:
        attempt = uow.attempts.list("run-1", step_id="inspect")[0]
    assert attempt.result == {"value": "checked"}
    coordinator.close(wait=True)


def _start_coordinator_call(runtime: Runtime):
    reports = []
    errors = []

    def invoke() -> None:
        try:
            reports.append(runtime.coordinator.run_once())
        except BaseException as exc:
            errors.append(exc)

    thread = Thread(target=invoke)
    thread.start()
    return thread, reports, errors


def test_blocked_worker_does_not_block_run_once_or_nonblocking_close() -> None:
    tool = BlockingTool("inspect")
    runtime = _make_runtime(tool=tool)
    thread, reports, errors = _start_coordinator_call(runtime)

    assert tool.started.wait(timeout=1)
    thread.join(timeout=0.2)
    try:
        assert not thread.is_alive()
        assert errors == []
        assert reports[0].dispatched == ("inspect",)

        closed = Event()
        close_thread = Thread(
            target=lambda: (runtime.coordinator.close(wait=False), closed.set())
        )
        close_thread.start()
        assert closed.wait(timeout=0.2)
        close_thread.join(timeout=1)
    finally:
        tool.release.set()
        thread.join(timeout=1)


def test_deadline_marks_blocked_call_unknown_and_ignores_late_success() -> None:
    tool = BlockingTool("inspect")
    runtime = _make_runtime(tool=tool)
    thread, _, errors = _start_coordinator_call(runtime)

    assert tool.started.wait(timeout=1)
    thread.join(timeout=0.2)
    try:
        assert not thread.is_alive()
        assert errors == []

        runtime.clock.advance(30)
        report = runtime.coordinator.run_once()
        attempt = runtime.query.attempts("run-1", "inspect")[0]
        assert report.blocked_reason == "result_unknown"
        assert attempt.error_class == ErrorClass.RESULT_UNKNOWN.value
        assert attempt.status == "failed"
        assert runtime.query.step("run-1", "inspect").state is StepState.RESULT_UNKNOWN

        tool.release.set()
        assert tool.finished.wait(timeout=1)
        runtime.coordinator.run_once()
        late_attempt = runtime.query.attempts("run-1", "inspect")[0]
        assert late_attempt.error_class == ErrorClass.RESULT_UNKNOWN.value
        assert late_attempt.result is None
    finally:
        tool.release.set()
        thread.join(timeout=1)
        runtime.coordinator.close(wait=True)


def test_completed_sibling_is_persisted_while_other_worker_is_blocked() -> None:
    tool = BlockingTool("inspect", blocked_step_id="step-a")
    runtime = _make_runtime(
        concurrency=2,
        step_ids=("step-a", "step-b"),
        tool=tool,
    )
    thread, _, errors = _start_coordinator_call(runtime)

    assert tool.started.wait(timeout=1)
    assert tool.fast_completed.wait(timeout=1)
    thread.join(timeout=0.2)
    try:
        assert not thread.is_alive()
        assert errors == []

        runtime.coordinator.run_once()

        assert runtime.query.step("run-1", "step-a").state is StepState.RUNNING
        assert runtime.query.step("run-1", "step-b").state is StepState.SUCCEEDED
        assert runtime.query.attempts("run-1", "step-b")[0].status == "success"
    finally:
        tool.release.set()
        thread.join(timeout=1)
        runtime.coordinator.close(wait=True)


def test_completed_future_is_collected_when_selector_temporarily_returns_none() -> None:
    selected_run = ["run-1"]
    tool = BlockingTool("inspect")
    runtime = _make_runtime(
        tool=tool,
        run_id_selector=lambda: selected_run[0],
    )

    first = runtime.coordinator.run_once()
    assert tool.started.wait(timeout=1)
    assert first.dispatched == ("inspect",)

    tool.release.set()
    assert tool.finished.wait(timeout=1)
    selected_run[0] = None
    second = runtime.coordinator.run_once()

    assert second.blocked_reason == "no_run_selected"
    assert runtime.query.attempts("run-1", "inspect")[0].status == "success"
    assert runtime.query.step("run-1", "inspect").state is StepState.SUCCEEDED
    runtime.coordinator.close(wait=True)


def test_ready_run_reports_capacity_when_another_run_occupies_executor() -> None:
    selected_run = ["run-1"]
    tool = BlockingTool("inspect")
    runtime = _make_runtime(
        concurrency=1,
        tool=tool,
        run_id_selector=lambda: selected_run[0],
    )
    with runtime.uow_factory() as uow:
        definition = uow.definitions.get("workflow-1", 1)
        run = WorkflowRun.new(
            run_id="run-2",
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_digest=definition.digest,
        )
        uow.runs.add(run)
        uow.steps.add(
            StepRun(
                run_id="run-2",
                step_id="inspect",
                state=StepState.READY,
                revision=1,
            )
        )
        uow.commit()

    first = runtime.coordinator.run_once()
    assert tool.started.wait(timeout=1)
    assert first.dispatched == ("inspect",)

    try:
        selected_run[0] = "run-2"
        second = runtime.coordinator.run_once()

        assert second.blocked_reason == "capacity"
        assert runtime.query.step("run-2", "inspect").state is StepState.READY
        assert runtime.query.attempts("run-2", "inspect") == ()
    finally:
        tool.release.set()
        assert tool.finished.wait(timeout=1)
        runtime.coordinator.close(wait=True)


def test_close_retries_done_outcome_after_second_transaction_failure() -> None:
    tool = BlockingTool("inspect")
    runtime = _make_runtime(
        tool=tool,
        uow_factory_wrapper=lambda factory: FailOnCommitUnitOfWorkFactory(
            factory,
            fail_on=2,
        ),
    )

    first = runtime.coordinator.run_once()
    assert tool.started.wait(timeout=1)
    assert first.dispatched == ("inspect",)
    tool.release.set()
    assert tool.finished.wait(timeout=1)

    with pytest.raises(OutcomePersistenceError, match="outcome persistence failed"):
        runtime.coordinator.close(wait=False)
    assert runtime.query.attempts("run-1", "inspect")[0].status == "running"

    runtime.coordinator.close(wait=False)
    assert runtime.query.attempts("run-1", "inspect")[0].status == "success"
    assert runtime.query.step("run-1", "inspect").state is StepState.SUCCEEDED


def test_worker_peak_is_measured_and_bounded_by_configured_concurrency() -> None:
    tool = ConcurrencyTrackingTool("inspect", expected_peak=2)
    runtime = _make_runtime(
        concurrency=2,
        step_ids=("step-a", "step-b", "step-c", "step-d"),
        tool=tool,
    )
    thread, reports, errors = _start_coordinator_call(runtime)

    assert tool.capacity_reached.wait(timeout=1)
    thread.join(timeout=0.2)
    try:
        assert not thread.is_alive()
        assert errors == []
        assert reports[0].dispatched == ("step-a", "step-b")
        assert tool.peak_active == 2
    finally:
        tool.release.set()
        thread.join(timeout=1)
        runtime.coordinator.close(wait=True)


def test_batch_preparation_failure_starts_no_attempts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _make_runtime(step_ids=("step-a", "step-b"))
    original = runtime.registry.coerce_arguments
    call_count = 0

    def fail_second_preparation(name, version, arguments):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            raise ValueError("invalid second request")
        return original(name, version, arguments)

    monkeypatch.setattr(runtime.registry, "coerce_arguments", fail_second_preparation)

    report = runtime.coordinator.run_once()

    assert report.blocked_reason == ErrorClass.INTERNAL_CONSISTENCY.value
    assert runtime.query.attempts("run-1", "step-a") == ()
    assert runtime.query.attempts("run-1", "step-b") == ()
    assert runtime.query.step("run-1", "step-a").state is StepState.READY
    assert runtime.query.step("run-1", "step-b").state is StepState.READY


def test_submit_failure_is_persisted_as_retryable_without_orphan_running(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _make_runtime(concurrency=2, step_ids=("step-a", "step-b"))
    original_submit = ThreadPoolExecutor.submit
    submit_count = 0

    def fail_submit(self, *args, **kwargs):
        nonlocal submit_count
        submit_count += 1
        if submit_count == 2:
            raise RuntimeError("submit internals must not be persisted")
        return original_submit(self, *args, **kwargs)

    monkeypatch.setattr(ThreadPoolExecutor, "submit", fail_submit)

    report = runtime.coordinator.run_once()
    assert runtime.tools.get("inspect").wait_for_calls(1)
    runtime.coordinator.run_once()
    succeeded = runtime.query.attempts("run-1", "step-a")[0]
    rejected = runtime.query.attempts("run-1", "step-b")[0]

    assert report.dispatched == ("step-a", "step-b")
    assert succeeded.status == "success"
    assert runtime.query.step("run-1", "step-a").state is StepState.SUCCEEDED
    assert rejected.status == "failed"
    assert rejected.error_class == ErrorClass.RETRYABLE.value
    assert rejected.error_message == "tool submission failed"
    assert runtime.query.step("run-1", "step-b").state is StepState.RETRY_WAIT
    runtime.coordinator.close(wait=True)


def test_exhausted_submit_failure_stops_current_batch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _make_runtime(
        concurrency=2,
        step_ids=("step-a", "step-b"),
        max_attempts=1,
    )

    def fail_submit(self, *args, **kwargs):
        raise RuntimeError("submit exhausted")

    monkeypatch.setattr(ThreadPoolExecutor, "submit", fail_submit)

    try:
        report = runtime.coordinator.run_once()

        assert report.dispatched == ("step-a",)
        assert runtime.query.run("run-1").state is RunState.FAILED
        assert runtime.query.step("run-1", "step-a").state is StepState.FAILED
        assert runtime.query.step("run-1", "step-b").state is StepState.READY
        assert runtime.query.attempts("run-1", "step-b") == ()
    finally:
        runtime.coordinator.close(wait=True)


def test_future_exception_isolated_from_completed_sibling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = _make_runtime(concurrency=2, step_ids=("step-a", "step-b"))
    completed = Event()
    lock = Lock()
    count = 0

    def exceptional_worker(request):
        nonlocal count
        try:
            if request.context.step_id == "step-a":
                raise RuntimeError("secret future failure")
            return ExecutionOutcome(
                run_id=request.context.run_id,
                step_id=request.context.step_id,
                attempt_no=request.context.attempt_number,
                status="success",
                result=FakeOutput(value="checked"),
            )
        finally:
            with lock:
                count += 1
                if count == 2:
                    completed.set()

    monkeypatch.setattr(coordinator_module, "_execute_request", exceptional_worker)

    first = runtime.coordinator.run_once()
    assert completed.wait(timeout=1)
    second = runtime.coordinator.run_once()

    assert first.dispatched == ("step-a", "step-b")
    assert set(second.completed) == {"step-b"}
    failed = runtime.query.attempts("run-1", "step-a")[0]
    succeeded = runtime.query.attempts("run-1", "step-b")[0]
    assert failed.error_class == ErrorClass.PERMANENT.value
    assert failed.error_message == "tool execution failed"
    assert succeeded.status == "success"


@pytest.mark.parametrize(
    "run_state",
    [RunState.WAITING_APPROVAL, RunState.COMPENSATING],
)
def test_nonforward_run_state_fails_closed(run_state: RunState) -> None:
    runtime = _make_runtime()
    with runtime.uow_factory() as uow:
        run = uow.runs.get("run-1")
        running, running_event = run.transition(
            RunState.RUNNING,
            occurred_at=runtime.clock.now(),
        )
        uow.runs.save(running, expected_revision=run.revision)
        uow.events.append(running_event)
        uow.commit()
    with runtime.uow_factory() as uow:
        running = uow.runs.get("run-1")
        blocked, blocked_event = running.transition(
            run_state,
            occurred_at=runtime.clock.now(),
        )
        uow.runs.save(blocked, expected_revision=running.revision)
        uow.events.append(blocked_event)
        uow.commit()

    report = runtime.coordinator.run_once()

    assert report.blocked_reason == "run_not_forward"
    assert runtime.query.attempts("run-1", "inspect") == ()


def test_attempt_started_and_completed_events_are_atomic_and_structured() -> None:
    tool = BlockingTool("inspect")
    tool.release.set()
    runtime = _make_runtime(tool=tool, track_transactions=True)

    runtime.coordinator.run_once()
    assert tool.finished.wait(timeout=1)
    runtime.coordinator.run_once()

    attempt_events = [
        event
        for event in runtime.query.events("run-1")
        if event.event_type.startswith("tool_attempt_")
    ]
    assert [event.event_type for event in attempt_events] == [
        "tool_attempt_started",
        "tool_attempt_completed",
    ]
    assert [event.attempt_id for event in attempt_events] == ["id-1", "id-1"]
    assert [event.attempt_no for event in attempt_events] == [1, 1]
    assert [event.phase for event in attempt_events] == ["forward", "forward"]
    assert attempt_events[0].summary == {"status": "running"}
    assert attempt_events[1].error_class is None
    assert attempt_events[1].summary == {
        "status": "success",
        "effect_applied": True,
        "result": {"value": "checked"},
    }
    assert runtime.uow_factory.commits == 2


class _LeakyFailureTool(ScriptedTool):
    def __init__(self, name: str, *, secret_key: str, secret_value: str) -> None:
        super().__init__(name)
        self.secret_key = secret_key
        self.secret_value = secret_value
        self.finished = Event()

    def execute(self, context, arguments) -> ToolExecutionResult:
        try:
            raise DomainError(
                f"credential {self.secret_key}={self.secret_value}",
                error_class=ErrorClass.PERMANENT,
            )
        finally:
            self.finished.set()


def test_sqlite_never_persists_raw_error_secret_key_or_value(tmp_path) -> None:
    secret_key = "prod-database-password-ref"
    secret_value = "value-that-must-never-reach-sqlite"
    database_path = tmp_path / "coordinator-secrets.db"
    engine = create_sqlite_engine(database_path)
    metadata.create_all(engine)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    registry = ToolRegistry()
    tool = _LeakyFailureTool(
        "inspect",
        secret_key=secret_key,
        secret_value=secret_value,
    )
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
    with uow_factory() as uow:
        uow.definitions.add(definition)
        uow.runs.add(run)
        uow.steps.add(
            StepRun(
                run_id="run-1",
                step_id="inspect",
                state=StepState.READY,
                revision=1,
            )
        )
        uow.commit()
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=FakeClock(),
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "run-1",
    )

    coordinator.run_once()
    assert tool.finished.wait(timeout=1)
    coordinator.run_once()
    close = getattr(coordinator, "close", None)
    if close is not None:
        close(wait=True)
    engine.dispose()

    persisted = b"".join(path.read_bytes() for path in tmp_path.glob("coordinator-secrets.db*"))
    assert secret_key.encode() not in persisted
    assert secret_value.encode() not in persisted
