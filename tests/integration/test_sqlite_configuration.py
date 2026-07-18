from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.exc import IntegrityError

from changepilot.workflow.application.approvals import ApprovalRequest
from changepilot.workflow.ports.persistence import PersistenceError
from tests.contract.uow_contract import _load_module


ROOT = Path(__file__).resolve().parents[2]
MIGRATION_V1_PATH = ROOT / "alembic" / "versions" / "0001_workflow_runtime.py"
MIGRATION_V2_PATH = ROOT / "alembic" / "versions" / "0002_durable_approvals.py"
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
    source = MIGRATION_V1_PATH.read_text(encoding="utf-8")

    assert "schema import metadata" not in source
    assert "metadata.create_all" not in source
    assert "metadata.drop_all" not in source
    approval_source = source.split('"approval_requests"', 1)[1].split(
        '"audit_events"',
        1,
    )[0]
    assert 'sa.Column("version"' not in approval_source
    assert 'sa.Column("binding_digest"' not in approval_source
    assert 'sa.Column("status"' not in approval_source
    assert 'sqlite_where=sa.text("decision IS NULL")' in source
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
            == "0002_durable_approvals"
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
        approval_table_sql = _sqlite_object_sql(
            connection,
            object_type="table",
            name="approval_requests",
        )
        assert "ck_approval_requests_version" in approval_table_sql
        assert "ck_approval_requests_status" in approval_table_sql
        assert "ck_approval_requests_decision" in approval_table_sql
        assert "ck_approval_requests_state" in approval_table_sql

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


def test_legacy_0001_upgrade_to_head_and_downgrade_preserves_approval_data(
    tmp_path: Path,
) -> None:
    sqlite = _load_module("changepilot.workflow.adapters.persistence.sqlite")
    database_path = tmp_path / "legacy-upgrade.db"
    config = _alembic_config(database_path)
    command.upgrade(config, "0001_workflow_runtime")
    engine = sqlite.create_sqlite_engine(database_path)
    request = ApprovalRequest(
        id="legacy-request",
        run_id="legacy-run",
        definition_digest="legacy-definition-digest",
        step_id="inspect",
        tool_name="inspect",
        tool_version="1.0.0",
        redacted_arguments={"target": "orders"},
        binding_digest="legacy-binding-digest",
        risk_reasons=("tool_risk_high",),
        created_at="2026-07-17T00:00:00+00:00",
    )
    definition_json = json.dumps(
        {
            "definition_id": "legacy-definition",
            "version": 1,
            "steps": [
                {
                    "id": "inspect",
                    "tool": {"name": "inspect", "version": "1.0.0"},
                    "arguments": {"target": "orders"},
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    payload_json = json.dumps(asdict(request), separators=(",", ":"), sort_keys=True)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workflow_definitions "
            "(definition_id, version, digest, definition_json) VALUES (?, ?, ?, ?)",
            ("legacy-definition", 1, "legacy-definition-digest", definition_json),
        )
        connection.exec_driver_sql(
            "INSERT INTO workflow_runs "
            "(run_id, definition_id, definition_version, definition_digest, state, revision) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-run",
                "legacy-definition",
                1,
                "legacy-definition-digest",
                "waiting_approval",
                1,
            ),
        )
        connection.exec_driver_sql(
            "INSERT INTO step_runs (run_id, step_id, state, revision) VALUES (?, ?, ?, ?)",
            ("legacy-run", "inspect", "ready", 1),
        )
        connection.exec_driver_sql(
            "INSERT INTO approval_requests "
            "(run_id, approval_key, record_module, record_qualname, payload_json, decision) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "legacy-run",
                "legacy-request",
                ApprovalRequest.__module__,
                ApprovalRequest.__qualname__,
                payload_json,
                None,
            ),
        )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sqlite.create_sqlite_engine(database_path)
    with sqlite.SQLiteUnitOfWork(engine) as uow:
        assert uow.approvals.get("legacy-run", "legacy-request") == request

    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT version, binding_digest, status, decision "
            "FROM approval_requests WHERE run_id = ? AND approval_key = ?",
            ("legacy-run", "legacy-request"),
        ).one()
        assert row == (0, "legacy-binding-digest", "pending", None)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE approval_requests SET status = 'corrupt' "
                "WHERE run_id = 'legacy-run' AND approval_key = 'legacy-request'"
            )

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE approval_requests SET version = 1, status = 'approved', "
            "decision = 'approved' WHERE run_id = 'legacy-run' "
            "AND approval_key = 'legacy-request'"
        )
    with pytest.raises(PersistenceError, match="structured approval columns"):
        sqlite.SQLiteUnitOfWork(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE approval_requests SET version = 0, status = 'pending', decision = NULL "
            "WHERE run_id = 'legacy-run' AND approval_key = 'legacy-request'"
        )

    duplicate_payload = json.loads(payload_json)
    duplicate_payload["id"] = "second-request"
    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "INSERT INTO approval_requests "
                "(run_id, approval_key, record_module, record_qualname, payload_json, "
                "version, binding_digest, status, decision) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "legacy-run",
                    "second-request",
                    ApprovalRequest.__module__,
                    ApprovalRequest.__qualname__,
                    json.dumps(duplicate_payload, separators=(",", ":"), sort_keys=True),
                    0,
                    "second-binding-digest",
                    "pending",
                    None,
                ),
            )
    engine.dispose()

    command.downgrade(config, "0001_workflow_runtime")
    engine = sqlite.create_sqlite_engine(database_path)
    with engine.connect() as connection:
        assert (
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar_one()
            == "0001_workflow_runtime"
        )
        columns = {
            row[1] for row in connection.exec_driver_sql("PRAGMA table_info('approval_requests')")
        }
        assert {"version", "binding_digest", "status"}.isdisjoint(columns)
        legacy_row = connection.exec_driver_sql(
            "SELECT run_id, approval_key, payload_json, decision FROM approval_requests"
        ).one()
        assert legacy_row == (
            "legacy-run",
            "legacy-request",
            payload_json,
            None,
        )
        assert _sqlite_object_sql(
            connection,
            object_type="index",
            name="ix_approval_requests_pending_unique",
        ) == (
            "CREATE UNIQUE INDEX ix_approval_requests_pending_unique "
            "ON approval_requests (run_id, approval_key) WHERE decision IS NULL"
        )
    engine.dispose()
