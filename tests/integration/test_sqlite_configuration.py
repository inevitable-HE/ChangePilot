from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from tests.contract.uow_contract import _load_module


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_TABLES = {
    "workflow_definitions",
    "workflow_runs",
    "step_runs",
    "step_attempts",
    "approval_requests",
    "audit_events",
}


def _database_url(path: Path) -> str:
    return f"sqlite:///{path.resolve().as_posix()}"


def _alembic_config(database_path: Path) -> Config:
    config = Config(str(ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", _database_url(database_path))
    return config


def _fetch_tables(connection) -> set[str]:
    rows = connection.exec_driver_sql(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    ).all()
    return {row[0] for row in rows}


def test_create_sqlite_engine_enables_sqlite_pragmas(tmp_path: Path) -> None:
    sqlite = _load_module("changepilot.workflow.adapters.persistence.sqlite")
    engine = sqlite.create_sqlite_engine(tmp_path / "pragma-check.db")

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000
        assert (
            connection.exec_driver_sql("PRAGMA journal_mode").scalar_one().lower() == "wal"
        )

    with engine.connect() as connection:
        assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
        assert connection.exec_driver_sql("PRAGMA busy_timeout").scalar_one() == 5000

    engine.dispose()


def test_alembic_upgrade_creates_runtime_schema_and_constraints(tmp_path: Path) -> None:
    sqlite = _load_module("changepilot.workflow.adapters.persistence.sqlite")
    database_path = tmp_path / "runtime-schema.db"
    command.upgrade(_alembic_config(database_path), "head")
    engine = sqlite.create_sqlite_engine(database_path)

    with engine.connect() as connection:
        tables = _fetch_tables(connection)
        assert RUNTIME_TABLES.issubset(tables)
        assert "alembic_version" in tables
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0001_workflow_runtime"
        )

        workflow_run_columns = {
            row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info('workflow_runs')")
        }
        assert workflow_run_columns["last_event_sequence"][3] == 1
        assert workflow_run_columns["revision"][3] == 1

        step_run_foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('step_runs')"
        ).all()
        assert any(
            row[2] == "workflow_runs" and row[3] == "run_id" and row[4] == "run_id"
            for row in step_run_foreign_keys
        )

        attempt_foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('step_attempts')"
        ).all()
        assert any(
            row[2] == "step_runs" and row[3] == "run_id" and row[4] == "run_id"
            for row in attempt_foreign_keys
        )
        assert any(
            row[2] == "step_runs" and row[3] == "step_id" and row[4] == "step_id"
            for row in attempt_foreign_keys
        )

        event_foreign_keys = connection.exec_driver_sql(
            "PRAGMA foreign_key_list('audit_events')"
        ).all()
        assert any(
            row[2] == "workflow_runs" and row[3] == "run_id" and row[4] == "run_id"
            for row in event_foreign_keys
        )

        approval_indexes = connection.exec_driver_sql(
            "PRAGMA index_list('approval_requests')"
        ).all()
        assert any(row[2] for row in approval_indexes)

    engine.dispose()


def test_alembic_downgrade_removes_runtime_schema(tmp_path: Path) -> None:
    sqlite = _load_module("changepilot.workflow.adapters.persistence.sqlite")
    database_path = tmp_path / "runtime-downgrade.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "head")
    command.downgrade(config, "base")
    engine = sqlite.create_sqlite_engine(database_path)

    with engine.connect() as connection:
        tables = _fetch_tables(connection)

    engine.dispose()
    assert RUNTIME_TABLES.isdisjoint(tables)
