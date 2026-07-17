from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


OCCURRED_AT = "2026-07-17T00:00:00Z"


def _load_module(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing implementation module: {exc.name}")


@dataclass(slots=True)
class AttemptRecord:
    run_id: str
    step_id: str
    attempt_no: int
    phase: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class ApprovalRecord:
    run_id: str
    approval_key: str
    payload: dict[str, Any] = field(default_factory=dict)


class StubToolCatalog:
    def contains(self, name: str, version: str) -> bool:
        return (name, version) == ("inspect", "1.0.0")

    def validate_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> None:
        if (name, version) != ("inspect", "1.0.0"):
            raise ValueError("unknown tool")
        if "target" not in arguments:
            raise ValueError("target is required")


def build_definition():
    definitions = _load_module("changepilot.workflow.domain.definitions")
    return definitions.WorkflowDefinition.from_mapping(
        {
            "definition_id": "definition-1",
            "version": 1,
            "steps": [
                {
                    "id": "inspect",
                    "tool": {"name": "inspect", "version": "1.0.0"},
                    "arguments": {"target": "orders"},
                }
            ],
        },
        StubToolCatalog(),
    )


def build_run(*, run_id: str = "run-1"):
    runs = _load_module("changepilot.workflow.domain.runs")
    definition = build_definition()
    return runs.WorkflowRun.new(
        run_id=run_id,
        definition_id=definition.definition_id,
        definition_version=definition.version,
        definition_digest=definition.digest,
    )


def build_step(*, run_id: str = "run-1", step_id: str = "inspect"):
    runs = _load_module("changepilot.workflow.domain.runs")
    return runs.StepRun.new(run_id=run_id, step_id=step_id)


class UnitOfWorkContract:
    __test__ = False

    @pytest.fixture
    def persistence_modules(self):
        return {
            "ports": _load_module("changepilot.workflow.ports.persistence"),
            "runs": _load_module("changepilot.workflow.domain.runs"),
            "states": _load_module("changepilot.workflow.domain.states"),
        }

    @pytest.fixture
    def store(self, adapter_modules):
        return adapter_modules["memory"].MemoryStore()

    def make_uow(self, adapter_modules, store):
        return adapter_modules["memory"].MemoryUnitOfWork(store)

    def _seed_graph(self, adapter_modules, store):
        definition = build_definition()
        run = build_run()
        step = build_step()
        with self.make_uow(adapter_modules, store) as uow:
            uow.definitions.add(definition)
            uow.runs.add(run)
            uow.steps.add(step)
            uow.commit()
        return definition, run, step

    def _seed_run_step_pair(
        self,
        adapter_modules,
        store,
        *,
        run_id: str,
        step_id: str = "inspect",
    ):
        run = build_run(run_id=run_id)
        step = build_step(run_id=run_id, step_id=step_id)
        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.add(run)
            uow.steps.add(step)
            uow.commit()
        return run, step

    def test_commit_persists_definitions_state_records_and_events(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, step = self._seed_graph(adapter_modules, store)
        running_run, run_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        ready_step, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
            payload={"labels": ["seed"]},
        )
        approval = ApprovalRecord(
            run_id="run-1",
            approval_key="approval-1",
            payload={"reviewers": ["ops"]},
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.save(running_run, expected_revision=0)
            uow.steps.save(ready_step, expected_revision=0)
            uow.attempts.add(attempt)
            uow.approvals.add(approval)
            uow.events.append(run_event)
            uow.events.append(step_event)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_definition = uow.definitions.get("definition-1", 1)
            persisted_run = uow.runs.get("run-1")
            persisted_step = uow.steps.get("run-1", "inspect")
            persisted_attempts = uow.attempts.list("run-1", step_id="inspect")
            persisted_approvals = uow.approvals.list("run-1")
            persisted_events = uow.events.list("run-1")

        assert persisted_definition == build_definition()
        assert persisted_run == running_run
        assert persisted_step == ready_step
        assert persisted_attempts == (attempt,)
        assert persisted_approvals == (approval,)
        assert [entry.sequence for entry in persisted_events] == [1, 2]
        assert [entry.event for entry in persisted_events] == [run_event, step_event]

    def test_context_exit_without_commit_rolls_back(
        self,
        adapter_modules,
        store,
    ) -> None:
        definition = build_definition()
        run = build_run()

        with self.make_uow(adapter_modules, store) as uow:
            uow.definitions.add(definition)
            uow.runs.add(run)

        with self.make_uow(adapter_modules, store) as uow:
            assert uow.definitions.get("definition-1", 1) is None
            assert uow.runs.get("run-1") is None

    def test_explicit_rollback_is_idempotent(
        self,
        adapter_modules,
        store,
    ) -> None:
        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.add(build_run())
            uow.rollback()
            uow.rollback()

        with self.make_uow(adapter_modules, store) as uow:
            assert uow.runs.get("run-1") is None

    def test_commit_after_rollback_raises_typed_state_error(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.add(build_run())
            uow.rollback()
            with pytest.raises(persistence_modules["ports"].UnitOfWorkStateError):
                uow.commit()

    def test_successful_commit_closes_uow_for_writes_and_repeated_commit(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.add(build_run())
            uow.commit()

            with pytest.raises(persistence_modules["ports"].UnitOfWorkStateError):
                uow.runs.add(build_run(run_id="run-2"))

            with pytest.raises(persistence_modules["ports"].UnitOfWorkStateError):
                uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            assert uow.runs.get("run-1") == build_run()
            assert uow.runs.get("run-2") is None

    def test_failed_commit_leaves_state_and_events_unpublished(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, _run, step = self._seed_graph(adapter_modules, store)

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.add(
                AttemptRecord(
                    run_id="run-1",
                    step_id="inspect",
                    attempt_no=1,
                    phase="forward",
                )
            )
            uow.commit()

        ready_step, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        duplicate_attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(ready_step, expected_revision=0)
            uow.attempts.add(duplicate_attempt)
            uow.events.append(step_event)
            with pytest.raises(persistence_modules["ports"].UniquenessError):
                uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_step = uow.steps.get("run-1", "inspect")
            persisted_events = uow.events.list("run-1")
            persisted_attempts = uow.attempts.list("run-1", step_id="inspect")

        assert persisted_step == step
        assert persisted_events == ()
        assert persisted_attempts == (
            AttemptRecord(
                run_id="run-1",
                step_id="inspect",
                attempt_no=1,
                phase="forward",
            ),
        )

    def test_failed_commit_does_not_consume_published_event_sequence(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, step = self._seed_graph(adapter_modules, store)
        running_run, run_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.save(running_run, expected_revision=0)
            uow.attempts.add(
                AttemptRecord(
                    run_id="run-1",
                    step_id="inspect",
                    attempt_no=1,
                    phase="forward",
                )
            )
            uow.events.append(run_event)
            uow.commit()

        ready_step, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(ready_step, expected_revision=0)
            uow.attempts.add(
                AttemptRecord(
                    run_id="run-1",
                    step_id="inspect",
                    attempt_no=1,
                    phase="forward",
                )
            )
            uow.events.append(step_event)
            with pytest.raises(persistence_modules["ports"].UniquenessError):
                uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(ready_step, expected_revision=0)
            uow.events.append(step_event)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_events = uow.events.list("run-1")

        assert [entry.sequence for entry in persisted_events] == [1, 2]
        assert [entry.event for entry in persisted_events] == [run_event, step_event]

    def test_run_save_raises_typed_optimistic_lock_error(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, _step = self._seed_graph(adapter_modules, store)
        first_update, _event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        stale_update, _event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.save(first_update, expected_revision=0)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.save(stale_update, expected_revision=0)
            with pytest.raises(persistence_modules["ports"].OptimisticLockError):
                uow.commit()

    def test_concurrent_run_commits_merge_different_aggregates(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, first_run, _step = self._seed_graph(adapter_modules, store)
        second_run, _second_step = self._seed_run_step_pair(
            adapter_modules,
            store,
            run_id="run-2",
        )
        first_update, first_event = first_run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        second_update, second_event = second_run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as first_uow, self.make_uow(
            adapter_modules,
            store,
        ) as second_uow:
            first_uow.runs.save(first_update, expected_revision=0)
            first_uow.events.append(first_event)
            second_uow.runs.save(second_update, expected_revision=0)
            second_uow.events.append(second_event)
            first_uow.commit()
            second_uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_first = uow.runs.get("run-1")
            persisted_second = uow.runs.get("run-2")
            first_events = uow.events.list("run-1")
            second_events = uow.events.list("run-2")

        assert persisted_first == first_update
        assert persisted_second == second_update
        assert [entry.sequence for entry in first_events] == [1]
        assert [entry.sequence for entry in second_events] == [1]

    def test_concurrent_same_run_commits_merge_step_updates_and_allocate_unique_sequences(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, _run, first_step = self._seed_graph(adapter_modules, store)
        second_step = build_step(run_id="run-1", step_id="verify")
        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.add(second_step)
            uow.commit()

        first_update, first_event = first_step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        second_update, second_event = second_step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as first_uow, self.make_uow(
            adapter_modules,
            store,
        ) as second_uow:
            first_uow.steps.save(first_update, expected_revision=0)
            first_uow.events.append(first_event)
            second_uow.steps.save(second_update, expected_revision=0)
            second_uow.events.append(second_event)
            first_uow.commit()
            second_uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_first = uow.steps.get("run-1", "inspect")
            persisted_second = uow.steps.get("run-1", "verify")
            persisted_events = uow.events.list("run-1")

        assert persisted_first == first_update
        assert persisted_second == second_update
        assert [entry.sequence for entry in persisted_events] == [1, 2]
        assert [entry.event for entry in persisted_events] == [first_event, second_event]

    def test_concurrent_same_run_update_raises_optimistic_lock_error(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, _step = self._seed_graph(adapter_modules, store)
        first_update, first_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        stale_update, stale_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as first_uow, self.make_uow(
            adapter_modules,
            store,
        ) as second_uow:
            first_uow.runs.save(first_update, expected_revision=0)
            first_uow.events.append(first_event)
            second_uow.runs.save(stale_update, expected_revision=0)
            second_uow.events.append(stale_event)
            first_uow.commit()
            with pytest.raises(persistence_modules["ports"].OptimisticLockError):
                second_uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_run = uow.runs.get("run-1")
            persisted_events = uow.events.list("run-1")

        assert persisted_run == first_update
        assert [entry.event for entry in persisted_events] == [first_event]

    def test_step_save_raises_typed_optimistic_lock_error(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, _run, step = self._seed_graph(adapter_modules, store)
        first_update, _event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        stale_update, _event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(first_update, expected_revision=0)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(stale_update, expected_revision=0)
            with pytest.raises(persistence_modules["ports"].OptimisticLockError):
                uow.commit()

    def test_run_save_rejects_malformed_revision_without_publishing_events(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, _step = self._seed_graph(adapter_modules, store)
        valid_update, run_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        malformed_update = replace(valid_update, revision=valid_update.revision + 1)

        with self.make_uow(adapter_modules, store) as uow:
            uow.events.append(run_event)
            with pytest.raises(persistence_modules["ports"].InvalidRevisionError):
                uow.runs.save(malformed_update, expected_revision=0)

        with self.make_uow(adapter_modules, store) as uow:
            persisted_run = uow.runs.get("run-1")
            persisted_events = uow.events.list("run-1")

        assert persisted_run == run
        assert persisted_events == ()

    def test_step_save_rejects_malformed_revision_without_publishing_events(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, _run, step = self._seed_graph(adapter_modules, store)
        valid_update, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        malformed_update = replace(valid_update, revision=valid_update.revision + 1)

        with self.make_uow(adapter_modules, store) as uow:
            uow.events.append(step_event)
            with pytest.raises(persistence_modules["ports"].InvalidRevisionError):
                uow.steps.save(malformed_update, expected_revision=0)

        with self.make_uow(adapter_modules, store) as uow:
            persisted_step = uow.steps.get("run-1", "inspect")
            persisted_events = uow.events.list("run-1")

        assert persisted_step == step
        assert persisted_events == ()

    def test_event_sequences_are_monotonic_per_run_and_filterable(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, step = self._seed_graph(adapter_modules, store)
        running_run, run_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        ready_step, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.save(running_run, expected_revision=0)
            uow.steps.save(ready_step, expected_revision=0)
            uow.events.append(run_event)
            uow.events.append(step_event)
            uow.commit()

        second_run = persistence_modules["runs"].WorkflowRun.new(
            run_id="run-2",
            definition_id=run.definition_id,
            definition_version=run.definition_version,
            definition_digest=run.definition_digest,
        )
        running_second_run, second_run_event = second_run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.add(second_run)
            uow.runs.save(running_second_run, expected_revision=0)
            uow.events.append(second_run_event)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            run_one_events = uow.events.list("run-1")
            filtered_run_one_events = uow.events.list("run-1", after_sequence=1)
            run_two_events = uow.events.list("run-2")

        assert [entry.sequence for entry in run_one_events] == [1, 2]
        assert [entry.sequence for entry in filtered_run_one_events] == [2]
        assert [entry.sequence for entry in run_two_events] == [1]

    def test_attempt_and_approval_uniqueness_is_enforced(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        self._seed_graph(adapter_modules, store)
        attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
        )
        approval = ApprovalRecord(
            run_id="run-1",
            approval_key="approval-1",
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.add(attempt)
            uow.approvals.add(approval)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.add(
                AttemptRecord(
                    run_id="run-1",
                    step_id="inspect",
                    attempt_no=1,
                    phase="forward",
                )
            )
            with pytest.raises(persistence_modules["ports"].UniquenessError):
                uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            uow.approvals.add(
                ApprovalRecord(
                    run_id="run-1",
                    approval_key="approval-1",
                )
            )
            with pytest.raises(persistence_modules["ports"].UniquenessError):
                uow.commit()

    def test_committed_records_are_isolated_from_mutable_container_references(
        self,
        adapter_modules,
        store,
    ) -> None:
        self._seed_graph(adapter_modules, store)
        attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
            payload={"labels": ["before"]},
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.add(attempt)
            uow.commit()

        attempt.payload["labels"].append("after")

        with self.make_uow(adapter_modules, store) as uow:
            persisted_attempt = uow.attempts.list("run-1", step_id="inspect")[0]
            persisted_attempt.payload["labels"].append("mutated-read")

        with self.make_uow(adapter_modules, store) as uow:
            reloaded_attempt = uow.attempts.list("run-1", step_id="inspect")[0]

        assert reloaded_attempt.payload["labels"] == ["before"]

    def test_attempt_save_persists_completion_in_a_later_transaction(
        self,
        adapter_modules,
        store,
    ) -> None:
        self._seed_graph(adapter_modules, store)
        running_attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
            payload={"status": "running"},
        )
        completed_attempt = replace(
            running_attempt,
            payload={"status": "succeeded"},
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.add(running_attempt)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.save(completed_attempt)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            assert uow.attempts.list("run-1", step_id="inspect") == (
                completed_attempt,
            )

    def test_duplicate_attempt_key_is_rejected_within_single_uow(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        self._seed_graph(adapter_modules, store)
        first_attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
            payload={"labels": ["first"]},
        )
        duplicate_attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
            payload={"labels": ["second"]},
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.attempts.add(first_attempt)

            with pytest.raises(persistence_modules["ports"].UniquenessError) as exc_info:
                uow.attempts.add(duplicate_attempt)

            assert exc_info.value.record_type == "attempt"
            assert exc_info.value.identifier == "run-1:inspect:1:forward"
            assert uow.attempts.list("run-1", step_id="inspect") == (first_attempt,)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_attempts = uow.attempts.list("run-1", step_id="inspect")

        assert persisted_attempts == (first_attempt,)

    def test_duplicate_approval_key_is_rejected_within_single_uow(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        self._seed_graph(adapter_modules, store)
        first_approval = ApprovalRecord(
            run_id="run-1",
            approval_key="approval-1",
            payload={"reviewers": ["ops"]},
        )
        duplicate_approval = ApprovalRecord(
            run_id="run-1",
            approval_key="approval-1",
            payload={"reviewers": ["legal"]},
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.approvals.add(first_approval)

            with pytest.raises(persistence_modules["ports"].UniquenessError) as exc_info:
                uow.approvals.add(duplicate_approval)

            assert exc_info.value.record_type == "approval"
            assert exc_info.value.identifier == "run-1:approval-1"
            assert uow.approvals.list("run-1") == (first_approval,)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_approvals = uow.approvals.list("run-1")

        assert persisted_approvals == (first_approval,)
