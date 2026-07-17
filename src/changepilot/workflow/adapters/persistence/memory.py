from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from threading import Lock
from typing import Any, Callable, Generic, TypeVar

from changepilot.workflow.ports.persistence import (
    ApprovalRecord,
    AttemptRecord,
    DefinitionRecord,
    EventRepository,
    HasRevision,
    InvalidRevisionError,
    OptimisticLockError,
    RunRepository,
    RunScopedRecord,
    SequencedEvent,
    StepRecord,
    StepRepository,
    StepScopedRecord,
    UnitOfWorkStateError,
    UniquenessError,
)


DefinitionT = TypeVar("DefinitionT", bound=DefinitionRecord)
RunT = TypeVar("RunT", bound=HasRevision)
StepT = TypeVar("StepT", bound=StepRecord)
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
    lock: Any = field(default_factory=Lock, repr=False, compare=False)


@dataclass(slots=True)
class _Snapshot:
    definitions: dict[tuple[str, int], object]
    runs: dict[str, object]
    steps: dict[tuple[str, str], object]
    attempts: dict[tuple[str, str, int, str], object]
    approvals: dict[tuple[str, str], object]
    committed_events: dict[str, list[SequencedEvent[object]]]
    committed_event_sequences: dict[str, int]
    pending_events: dict[str, list[object]] = field(default_factory=dict)
    new_definition_keys: set[tuple[str, int]] = field(default_factory=set)
    new_run_keys: set[str] = field(default_factory=set)
    dirty_run_keys: set[str] = field(default_factory=set)
    new_step_keys: set[tuple[str, str]] = field(default_factory=set)
    dirty_step_keys: set[tuple[str, str]] = field(default_factory=set)
    new_attempt_keys: set[tuple[str, str, int, str]] = field(default_factory=set)
    new_approval_keys: set[tuple[str, str]] = field(default_factory=set)
    run_expected_revisions: dict[str, int] = field(default_factory=dict)
    step_expected_revisions: dict[tuple[str, str], int] = field(default_factory=dict)

    @classmethod
    def from_store(cls, store: MemoryStore) -> "_Snapshot":
        with store.lock:
            return cls(
                definitions={
                    key: _clone(value) for key, value in store.definitions.items()
                },
                runs={key: _clone(value) for key, value in store.runs.items()},
                steps={key: _clone(value) for key, value in store.steps.items()},
                attempts={
                    key: _clone(value) for key, value in store.attempts.items()
                },
                approvals={
                    key: _clone(value) for key, value in store.approvals.items()
                },
                committed_events={
                    run_id: [_clone(entry) for entry in entries]
                    for run_id, entries in store.events.items()
                },
                committed_event_sequences=dict(store.event_sequences),
            )

    def stage_event(self, event: RunScopedRecord) -> SequencedEvent[object]:
        staged_event = _clone(event)
        staged_events = self.pending_events.setdefault(event.run_id, [])
        staged_events.append(staged_event)
        sequence = self.committed_event_sequences.get(event.run_id, 0) + len(staged_events)
        return SequencedEvent(sequence=sequence, event=_clone(staged_event))

    def list_events(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[SequencedEvent[object], ...]:
        committed_entries = [
            _clone(entry)
            for entry in self.committed_events.get(run_id, [])
            if entry.sequence > after_sequence
        ]
        staged_entries = []
        base_sequence = self.committed_event_sequences.get(run_id, 0)
        for offset, event in enumerate(self.pending_events.get(run_id, ()), start=1):
            sequence = base_sequence + offset
            if sequence <= after_sequence:
                continue
            staged_entries.append(SequencedEvent(sequence=sequence, event=_clone(event)))
        return tuple(committed_entries + staged_entries)


class _MemoryDefinitionRepository(Generic[DefinitionT]):
    def __init__(
        self,
        snapshot: _Snapshot,
        ensure_writable: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._ensure_writable = ensure_writable

    def add(self, definition: DefinitionT) -> None:
        self._ensure_writable()
        key = _definition_key(definition)
        self._snapshot.definitions[key] = _clone(definition)
        self._snapshot.new_definition_keys.add(key)

    def get(self, definition_id: str, version: int) -> DefinitionT | None:
        definition = self._snapshot.definitions.get((definition_id, version))
        if definition is None:
            return None
        return _clone(definition)


class _MemoryRunRepository(RunRepository[RunT]):
    def __init__(
        self,
        snapshot: _Snapshot,
        ensure_writable: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._ensure_writable = ensure_writable

    def add(self, run: RunT) -> None:
        self._ensure_writable()
        self._snapshot.runs[run.run_id] = _clone(run)
        self._snapshot.new_run_keys.add(run.run_id)

    def get(self, run_id: str) -> RunT | None:
        run = self._snapshot.runs.get(run_id)
        if run is None:
            return None
        return _clone(run)

    def save(self, run: RunT, *, expected_revision: int) -> None:
        self._ensure_writable()
        expected_new_revision = expected_revision + 1
        if run.revision != expected_new_revision:
            raise InvalidRevisionError(
                aggregate_type="run",
                identifier=run.run_id,
                expected_revision=expected_new_revision,
                actual_revision=run.revision,
            )

        self._snapshot.runs[run.run_id] = _clone(run)
        self._snapshot.dirty_run_keys.add(run.run_id)
        if run.run_id not in self._snapshot.new_run_keys:
            self._snapshot.run_expected_revisions.setdefault(run.run_id, expected_revision)


class _MemoryStepRepository(StepRepository[StepT]):
    def __init__(
        self,
        snapshot: _Snapshot,
        ensure_writable: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._ensure_writable = ensure_writable

    def add(self, step: StepT) -> None:
        self._ensure_writable()
        key = _step_key(step)
        self._snapshot.steps[key] = _clone(step)
        self._snapshot.new_step_keys.add(key)

    def get(self, run_id: str, step_id: str) -> StepT | None:
        step = self._snapshot.steps.get((run_id, step_id))
        if step is None:
            return None
        return _clone(step)

    def save(self, step: StepT, *, expected_revision: int) -> None:
        self._ensure_writable()
        key = _step_key(step)
        expected_new_revision = expected_revision + 1
        if step.revision != expected_new_revision:
            raise InvalidRevisionError(
                aggregate_type="step",
                identifier=f"{key[0]}:{key[1]}",
                expected_revision=expected_new_revision,
                actual_revision=step.revision,
            )

        self._snapshot.steps[key] = _clone(step)
        self._snapshot.dirty_step_keys.add(key)
        if key not in self._snapshot.new_step_keys:
            self._snapshot.step_expected_revisions.setdefault(key, expected_revision)


class _MemoryAttemptRepository(Generic[AttemptT]):
    def __init__(
        self,
        snapshot: _Snapshot,
        ensure_writable: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._ensure_writable = ensure_writable

    def add(self, attempt: AttemptT) -> None:
        self._ensure_writable()
        key = _attempt_key(attempt)
        if key in self._snapshot.new_attempt_keys:
            raise UniquenessError(
                record_type="attempt",
                identifier=f"{key[0]}:{key[1]}:{key[2]}:{key[3]}",
            )
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
    def __init__(
        self,
        snapshot: _Snapshot,
        ensure_writable: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._ensure_writable = ensure_writable

    def add(self, approval: ApprovalT) -> None:
        self._ensure_writable()
        key = _approval_key(approval)
        if key in self._snapshot.new_approval_keys:
            raise UniquenessError(
                record_type="approval",
                identifier=f"{key[0]}:{key[1]}",
            )
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
    def __init__(
        self,
        snapshot: _Snapshot,
        ensure_writable: Callable[[], None],
    ) -> None:
        self._snapshot = snapshot
        self._ensure_writable = ensure_writable

    def append(self, event: EventT) -> SequencedEvent[EventT]:
        self._ensure_writable()
        return _clone(self._snapshot.stage_event(event))

    def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[SequencedEvent[EventT], ...]:
        return tuple(
            _clone(entry)
            for entry in self._snapshot.list_events(run_id, after_sequence=after_sequence)
        )


class MemoryUnitOfWork:
    def __init__(self, store: MemoryStore) -> None:
        self._store = store
        self._snapshot = _Snapshot.from_store(store)
        self._committed = False
        self._rolled_back = False
        ensure_writable = self._ensure_writable
        self.definitions = _MemoryDefinitionRepository(self._snapshot, ensure_writable)
        self.runs = _MemoryRunRepository(self._snapshot, ensure_writable)
        self.steps = _MemoryStepRepository(self._snapshot, ensure_writable)
        self.attempts = _MemoryAttemptRepository(self._snapshot, ensure_writable)
        self.approvals = _MemoryApprovalRepository(self._snapshot, ensure_writable)
        self.events = _MemoryEventRepository(self._snapshot, ensure_writable)

    def __enter__(self) -> "MemoryUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if not self._committed:
            self.rollback()

    def commit(self) -> None:
        if self._rolled_back:
            raise UnitOfWorkStateError("cannot commit a rolled back unit of work")
        if self._committed:
            raise UnitOfWorkStateError("cannot commit an already committed unit of work")

        with self._store.lock:
            self._validate_uniqueness_locked()
            self._validate_revisions_locked()
            published_events, published_sequences = self._allocate_published_events_locked()
            self._publish_locked(published_events, published_sequences)

        self._committed = True

    def rollback(self) -> None:
        if self._rolled_back:
            return
        if self._committed:
            raise UnitOfWorkStateError("cannot rollback a committed unit of work")
        self._rolled_back = True

    def _ensure_writable(self) -> None:
        if self._rolled_back:
            raise UnitOfWorkStateError("unit of work is already rolled back")
        if self._committed:
            raise UnitOfWorkStateError("unit of work is already committed")

    def _publish_locked(
        self,
        published_events: dict[str, list[SequencedEvent[object]]],
        published_sequences: dict[str, int],
    ) -> None:
        definition_updates = {
            key: _clone(self._snapshot.definitions[key])
            for key in self._snapshot.new_definition_keys
        }
        run_updates = {
            key: _clone(self._snapshot.runs[key])
            for key in self._snapshot.new_run_keys | self._snapshot.dirty_run_keys
        }
        step_updates = {
            key: _clone(self._snapshot.steps[key])
            for key in self._snapshot.new_step_keys | self._snapshot.dirty_step_keys
        }
        attempt_updates = {
            key: _clone(self._snapshot.attempts[key]) for key in self._snapshot.new_attempt_keys
        }
        approval_updates = {
            key: _clone(self._snapshot.approvals[key])
            for key in self._snapshot.new_approval_keys
        }

        for key, value in definition_updates.items():
            self._store.definitions[key] = value

        for key, value in run_updates.items():
            self._store.runs[key] = value

        for key, value in step_updates.items():
            self._store.steps[key] = value

        for key, value in attempt_updates.items():
            self._store.attempts[key] = value

        for key, value in approval_updates.items():
            self._store.approvals[key] = value

        for run_id, entries in published_events.items():
            self._store.events.setdefault(run_id, []).extend(entries)

        for run_id, sequence in published_sequences.items():
            self._store.event_sequences[run_id] = sequence

    def _allocate_published_events_locked(
        self,
    ) -> tuple[dict[str, list[SequencedEvent[object]]], dict[str, int]]:
        published_events: dict[str, list[SequencedEvent[object]]] = {}
        published_sequences: dict[str, int] = {}

        for run_id, events in self._snapshot.pending_events.items():
            next_sequence = self._store.event_sequences.get(run_id, 0)
            committed_entries: list[SequencedEvent[object]] = []
            for event in events:
                next_sequence += 1
                committed_entries.append(
                    SequencedEvent(sequence=next_sequence, event=_clone(event))
                )
            published_events[run_id] = committed_entries
            published_sequences[run_id] = next_sequence

        return published_events, published_sequences

    def _validate_uniqueness_locked(self) -> None:
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

    def _validate_revisions_locked(self) -> None:
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
