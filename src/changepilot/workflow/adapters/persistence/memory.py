from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any, Generic, TypeVar

from changepilot.workflow.ports.persistence import (
    ApprovalRecord,
    AttemptRecord,
    DefinitionRecord,
    EventRepository,
    HasRevision,
    OptimisticLockError,
    RunRepository,
    RunScopedRecord,
    SequencedEvent,
    StepRepository,
    StepScopedRecord,
    UniquenessError,
)


DefinitionT = TypeVar("DefinitionT", bound=DefinitionRecord)
RunT = TypeVar("RunT", bound=HasRevision)
StepT = TypeVar("StepT", bound=HasRevision | StepScopedRecord)
AttemptT = TypeVar("AttemptT", bound=AttemptRecord)
ApprovalT = TypeVar("ApprovalT", bound=ApprovalRecord)
EventT = TypeVar("EventT", bound=RunScopedRecord)


def _clone(value: Any):
    try:
        return deepcopy(value)
    except TypeError:
        return value


def _definition_key(definition: DefinitionRecord) -> tuple[str, int]:
    return definition.definition_id, definition.version


def _step_key(step: StepScopedRecord) -> tuple[str, str]:
    return step.run_id, step.step_id


def _attempt_key(attempt: AttemptRecord) -> tuple[str, str, int, str]:
    return attempt.run_id, attempt.step_id, attempt.attempt_no, attempt.phase


def _approval_key(approval: ApprovalRecord) -> tuple[str, str]:
    return approval.run_id, approval.approval_key


@dataclass(slots=True)
class MemoryStore:
    definitions: dict[tuple[str, int], object] = field(default_factory=dict)
    runs: dict[str, object] = field(default_factory=dict)
    steps: dict[tuple[str, str], object] = field(default_factory=dict)
    attempts: dict[tuple[str, str, int, str], object] = field(default_factory=dict)
    approvals: dict[tuple[str, str], object] = field(default_factory=dict)
    events: dict[str, list[SequencedEvent[object]]] = field(default_factory=dict)
    event_sequences: dict[str, int] = field(default_factory=dict)


@dataclass(slots=True)
class _Snapshot:
    definitions: dict[tuple[str, int], object]
    runs: dict[str, object]
    steps: dict[tuple[str, str], object]
    attempts: dict[tuple[str, str, int, str], object]
    approvals: dict[tuple[str, str], object]
    events: dict[str, list[SequencedEvent[object]]]
    event_sequences: dict[str, int]
    new_definition_keys: set[tuple[str, int]] = field(default_factory=set)
    new_run_keys: set[str] = field(default_factory=set)
    new_step_keys: set[tuple[str, str]] = field(default_factory=set)
    new_attempt_keys: set[tuple[str, str, int, str]] = field(default_factory=set)
    new_approval_keys: set[tuple[str, str]] = field(default_factory=set)
    run_expected_revisions: dict[str, int] = field(default_factory=dict)
    step_expected_revisions: dict[tuple[str, str], int] = field(default_factory=dict)

    @classmethod
    def from_store(cls, store: MemoryStore) -> "_Snapshot":
        return cls(
            definitions={key: _clone(value) for key, value in store.definitions.items()},
            runs={key: _clone(value) for key, value in store.runs.items()},
            steps={key: _clone(value) for key, value in store.steps.items()},
            attempts={key: _clone(value) for key, value in store.attempts.items()},
            approvals={key: _clone(value) for key, value in store.approvals.items()},
            events={
                run_id: [_clone(entry) for entry in entries]
                for run_id, entries in store.events.items()
            },
            event_sequences=dict(store.event_sequences),
        )


class _MemoryDefinitionRepository(Generic[DefinitionT]):
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot

    def add(self, definition: DefinitionT) -> None:
        key = _definition_key(definition)
        self._snapshot.definitions[key] = _clone(definition)
        self._snapshot.new_definition_keys.add(key)

    def get(self, definition_id: str, version: int) -> DefinitionT | None:
        definition = self._snapshot.definitions.get((definition_id, version))
        if definition is None:
            return None
        return _clone(definition)


class _MemoryRunRepository(RunRepository[RunT]):
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot

    def add(self, run: RunT) -> None:
        self._snapshot.runs[run.run_id] = _clone(run)
        self._snapshot.new_run_keys.add(run.run_id)

    def get(self, run_id: str) -> RunT | None:
        run = self._snapshot.runs.get(run_id)
        if run is None:
            return None
        return _clone(run)

    def save(self, run: RunT, *, expected_revision: int) -> None:
        self._snapshot.runs[run.run_id] = _clone(run)
        if run.run_id not in self._snapshot.new_run_keys:
            self._snapshot.run_expected_revisions.setdefault(run.run_id, expected_revision)


class _MemoryStepRepository(StepRepository[StepT]):
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot

    def add(self, step: StepT) -> None:
        key = _step_key(step)
        self._snapshot.steps[key] = _clone(step)
        self._snapshot.new_step_keys.add(key)

    def get(self, run_id: str, step_id: str) -> StepT | None:
        step = self._snapshot.steps.get((run_id, step_id))
        if step is None:
            return None
        return _clone(step)

    def save(self, step: StepT, *, expected_revision: int) -> None:
        key = _step_key(step)
        self._snapshot.steps[key] = _clone(step)
        if key not in self._snapshot.new_step_keys:
            self._snapshot.step_expected_revisions.setdefault(key, expected_revision)


class _MemoryAttemptRepository(Generic[AttemptT]):
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot

    def add(self, attempt: AttemptT) -> None:
        key = _attempt_key(attempt)
        self._snapshot.attempts[key] = _clone(attempt)
        self._snapshot.new_attempt_keys.add(key)

    def list(self, run_id: str, *, step_id: str | None = None) -> tuple[AttemptT, ...]:
        attempts = [
            _clone(attempt)
            for key, attempt in self._snapshot.attempts.items()
            if key[0] == run_id and (step_id is None or key[1] == step_id)
        ]
        attempts.sort(key=lambda attempt: (attempt.step_id, attempt.attempt_no, attempt.phase))
        return tuple(attempts)


class _MemoryApprovalRepository(Generic[ApprovalT]):
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot

    def add(self, approval: ApprovalT) -> None:
        key = _approval_key(approval)
        self._snapshot.approvals[key] = _clone(approval)
        self._snapshot.new_approval_keys.add(key)

    def list(self, run_id: str) -> tuple[ApprovalT, ...]:
        approvals = [
            _clone(approval)
            for key, approval in self._snapshot.approvals.items()
            if key[0] == run_id
        ]
        approvals.sort(key=lambda approval: approval.approval_key)
        return tuple(approvals)


class _MemoryEventRepository(EventRepository[EventT]):
    def __init__(self, snapshot: _Snapshot) -> None:
        self._snapshot = snapshot

    def append(self, event: EventT) -> SequencedEvent[EventT]:
        next_sequence = self._snapshot.event_sequences.get(event.run_id, 0) + 1
        self._snapshot.event_sequences[event.run_id] = next_sequence
        entry = SequencedEvent(sequence=next_sequence, event=_clone(event))
        self._snapshot.events.setdefault(event.run_id, []).append(entry)
        return _clone(entry)

    def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[SequencedEvent[EventT], ...]:
        entries = self._snapshot.events.get(run_id, [])
        return tuple(
            _clone(entry)
            for entry in entries
            if entry.sequence > after_sequence
        )


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self._snapshot = _Snapshot.from_store(store)
        self._committed = False
        self._rolled_back = False
        self.definitions = _MemoryDefinitionRepository(self._snapshot)
        self.runs = _MemoryRunRepository(self._snapshot)
        self.steps = _MemoryStepRepository(self._snapshot)
        self.attempts = _MemoryAttemptRepository(self._snapshot)
        self.approvals = _MemoryApprovalRepository(self._snapshot)
        self.events = _MemoryEventRepository(self._snapshot)

    def __enter__(self) -> "MemoryUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._committed:
            self.rollback()

    def commit(self) -> None:
        if self._rolled_back:
            return

        self._validate_uniqueness()
        self._validate_revisions()

        self._store.definitions = {
            key: _clone(value) for key, value in self._snapshot.definitions.items()
        }
        self._store.runs = {
            key: _clone(value) for key, value in self._snapshot.runs.items()
        }
        self._store.steps = {
            key: _clone(value) for key, value in self._snapshot.steps.items()
        }
        self._store.attempts = {
            key: _clone(value) for key, value in self._snapshot.attempts.items()
        }
        self._store.approvals = {
            key: _clone(value) for key, value in self._snapshot.approvals.items()
        }
        self._store.events = {
            run_id: [_clone(entry) for entry in entries]
            for run_id, entries in self._snapshot.events.items()
        }
        self._store.event_sequences = dict(self._snapshot.event_sequences)
        self._committed = True

    def rollback(self) -> None:
        self._rolled_back = True

    def _validate_uniqueness(self) -> None:
        for key in self._snapshot.new_definition_keys:
            if key in self._store.definitions:
                raise UniquenessError(
                    record_type="definition",
                    identifier=f"{key[0]}@{key[1]}",
                )

        for key in self._snapshot.new_run_keys:
            if key in self._store.runs:
                raise UniquenessError(record_type="run", identifier=key)

        for key in self._snapshot.new_step_keys:
            if key in self._store.steps:
                raise UniquenessError(
                    record_type="step",
                    identifier=f"{key[0]}:{key[1]}",
                )

        for key in self._snapshot.new_attempt_keys:
            if key in self._store.attempts:
                raise UniquenessError(
                    record_type="attempt",
                    identifier=f"{key[0]}:{key[1]}:{key[2]}:{key[3]}",
                )

        for key in self._snapshot.new_approval_keys:
            if key in self._store.approvals:
                raise UniquenessError(
                    record_type="approval",
                    identifier=f"{key[0]}:{key[1]}",
                )

    def _validate_revisions(self) -> None:
        for run_id, expected_revision in self._snapshot.run_expected_revisions.items():
            current = self._store.runs.get(run_id)
            actual_revision = -1 if current is None else current.revision
            if actual_revision != expected_revision:
                raise OptimisticLockError(
                    aggregate_type="run",
                    identifier=run_id,
                    expected_revision=expected_revision,
                    actual_revision=actual_revision,
                )

        for key, expected_revision in self._snapshot.step_expected_revisions.items():
            current = self._store.steps.get(key)
            actual_revision = -1 if current is None else current.revision
            if actual_revision != expected_revision:
                raise OptimisticLockError(
                    aggregate_type="step",
                    identifier=f"{key[0]}:{key[1]}",
                    expected_revision=expected_revision,
                    actual_revision=actual_revision,
                )
