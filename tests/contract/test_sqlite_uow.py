from __future__ import annotations

from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import event as sqlalchemy_event

from tests.contract.uow_contract import (
    OCCURRED_AT,
    AttemptRecord,
    UnitOfWorkContract,
    _load_module,
)


ROOT = Path(__file__).resolve().parents[2]


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", _database_url(database_path))
    return config


def _upgrade_to_head(database_path: Path) -> None:
    command.upgrade(_alembic_config(database_path), "head")


@pytest.fixture
def adapter_modules():
    return {
        "sqlite": _load_module("changepilot.workflow.adapters.persistence.sqlite"),
    }


class TestSQLiteUnitOfWork(UnitOfWorkContract):
    __test__ = True

    @pytest.fixture
    def store(self, tmp_path: Path, adapter_modules):
        database_path = tmp_path / "workflow-runtime.db"
        _upgrade_to_head(database_path)
        engine = adapter_modules["sqlite"].create_sqlite_engine(database_path)
        try:
            yield engine
        finally:
            engine.dispose()

    def make_uow(self, adapter_modules, store):
        return adapter_modules["sqlite"].SQLiteUnitOfWork(store)

    def test_commit_rollback_on_json_serialization_failure_leaves_uow_terminal(
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
            uow.events.append(run_event)
            uow.commit()

        ready_step, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        bad_attempt = AttemptRecord(
            run_id="run-1",
            step_id="inspect",
            attempt_no=1,
            phase="forward",
            payload={"bad": {1, 2, 3}},
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(ready_step, expected_revision=0)
            uow.attempts.add(bad_attempt)
            uow.events.append(step_event)

            with pytest.raises(persistence_modules["ports"].PersistenceError) as exc_info:
                uow.commit()

            assert isinstance(exc_info.value.__cause__, TypeError)

            with pytest.raises(persistence_modules["ports"].UnitOfWorkStateError):
                uow.steps.save(ready_step, expected_revision=0)

            with pytest.raises(persistence_modules["ports"].UnitOfWorkStateError):
                uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_step = uow.steps.get("run-1", "inspect")
            persisted_attempts = uow.attempts.list("run-1", step_id="inspect")
            persisted_events = uow.events.list("run-1")

        assert persisted_step == step
        assert persisted_attempts == ()
        assert [entry.sequence for entry in persisted_events] == [1]
        assert [entry.event for entry in persisted_events] == [run_event]

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(ready_step, expected_revision=0)
            uow.events.append(step_event)
            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            recovered_events = uow.events.list("run-1")

        assert [entry.sequence for entry in recovered_events] == [1, 2]
        assert [entry.event for entry in recovered_events] == [run_event, step_event]

    def test_same_uow_rejects_multiple_run_saves_for_same_aggregate(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, run, _step = self._seed_graph(adapter_modules, store)
        running_run, run_event = run.transition(
            persistence_modules["states"].RunState.RUNNING,
            occurred_at=OCCURRED_AT,
        )
        succeeded_run, _ignored_event = running_run.transition(
            persistence_modules["states"].RunState.SUCCEEDED,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.runs.save(running_run, expected_revision=0)
            uow.events.append(run_event)

            with pytest.raises(persistence_modules["ports"].PersistenceError):
                uow.runs.save(succeeded_run, expected_revision=1)

            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_run = uow.runs.get("run-1")
            persisted_events = uow.events.list("run-1")

        assert persisted_run == running_run
        assert [entry.event for entry in persisted_events] == [run_event]

    def test_same_uow_rejects_multiple_step_saves_for_same_aggregate(
        self,
        adapter_modules,
        persistence_modules,
        store,
    ) -> None:
        _definition, _run, step = self._seed_graph(adapter_modules, store)
        ready_step, step_event = step.transition(
            persistence_modules["states"].StepState.READY,
            occurred_at=OCCURRED_AT,
        )
        running_step, _ignored_event = ready_step.transition(
            persistence_modules["states"].StepState.RUNNING,
            occurred_at=OCCURRED_AT,
        )

        with self.make_uow(adapter_modules, store) as uow:
            uow.steps.save(ready_step, expected_revision=0)
            uow.events.append(step_event)

            with pytest.raises(persistence_modules["ports"].PersistenceError):
                uow.steps.save(running_step, expected_revision=1)

            uow.commit()

        with self.make_uow(adapter_modules, store) as uow:
            persisted_step = uow.steps.get("run-1", "inspect")
            persisted_events = uow.events.list("run-1")

        assert persisted_step == ready_step
        assert [entry.event for entry in persisted_events] == [step_event]

    def test_commit_updates_include_expected_revision_in_sql_predicates(
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
        statements: list[str] = []

        def _capture_sql(
            _conn,
            _cursor,
            statement,
            _parameters,
            _context,
            _executemany,
        ) -> None:
            if statement.startswith("UPDATE workflow_runs") or statement.startswith(
                "UPDATE step_runs"
            ):
                statements.append(statement)

        sqlalchemy_event.listen(store, "before_cursor_execute", _capture_sql)
        try:
            with self.make_uow(adapter_modules, store) as uow:
                uow.runs.save(running_run, expected_revision=0)
                uow.steps.save(ready_step, expected_revision=0)
                uow.events.append(run_event)
                uow.events.append(step_event)
                uow.commit()
        finally:
            sqlalchemy_event.remove(store, "before_cursor_execute", _capture_sql)

        assert any(
            "SET definition_id" in statement and "workflow_runs.revision" in statement
            for statement in statements
        )
        assert any(
            "SET state" in statement
            and "step_runs.run_id" in statement
            and "step_runs.step_id" in statement
            and "step_runs.revision" in statement
            for statement in statements
        )
