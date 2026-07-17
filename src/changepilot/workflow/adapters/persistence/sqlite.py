from __future__ import annotations

import importlib
import json
from copy import deepcopy
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Generic, TypeVar

from sqlalchemy import create_engine, insert, select, update
from sqlalchemy.engine import Connection, Engine
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy import event as sqlalchemy_event

from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.persistence import (
    ApprovalRecord,
    AttemptRecord,
    DefinitionRecord,
    EventRepository,
    HasRevision,
    InvalidRevisionError,
    OptimisticLockError,
    PersistenceError,
    RunRepository,
    RunScopedRecord,
    SequencedEvent,
    StepRecord,
    StepRepository,
    StepScopedRecord,
    UnitOfWorkStateError,
    UniquenessError,
)

from .schema import (
    approval_requests,
    audit_events,
    step_attempts,
    step_runs,
    workflow_definitions,
    workflow_runs,
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


def _record_identity(record: object) -> tuple[str, str]:
    record_type = type(record)
    return record_type.__module__, record_type.__qualname__


def _resolve_qualname(module_name: str, qualname: str):
    current = importlib.import_module(module_name)
    for part in qualname.split("."):
        current = getattr(current, part)
    return current


def _freeze_value(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return {key: _freeze_value(item) for key, item in value.items()}
    if isinstance(value, dict):
        return {key: _freeze_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_freeze_value(item) for item in value]
    if isinstance(value, tuple):
        return [_freeze_value(item) for item in value]
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            field_def.name: _freeze_value(getattr(value, field_def.name))
            for field_def in fields(value)
        }
    return value


def _record_payload(record: object) -> dict[str, object]:
    if is_dataclass(record):
        values = {
            field_def.name: getattr(record, field_def.name) for field_def in fields(record)
        }
    elif hasattr(record, "__dict__"):
        values = vars(record)
    else:
        raise TypeError(f"cannot persist record type {type(record)!r}")
    return {key: _freeze_value(value) for key, value in values.items()}


class _PassthroughToolCatalog:
    def contains(self, name: str, version: str) -> bool:
        return True

    def validate_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> None:
        return None


def _definition_to_mapping(definition: WorkflowDefinition) -> dict[str, object]:
    return {
        "definition_id": definition.definition_id,
        "version": definition.version,
        "steps": [
            {
                "id": step.id,
                "tool": {
                    "name": step.tool.name,
                    "version": step.tool.version,
                },
                "arguments": _freeze_value(step.arguments),
                "depends_on": list(step.depends_on),
                "risk": step.risk,
                "retry": {
                    "max_attempts": step.retry.max_attempts,
                    "initial_backoff_seconds": step.retry.initial_backoff_seconds,
                },
                "compensation_tool": (
                    None
                    if step.compensation_tool is None
                    else {
                        "name": step.compensation_tool.name,
                        "version": step.compensation_tool.version,
                    }
                ),
            }
            for step in definition.steps
        ],
    }


def _definition_from_mapping(payload: dict[str, object]) -> WorkflowDefinition:
    return WorkflowDefinition.from_mapping(payload, _PassthroughToolCatalog())


def _serialize_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _deserialize_generic_record(module_name: str, qualname: str, payload_json: str):
    record_type = _resolve_qualname(module_name, qualname)
    return record_type(**json.loads(payload_json))


def _load_definition_rows(connection: Connection) -> dict[tuple[str, int], object]:
    rows = connection.execute(select(workflow_definitions)).mappings().all()
    return {
        (row["definition_id"], row["version"]): _definition_from_mapping(
            json.loads(row["definition_json"])
        )
        for row in rows
    }


def _load_run_rows(connection: Connection) -> dict[str, object]:
    rows = connection.execute(select(workflow_runs)).mappings().all()
    return {
        row["run_id"]: WorkflowRun(
            run_id=row["run_id"],
            definition_id=row["definition_id"],
            definition_version=row["definition_version"],
            definition_digest=row["definition_digest"],
            state=RunState(row["state"]),
            revision=row["revision"],
        )
        for row in rows
    }


def _load_step_rows(connection: Connection) -> dict[tuple[str, str], object]:
    rows = connection.execute(select(step_runs)).mappings().all()
    return {
        (row["run_id"], row["step_id"]): StepRun(
            run_id=row["run_id"],
            step_id=row["step_id"],
            state=StepState(row["state"]),
            revision=row["revision"],
        )
        for row in rows
    }


def _load_attempt_rows(connection: Connection) -> dict[tuple[str, str, int, str], object]:
    rows = connection.execute(select(step_attempts)).mappings().all()
    return {
        (row["run_id"], row["step_id"], row["attempt_no"], row["phase"]): _deserialize_generic_record(
            row["record_module"],
            row["record_qualname"],
            row["payload_json"],
        )
        for row in rows
    }


def _load_approval_rows(connection: Connection) -> dict[tuple[str, str], object]:
    rows = connection.execute(select(approval_requests)).mappings().all()
    return {
        (row["run_id"], row["approval_key"]): _deserialize_generic_record(
            row["record_module"],
            row["record_qualname"],
            row["payload_json"],
        )
        for row in rows
    }


def _load_event_rows(
    connection: Connection,
) -> tuple[dict[str, list[SequencedEvent[object]]], dict[str, int]]:
    rows = connection.execute(
        select(audit_events).order_by(audit_events.c.run_id, audit_events.c.sequence)
    ).mappings()
    events_by_run: dict[str, list[SequencedEvent[object]]] = {}
    for row in rows:
        events_by_run.setdefault(row["run_id"], []).append(
            SequencedEvent(
                sequence=row["sequence"],
                event=_deserialize_generic_record(
                    row["record_module"],
                    row["record_qualname"],
                    row["payload_json"],
                ),
            )
        )

    sequences = {
        row["run_id"]: row["last_event_sequence"]
        for row in connection.execute(select(workflow_runs.c.run_id, workflow_runs.c.last_event_sequence)).mappings()
    }
    return events_by_run, sequences


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
    saved_run_keys: set[str] = field(default_factory=set)
    saved_step_keys: set[tuple[str, str]] = field(default_factory=set)

    @classmethod
    def from_connection(cls, connection: Connection) -> "_Snapshot":
        committed_events, committed_sequences = _load_event_rows(connection)
        return cls(
            definitions={key: _clone(value) for key, value in _load_definition_rows(connection).items()},
            runs={key: _clone(value) for key, value in _load_run_rows(connection).items()},
            steps={key: _clone(value) for key, value in _load_step_rows(connection).items()},
            attempts={key: _clone(value) for key, value in _load_attempt_rows(connection).items()},
            approvals={key: _clone(value) for key, value in _load_approval_rows(connection).items()},
            committed_events={
                run_id: [_clone(entry) for entry in entries]
                for run_id, entries in committed_events.items()
            },
            committed_event_sequences=dict(committed_sequences),
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

    def mark_committed(
        self,
        published_events: dict[str, list[SequencedEvent[object]]],
        published_sequences: dict[str, int],
    ) -> None:
        for run_id, sequence in published_sequences.items():
            self.committed_event_sequences[run_id] = sequence
        for run_id, entries in published_events.items():
            self.committed_events.setdefault(run_id, []).extend(_clone(entries))
        self.pending_events.clear()


class _SQLiteDefinitionRepository(Generic[DefinitionT]):
    def __init__(self, snapshot: _Snapshot, ensure_writable: Callable[[], None]) -> None:
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


class _SQLiteRunRepository(RunRepository[RunT]):
    def __init__(self, snapshot: _Snapshot, ensure_writable: Callable[[], None]) -> None:
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
        if run.run_id in self._snapshot.saved_run_keys:
            raise PersistenceError(
                f"multiple run saves in a single unit of work are not supported: {run.run_id}"
            )

        self._snapshot.runs[run.run_id] = _clone(run)
        self._snapshot.dirty_run_keys.add(run.run_id)
        self._snapshot.saved_run_keys.add(run.run_id)
        if run.run_id not in self._snapshot.new_run_keys:
            self._snapshot.run_expected_revisions.setdefault(run.run_id, expected_revision)


class _SQLiteStepRepository(StepRepository[StepT]):
    def __init__(self, snapshot: _Snapshot, ensure_writable: Callable[[], None]) -> None:
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
        if key in self._snapshot.saved_step_keys:
            raise PersistenceError(
                "multiple step saves in a single unit of work are not supported: "
                f"{key[0]}:{key[1]}"
            )

        self._snapshot.steps[key] = _clone(step)
        self._snapshot.dirty_step_keys.add(key)
        self._snapshot.saved_step_keys.add(key)
        if key not in self._snapshot.new_step_keys:
            self._snapshot.step_expected_revisions.setdefault(key, expected_revision)


class _SQLiteAttemptRepository(Generic[AttemptT]):
    def __init__(self, snapshot: _Snapshot, ensure_writable: Callable[[], None]) -> None:
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


class _SQLiteApprovalRepository(Generic[ApprovalT]):
    def __init__(self, snapshot: _Snapshot, ensure_writable: Callable[[], None]) -> None:
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


class _SQLiteEventRepository(EventRepository[EventT]):
    def __init__(self, snapshot: _Snapshot, ensure_writable: Callable[[], None]) -> None:
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


def create_sqlite_engine(path: str | Path) -> Engine:
    database_path = Path(path).expanduser().resolve()
    database_path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(f"sqlite:///{database_path.as_posix()}")

    @sqlalchemy_event.listens_for(engine, "connect")
    def _configure_pragmas(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys = ON")
        cursor.execute("PRAGMA journal_mode = WAL")
        cursor.execute("PRAGMA busy_timeout = 5000")
        cursor.close()

    return engine


class SQLiteUnitOfWork:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._connection = engine.connect()
        self._snapshot = _Snapshot.from_connection(self._connection)
        if self._connection.in_transaction():
            self._connection.rollback()
        self._committed = False
        self._rolled_back = False
        ensure_writable = self._ensure_writable
        self.definitions = _SQLiteDefinitionRepository(self._snapshot, ensure_writable)
        self.runs = _SQLiteRunRepository(self._snapshot, ensure_writable)
        self.steps = _SQLiteStepRepository(self._snapshot, ensure_writable)
        self.attempts = _SQLiteAttemptRepository(self._snapshot, ensure_writable)
        self.approvals = _SQLiteApprovalRepository(self._snapshot, ensure_writable)
        self.events = _SQLiteEventRepository(self._snapshot, ensure_writable)

    def __enter__(self) -> "SQLiteUnitOfWork":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            if not self._committed:
                self.rollback()
        finally:
            self._connection.close()

    def commit(self) -> None:
        if self._rolled_back:
            raise UnitOfWorkStateError("cannot commit a rolled back unit of work")
        if self._committed:
            raise UnitOfWorkStateError("cannot commit an already committed unit of work")

        try:
            self._connection.exec_driver_sql("BEGIN IMMEDIATE")
            self._validate_uniqueness()
            published_events, published_sequences = self._allocate_published_events()
            self._publish(published_events, published_sequences)
            self._connection.commit()
        except (InvalidRevisionError, OptimisticLockError, UniquenessError):
            self.rollback()
            raise
        except IntegrityError as exc:
            self.rollback()
            raise self._translate_integrity_error(exc) from exc
        except TypeError as exc:
            self.rollback()
            raise PersistenceError(str(exc)) from exc
        except SQLAlchemyError as exc:
            self.rollback()
            raise PersistenceError(str(exc)) from exc

        self._snapshot.mark_committed(published_events, published_sequences)
        self._committed = True

    def rollback(self) -> None:
        if self._rolled_back:
            return
        if self._committed:
            raise UnitOfWorkStateError("cannot rollback a committed unit of work")
        if self._connection.in_transaction():
            self._connection.rollback()
        self._rolled_back = True

    def _ensure_writable(self) -> None:
        if self._rolled_back:
            raise UnitOfWorkStateError("unit of work is already rolled back")
        if self._committed:
            raise UnitOfWorkStateError("unit of work is already committed")

    def _validate_uniqueness(self) -> None:
        for key in self._snapshot.new_definition_keys:
            existing = self._connection.execute(
                select(workflow_definitions.c.definition_id).where(
                    workflow_definitions.c.definition_id == key[0],
                    workflow_definitions.c.version == key[1],
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise UniquenessError(record_type="definition", identifier=f"{key[0]}@{key[1]}")

        for key in self._snapshot.new_run_keys:
            existing = self._connection.execute(
                select(workflow_runs.c.run_id).where(workflow_runs.c.run_id == key)
            ).scalar_one_or_none()
            if existing is not None:
                raise UniquenessError(record_type="run", identifier=key)

        for key in self._snapshot.new_step_keys:
            existing = self._connection.execute(
                select(step_runs.c.run_id).where(
                    step_runs.c.run_id == key[0],
                    step_runs.c.step_id == key[1],
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise UniquenessError(record_type="step", identifier=f"{key[0]}:{key[1]}")

        for key in self._snapshot.new_attempt_keys:
            existing = self._connection.execute(
                select(step_attempts.c.run_id).where(
                    step_attempts.c.run_id == key[0],
                    step_attempts.c.step_id == key[1],
                    step_attempts.c.attempt_no == key[2],
                    step_attempts.c.phase == key[3],
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise UniquenessError(
                    record_type="attempt",
                    identifier=f"{key[0]}:{key[1]}:{key[2]}:{key[3]}",
                )

        for key in self._snapshot.new_approval_keys:
            existing = self._connection.execute(
                select(approval_requests.c.run_id).where(
                    approval_requests.c.run_id == key[0],
                    approval_requests.c.approval_key == key[1],
                )
            ).scalar_one_or_none()
            if existing is not None:
                raise UniquenessError(
                    record_type="approval",
                    identifier=f"{key[0]}:{key[1]}",
                )

    def _actual_run_revision(self, run_id: str) -> int:
        actual_revision = self._connection.execute(
            select(workflow_runs.c.revision).where(workflow_runs.c.run_id == run_id)
        ).scalar_one_or_none()
        if actual_revision is None:
            return -1
        return actual_revision

    def _actual_step_revision(self, key: tuple[str, str]) -> int:
        actual_revision = self._connection.execute(
            select(step_runs.c.revision).where(
                step_runs.c.run_id == key[0],
                step_runs.c.step_id == key[1],
            )
        ).scalar_one_or_none()
        if actual_revision is None:
            return -1
        return actual_revision

    def _allocate_published_events(
        self,
    ) -> tuple[dict[str, list[SequencedEvent[object]]], dict[str, int]]:
        published_events: dict[str, list[SequencedEvent[object]]] = {}
        published_sequences: dict[str, int] = {}

        for run_id, events in self._snapshot.pending_events.items():
            current_sequence = self._connection.execute(
                select(workflow_runs.c.last_event_sequence).where(workflow_runs.c.run_id == run_id)
            ).scalar_one_or_none()
            if current_sequence is None:
                if run_id in self._snapshot.new_run_keys:
                    next_sequence = 0
                else:
                    raise PersistenceError(f"missing workflow run for events: {run_id}")
            else:
                next_sequence = current_sequence
            committed_entries: list[SequencedEvent[object]] = []
            for event in events:
                next_sequence += 1
                committed_entries.append(
                    SequencedEvent(sequence=next_sequence, event=_clone(event))
                )
            published_events[run_id] = committed_entries
            published_sequences[run_id] = next_sequence

        return published_events, published_sequences

    def _publish(
        self,
        published_events: dict[str, list[SequencedEvent[object]]],
        published_sequences: dict[str, int],
    ) -> None:
        for key in self._snapshot.new_definition_keys:
            definition = self._snapshot.definitions[key]
            self._connection.execute(
                insert(workflow_definitions).values(
                    definition_id=definition.definition_id,
                    version=definition.version,
                    digest=definition.digest,
                    definition_json=_serialize_json(_definition_to_mapping(definition)),
                )
            )

        for key in self._snapshot.new_run_keys:
            run = self._snapshot.runs[key]
            self._connection.execute(
                insert(workflow_runs).values(
                    run_id=run.run_id,
                    definition_id=run.definition_id,
                    definition_version=run.definition_version,
                    definition_digest=run.definition_digest,
                    state=run.state.value,
                    revision=run.revision,
                    last_event_sequence=self._snapshot.committed_event_sequences.get(run.run_id, 0),
                )
            )

        for key in self._snapshot.dirty_run_keys - self._snapshot.new_run_keys:
            run = self._snapshot.runs[key]
            expected_revision = self._snapshot.run_expected_revisions[run.run_id]
            result = self._connection.execute(
                update(workflow_runs)
                .where(
                    workflow_runs.c.run_id == run.run_id,
                    workflow_runs.c.revision == expected_revision,
                )
                .values(
                    definition_id=run.definition_id,
                    definition_version=run.definition_version,
                    definition_digest=run.definition_digest,
                    state=run.state.value,
                    revision=run.revision,
                )
            )
            if result.rowcount != 1:
                raise OptimisticLockError(
                    aggregate_type="run",
                    identifier=run.run_id,
                    expected_revision=expected_revision,
                    actual_revision=self._actual_run_revision(run.run_id),
                )

        for key in self._snapshot.new_step_keys:
            step = self._snapshot.steps[key]
            self._connection.execute(
                insert(step_runs).values(
                    run_id=step.run_id,
                    step_id=step.step_id,
                    state=step.state.value,
                    revision=step.revision,
                )
            )

        for key in self._snapshot.dirty_step_keys - self._snapshot.new_step_keys:
            step = self._snapshot.steps[key]
            expected_revision = self._snapshot.step_expected_revisions[key]
            result = self._connection.execute(
                update(step_runs)
                .where(
                    step_runs.c.run_id == step.run_id,
                    step_runs.c.step_id == step.step_id,
                    step_runs.c.revision == expected_revision,
                )
                .values(
                    state=step.state.value,
                    revision=step.revision,
                )
            )
            if result.rowcount != 1:
                raise OptimisticLockError(
                    aggregate_type="step",
                    identifier=f"{key[0]}:{key[1]}",
                    expected_revision=expected_revision,
                    actual_revision=self._actual_step_revision(key),
                )

        for key in self._snapshot.new_attempt_keys:
            attempt = self._snapshot.attempts[key]
            module_name, qualname = _record_identity(attempt)
            self._connection.execute(
                insert(step_attempts).values(
                    run_id=attempt.run_id,
                    step_id=attempt.step_id,
                    attempt_no=attempt.attempt_no,
                    phase=attempt.phase,
                    record_module=module_name,
                    record_qualname=qualname,
                    payload_json=_serialize_json(_record_payload(attempt)),
                )
            )

        for key in self._snapshot.new_approval_keys:
            approval = self._snapshot.approvals[key]
            module_name, qualname = _record_identity(approval)
            self._connection.execute(
                insert(approval_requests).values(
                    run_id=approval.run_id,
                    approval_key=approval.approval_key,
                    record_module=module_name,
                    record_qualname=qualname,
                    payload_json=_serialize_json(_record_payload(approval)),
                )
            )

        for run_id, entries in published_events.items():
            for entry in entries:
                event_record = entry.event
                module_name, qualname = _record_identity(event_record)
                self._connection.execute(
                    insert(audit_events).values(
                        run_id=event_record.run_id,
                        sequence=entry.sequence,
                        step_id=getattr(event_record, "step_id", None),
                        event_type=getattr(event_record, "event_type"),
                        occurred_at=getattr(event_record, "occurred_at"),
                        previous_state=getattr(event_record, "previous_state"),
                        new_state=getattr(event_record, "new_state"),
                        previous_revision=getattr(event_record, "previous_revision"),
                        revision=getattr(event_record, "revision"),
                        record_module=module_name,
                        record_qualname=qualname,
                        payload_json=_serialize_json(_record_payload(event_record)),
                    )
                )
            self._connection.execute(
                update(workflow_runs)
                .where(workflow_runs.c.run_id == run_id)
                .values(last_event_sequence=published_sequences[run_id])
            )

    def _translate_integrity_error(self, exc: IntegrityError) -> PersistenceError:
        message = str(exc.orig).lower()
        if "foreign key constraint failed" in message:
            return PersistenceError("foreign key constraint failed")
        return PersistenceError(str(exc.orig))
