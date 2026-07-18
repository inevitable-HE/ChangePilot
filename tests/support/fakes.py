from __future__ import annotations

from collections import deque
from datetime import datetime, timedelta
from threading import Condition, Event, Lock
from typing import Callable

from pydantic import BaseModel, ConfigDict

from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.ports.tools import (
    RecoveryQuery,
    ToolDescriptor,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolIdempotency,
    ToolProbeResult,
    ToolProbeStatus,
    ToolRisk,
)


class FakeClock:
    def __init__(self, start: str = "2026-07-17T00:00:00+00:00") -> None:
        self._current = datetime.fromisoformat(start)

    def now(self) -> str:
        return self._current.isoformat()

    def advance(self, seconds: float) -> None:
        self._current += timedelta(seconds=seconds)


class DeterministicIdentifiers:
    def __init__(self) -> None:
        self._next = 1

    def new(self) -> str:
        value = f"id-{self._next}"
        self._next += 1
        return value


class FakeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: str = "ok"


class FakeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: str


class ScriptedTool:
    def __init__(self, name: str, version: str = "1.0.0") -> None:
        self.descriptor = ToolDescriptor(
            name=name,
            version=version,
            input_model=FakeArguments,
            output_model=FakeOutput,
            risk=ToolRisk.LOW,
            idempotency=ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=30,
        )
        self._script: deque[str] = deque()
        self._lock = Lock()
        self._calls_changed = Condition(self._lock)
        self.calls: list[ToolExecutionContext] = []

    def script(self, outcomes: list[str]) -> None:
        with self._lock:
            self._script = deque(outcomes)

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: FakeArguments,
    ) -> ToolExecutionResult:
        with self._lock:
            self.calls.append(context)
            self._calls_changed.notify_all()
            outcome = self._script.popleft() if self._script else "success"
        if outcome == "success":
            return ToolExecutionResult(
                output=FakeOutput(value=arguments.value),
                effect_applied=True,
            )
        try:
            error_class = ErrorClass(outcome)
        except ValueError as exc:
            raise AssertionError(f"unknown scripted outcome: {outcome}") from exc
        raise DomainError(f"scripted {outcome}", error_class=error_class)

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)

    def wait_for_calls(self, count: int, *, timeout: float = 1.0) -> bool:
        with self._calls_changed:
            return self._calls_changed.wait_for(
                lambda: len(self.calls) >= count,
                timeout=timeout,
            )


class BlockingTool(ScriptedTool):
    def __init__(self, name: str, *, blocked_step_id: str | None = None) -> None:
        super().__init__(name)
        self.blocked_step_id = blocked_step_id
        self.started = Event()
        self.fast_completed = Event()
        self.finished = Event()
        self.release = Event()

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: FakeArguments,
    ) -> ToolExecutionResult:
        try:
            if self.blocked_step_id is None or context.step_id == self.blocked_step_id:
                self.started.set()
                if not self.release.wait(timeout=5):
                    raise AssertionError("blocking tool was not released")
            else:
                self.fast_completed.set()
            return super().execute(context, arguments)
        finally:
            self.finished.set()


class ConcurrencyTrackingTool(ScriptedTool):
    def __init__(self, name: str, *, expected_peak: int) -> None:
        super().__init__(name)
        self.expected_peak = expected_peak
        self.active = 0
        self.peak_active = 0
        self.capacity_reached = Event()
        self.release = Event()

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: FakeArguments,
    ) -> ToolExecutionResult:
        with self._lock:
            self.active += 1
            self.peak_active = max(self.peak_active, self.active)
            if self.active == self.expected_peak:
                self.capacity_reached.set()
        try:
            if not self.release.wait(timeout=5):
                raise AssertionError("concurrency tracking tool was not released")
            return super().execute(context, arguments)
        finally:
            with self._lock:
                self.active -= 1


class ToolScripts:
    def __init__(self) -> None:
        self._tools: dict[str, ScriptedTool] = {}

    def add(self, tool: ScriptedTool) -> ScriptedTool:
        self._tools[tool.descriptor.name] = tool
        return tool

    def script(self, name: str, outcomes: list[str]) -> None:
        self._tools[name].script(outcomes)

    def get(self, name: str) -> ScriptedTool:
        return self._tools[name]


class TrackingUnitOfWorkFactory:
    def __init__(self, factory: Callable[[], object]) -> None:
        self._factory = factory
        self._lock = Lock()
        self.active = 0
        self.commits = 0

    def __call__(self):
        return _TrackedUnitOfWork(self, self._factory())


class _TrackedUnitOfWork:
    def __init__(self, tracker: TrackingUnitOfWorkFactory, inner: object) -> None:
        self._tracker = tracker
        self._inner = inner

    def __enter__(self):
        self._inner.__enter__()
        with self._tracker._lock:
            self._tracker.active += 1
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self._inner.__exit__(exc_type, exc, tb)
        finally:
            with self._tracker._lock:
                self._tracker.active -= 1

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def commit(self) -> None:
        self._inner.commit()
        with self._tracker._lock:
            self._tracker.commits += 1


class FailOnCommitUnitOfWorkFactory:
    def __init__(self, factory: Callable[[], object], *, fail_on: int) -> None:
        self._factory = factory
        self._fail_on = fail_on
        self._lock = Lock()
        self._commit_count = 0

    def __call__(self):
        return _FailOnCommitUnitOfWork(self, self._factory())


class _FailOnCommitUnitOfWork:
    def __init__(self, factory: FailOnCommitUnitOfWorkFactory, inner: object) -> None:
        self._factory = factory
        self._inner = inner

    def __enter__(self):
        self._inner.__enter__()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self._inner.__exit__(exc_type, exc, tb)

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def commit(self) -> None:
        with self._factory._lock:
            self._factory._commit_count += 1
            commit_number = self._factory._commit_count
        if commit_number == self._factory._fail_on:
            raise RuntimeError("injected persistence failure")
        self._inner.commit()
