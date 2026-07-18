from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config

from tests.contract.uow_contract import _load_module


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_PATH = ROOT / "alembic" / "versions" / "0001_workflow_runtime.py"
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


def _sqlite_object_sql(connection, *, object_type: str, name: str) -> str:
    return connection.exec_driver_sql(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, name),
    ).scalar_one()


def _foreign_keys_by_id(connection, table_name: str) -> dict[int, list[tuple]]:
    groups: dict[int, list[tuple]] = {}
    for row in connection.exec_driver_sql(f"PRAGMA foreign_key_list('{table_name}')"):
        groups.setdefault(row[0], []).append(row)
    return groups


def test_frozen_migration_uses_explicit_alembic_ddl() -> None:
    source = MIGRATION_PATH.read_text(encoding="utf-8")

    assert "schema import metadata" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    assert source.count("op.create_table(") == len(RUNTIME_TABLES)
    assert "op.create_index(" in source
    assert "op.drop_index(" in source
    assert source.count("op.drop_table(") == len(RUNTIME_TABLES)


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
        assert connection.exec_driver_sql("PRAGMA foreign_key_list('workflow_runs')").all() == []

        step_run_foreign_keys = _foreign_keys_by_id(connection, "step_runs")
        assert list(step_run_foreign_keys) == [0]
        assert step_run_foreign_keys[0] == [
            (0, 0, "workflow_runs", "run_id", "run_id", "NO ACTION", "CASCADE", "NONE")
        ]

        attempt_foreign_keys = _foreign_keys_by_id(connection, "step_attempts")
        assert list(attempt_foreign_keys) == [0]
        assert attempt_foreign_keys[0] == [
            (0, 0, "step_runs", "run_id", "run_id", "NO ACTION", "CASCADE", "NONE"),
            (0, 1, "step_runs", "step_id", "step_id", "NO ACTION", "CASCADE", "NONE"),
        ]

        event_foreign_keys = _foreign_keys_by_id(connection, "audit_events")
        assert list(event_foreign_keys) == [0, 1]
        assert event_foreign_keys[0] == [
            (0, 0, "step_runs", "run_id", "run_id", "NO ACTION", "CASCADE", "NONE"),
            (0, 1, "step_runs", "step_id", "step_id", "NO ACTION", "CASCADE", "NONE"),
        ]
        assert event_foreign_keys[1] == [
            (1, 0, "workflow_runs", "run_id", "run_id", "NO ACTION", "CASCADE", "NONE")
        ]

        approval_index_sql = _sqlite_object_sql(
            connection,
            object_type="index",
            name="ix_approval_requests_pending_unique",
        )
        assert approval_index_sql == (
            "CREATE UNIQUE INDEX ix_approval_requests_pending_unique "
            "ON approval_requests (run_id) WHERE status = 'pending'"
        )

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
