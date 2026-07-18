from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Protocol, TypeVar


DefinitionT = TypeVar("DefinitionT")
RunT = TypeVar("RunT", bound="HasRevision")
StepT = TypeVar("StepT", bound="StepRecord")
AttemptT = TypeVar("AttemptT", bound="AttemptRecord")
ApprovalT = TypeVar("ApprovalT", bound="ApprovalRecord")
EventT = TypeVar("EventT", bound="RunScopedRecord")


class RunScopedRecord(Protocol):
    run_id: str


class HasRevision(RunScopedRecord, Protocol):
    revision: int


class StepScopedRecord(RunScopedRecord, Protocol):
    step_id: str


class StepRecord(HasRevision, StepScopedRecord, Protocol):
    """Step aggregate with explicit run/step identity and optimistic revision."""


class DefinitionRecord(Protocol):
    definition_id: str
    version: int


class AttemptRecord(StepScopedRecord, Protocol):
    attempt_no: int
    phase: str


class ApprovalRecord(RunScopedRecord, Protocol):
    approval_key: str
    version: int
    status: str
    binding_digest: str
    decision: str | None


@dataclass(frozen=True, slots=True)
class LegacyApprovalRecord:
    run_id: str
    approval_key: str
    payload: object
    record_module: str
    record_qualname: str
    decision: object
    version: int = 0
    binding_digest: None = None
    status: str = "legacy"


@dataclass(frozen=True, slots=True)
class SequencedEvent(Generic[EventT]):
    sequence: int
    event: EventT


class PersistenceError(RuntimeError):
    """Base class for persistence failures."""


class OptimisticLockError(PersistenceError):
    def __init__(
        self,
        *,
        aggregate_type: str,
        identifier: str,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        super().__init__(
            f"{aggregate_type} optimistic lock failed for {identifier}: "
            f"expected revision {expected_revision}, found {actual_revision}"
        )
        self.aggregate_type = aggregate_type
        self.identifier = identifier
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class UniquenessError(PersistenceError):
    def __init__(self, *, record_type: str, identifier: str) -> None:
        super().__init__(f"{record_type} already exists: {identifier}")
        self.record_type = record_type
        self.identifier = identifier


class InvalidRevisionError(PersistenceError):
    def __init__(
        self,
        *,
        aggregate_type: str,
        identifier: str,
        expected_revision: int,
        actual_revision: int,
    ) -> None:
        super().__init__(
            f"{aggregate_type} save rejected for {identifier}: "
            f"expected new revision {expected_revision}, found {actual_revision}"
        )
        self.aggregate_type = aggregate_type
        self.identifier = identifier
        self.expected_revision = expected_revision
        self.actual_revision = actual_revision


class UnitOfWorkStateError(PersistenceError):
    """Raised when a unit of work is used after commit or rollback."""


class DefinitionRepository(Protocol[DefinitionT]):
    def add(self, definition: DefinitionT) -> None:
        """Stage a new workflow definition."""

    def get(self, definition_id: str, version: int) -> DefinitionT | None:
        """Return the staged or committed definition."""


class RunRepository(Protocol[RunT]):
    def add(self, run: RunT) -> None:
        """Stage a new workflow run."""

    def get(self, run_id: str) -> RunT | None:
        """Return the staged or committed run."""

    def save(self, run: RunT, *, expected_revision: int) -> None:
        """Stage a run update guarded by optimistic locking."""


class StepRepository(Protocol[StepT]):
    def add(self, step: StepT) -> None:
        """Stage a new step run."""

    def get(self, run_id: str, step_id: str) -> StepT | None:
        """Return the staged or committed step run."""

    def save(self, step: StepT, *, expected_revision: int) -> None:
        """Stage a step update guarded by optimistic locking."""


class AttemptRepository(Protocol[AttemptT]):
    def add(self, attempt: AttemptT) -> None:
        """Stage a step attempt."""

    def save(self, attempt: AttemptT) -> None:
        """Stage an update to an existing step attempt."""

    def list(self, run_id: str, *, step_id: str | None = None) -> tuple[AttemptT, ...]:
        """List attempts visible inside this unit of work."""


class ApprovalRepository(Protocol[ApprovalT]):
    def add(self, approval: ApprovalT) -> None:
        """Stage an approval record."""

    def list(self, run_id: str) -> tuple[ApprovalT, ...]:
        """List approval records visible inside this unit of work."""

    def get(self, run_id: str, approval_key: str) -> ApprovalT | None:
        """Return an approval by its run-scoped request identifier."""

    def pending(self, run_id: str) -> ApprovalT | None:
        """Return the run's single effective pending approval, if present."""

    def save(self, approval: ApprovalT, *, expected_version: int) -> None:
        """Stage an approval update guarded by optimistic locking."""


class EventRepository(Protocol[EventT]):
    def append(self, event: EventT) -> SequencedEvent[EventT]:
        """Stage an event in run-local order; the committed sequence is finalized at commit."""

    def list(
        self,
        run_id: str,
        *,
        after_sequence: int = 0,
    ) -> tuple[SequencedEvent[EventT], ...]:
        """List run events ordered by sequence."""


class UnitOfWork(
    Protocol[DefinitionT, RunT, StepT, AttemptT, ApprovalT, EventT],
):
    definitions: DefinitionRepository[DefinitionT]
    runs: RunRepository[RunT]
    steps: StepRepository[StepT]
    attempts: AttemptRepository[AttemptT]
    approvals: ApprovalRepository[ApprovalT]
    events: EventRepository[EventT]

    def __enter__(self) -> "UnitOfWork[DefinitionT, RunT, StepT, AttemptT, ApprovalT, EventT]":
        """Open a transactional scope."""

    def __exit__(self, exc_type, exc, tb) -> None:
        """Rollback unless commit() completed successfully."""

    def commit(self) -> None:
        """Publish every staged change atomically."""

    def rollback(self) -> None:
        """Discard every staged change."""
