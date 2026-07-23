from __future__ import annotations

import json
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from pydantic import BaseModel, ConfigDict
from sqlalchemy.exc import IntegrityError

from changepilot.workflow.application.approvals import (
    ApprovalDecisionError,
    ApprovalService,
)
from changepilot.workflow.application.coordinator import Coordinator
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.adapters.persistence.schema import metadata
from changepilot.workflow.ports.persistence import PersistenceError
from tests.support.fakes import (
    DeterministicIdentifiers,
    FakeClock,
    ScriptedTool,
)
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


LEGACY_APPROVAL_MODULE = "tests.contract.uow_contract"
LEGACY_APPROVAL_QUALNAME = "ApprovalRecord"


class _LegacyArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: str


def _legacy_80da0a0_payload(
    run_id: str,
    approval_key: str,
    payload: dict[str, object],
) -> str:
    # Frozen from ApprovalRecord and _record_payload at commit 80da0a0.
    return json.dumps(
        {
            "run_id": run_id,
            "approval_key": approval_key,
            "payload": payload,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


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


def _insert_current_approval(connection) -> None:
    connection.exec_driver_sql(
        "INSERT INTO workflow_definitions "
        "(definition_id, version, digest, definition_json) VALUES (?, ?, ?, ?)",
        ("definition", 1, "d" * 64, '{"definition_id":"definition","steps":[],"version":1}'),
    )
    connection.exec_driver_sql(
        "INSERT INTO workflow_runs "
        "(run_id, definition_id, definition_version, definition_digest, state, revision) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        ("run", "definition", 1, "d" * 64, "running", 0),
    )
    connection.exec_driver_sql(
        "INSERT INTO approval_requests "
        "(run_id, approval_key, record_module, record_qualname, payload_json, "
        "version, binding_digest, status, decision) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "run",
            "current",
            "tests.fixture",
            "ApprovalRecord",
            "{}",
            0,
            "a" * 64,
            "pending",
            None,
        ),
    )


@pytest.mark.parametrize("schema_source", ["create_all", "alembic"])
@pytest.mark.parametrize(
    "invalid_digest",
    [
        None,
        "",
        "a" * 63,
        "g" * 64,
        "+" + "a" * 63,
        "a_" + "b" * 62,
        " " + "a" * 62 + " ",
    ],
)
def test_current_approval_digest_constraints_reject_invalid_values(
    tmp_path: Path,
    schema_source: str,
    invalid_digest: str | None,
) -> None:
    sqlite = _load_module("changepilot.workflow.adapters.persistence.sqlite")
    database_path = tmp_path / f"approval-digest-{schema_source}.db"
    engine = sqlite.create_sqlite_engine(database_path)
    if schema_source == "create_all":
        metadata.create_all(engine)
    else:
        engine.dispose()
        command.upgrade(_alembic_config(database_path), "head")
        engine = sqlite.create_sqlite_engine(database_path)

    with engine.begin() as connection:
        _insert_current_approval(connection)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE approval_requests SET binding_digest = ? "
                "WHERE run_id = 'run' AND approval_key = 'current'",
                (invalid_digest,),
            )

    engine.dispose()


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
            == "0003_compensation_errors"
        )

        workflow_run_columns = {
            row[1]: row for row in connection.exec_driver_sql("PRAGMA table_info('workflow_runs')")
        }
        assert workflow_run_columns["last_event_sequence"][3] == 1
        assert workflow_run_columns["revision"][3] == 1
        assert workflow_run_columns["compensation_error"][3] == 0
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
        assert "binding_digest IS NOT NULL" in approval_table_sql
        assert "status = 'legacy' AND version = 0 AND binding_digest IS NULL" in approval_table_sql

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
    definition_json = json.dumps(
        {
            "definition_id": "legacy-definition",
            "version": 1,
            "steps": [
                {
                    "id": "protected",
                    "tool": {"name": "schema.apply", "version": "1.0.0"},
                    "arguments": {"target": "orders"},
                    "risk": "high",
                }
            ],
        },
        separators=(",", ":"),
        sort_keys=True,
    )
    legacy_rows = (
        (
            "legacy-run-a",
            "pending-a",
            LEGACY_APPROVAL_MODULE,
            LEGACY_APPROVAL_QUALNAME,
            _legacy_80da0a0_payload(
                "legacy-run-a",
                "pending-a",
                {"reviewers": ["ops"]},
            ),
            None,
        ),
        (
            "legacy-run-a",
            "pending-b",
            LEGACY_APPROVAL_MODULE,
            LEGACY_APPROVAL_QUALNAME,
            _legacy_80da0a0_payload(
                "legacy-run-a",
                "pending-b",
                {"reviewers": ["legal"]},
            ),
            None,
        ),
        (
            "legacy-run-a",
            "decided",
            LEGACY_APPROVAL_MODULE,
            LEGACY_APPROVAL_QUALNAME,
            _legacy_80da0a0_payload(
                "legacy-run-a",
                "decided",
                {"reviewers": ["security"]},
            ),
            "approved",
        ),
        (
            "legacy-run-b",
            "shared-key",
            LEGACY_APPROVAL_MODULE,
            LEGACY_APPROVAL_QUALNAME,
            _legacy_80da0a0_payload(
                "legacy-run-b",
                "shared-key",
                {"reviewers": ["ops"]},
            ),
            None,
        ),
        (
            "legacy-run-c",
            "shared-key",
            LEGACY_APPROVAL_MODULE,
            LEGACY_APPROVAL_QUALNAME,
            _legacy_80da0a0_payload(
                "legacy-run-c",
                "shared-key",
                {"reviewers": ["ops"]},
            ),
            "rejected",
        ),
    )
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO workflow_definitions "
            "(definition_id, version, digest, definition_json) VALUES (?, ?, ?, ?)",
            ("legacy-definition", 1, "legacy-definition-digest", definition_json),
        )
        for run_id in ("legacy-run-a", "legacy-run-b", "legacy-run-c"):
            connection.exec_driver_sql(
                "INSERT INTO workflow_runs "
                "(run_id, definition_id, definition_version, definition_digest, state, revision) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (
                    run_id,
                    "legacy-definition",
                    1,
                    "legacy-definition-digest",
                    "running",
                    1,
                ),
            )
        connection.exec_driver_sql(
            "INSERT INTO step_runs (run_id, step_id, state, revision) VALUES (?, ?, ?, ?)",
            ("legacy-run-a", "protected", "ready", 1),
        )
        for row in legacy_rows:
            connection.exec_driver_sql(
                "INSERT INTO approval_requests "
                "(run_id, approval_key, record_module, record_qualname, payload_json, decision) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                row,
            )
    engine.dispose()

    command.upgrade(config, "head")
    engine = sqlite.create_sqlite_engine(database_path)
    with sqlite.SQLiteUnitOfWork(engine) as uow:
        records = uow.approvals.list("legacy-run-a")
        assert [record.approval_key for record in records] == [
            "decided",
            "pending-a",
            "pending-b",
        ]
        assert all(record.status == "legacy" for record in records)
        assert all(record.record_module == LEGACY_APPROVAL_MODULE for record in records)
        assert all(record.record_qualname == LEGACY_APPROVAL_QUALNAME for record in records)
        assert uow.approvals.pending("legacy-run-a") is None

    with engine.connect() as connection:
        structured_rows = connection.exec_driver_sql(
            "SELECT run_id, approval_key, version, binding_digest, status, decision "
            "FROM approval_requests ORDER BY run_id, approval_key"
        ).all()
        assert structured_rows == [
            (run_id, approval_key, 0, None, "legacy", decision)
            for run_id, approval_key, _module, _qualname, _payload, decision in sorted(
                legacy_rows,
                key=lambda row: (row[0], row[1]),
            )
        ]

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE approval_requests SET status = 'corrupt' "
                "WHERE run_id = 'legacy-run-a' AND approval_key = 'pending-a'"
            )

    clock = FakeClock()
    service = ApprovalService(
        uow_factory=lambda: sqlite.SQLiteUnitOfWork(engine),
        clock=clock,
    )
    with pytest.raises(ApprovalDecisionError, match="legacy_approval"):
        service.decide(
            "legacy-run-a",
            "pending-a",
            "approved",
            expected_version=0,
            binding_digest="not-a-current-binding",
            actor="ops-user",
            reason="legacy record",
        )

    tool = ScriptedTool("schema.apply", "1.0.0")
    tool.descriptor = tool.descriptor.model_copy(
        update={"input_model": _LegacyArguments}
    )
    coordinator = Coordinator(
        uow_factory=lambda: sqlite.SQLiteUnitOfWork(engine),
        tools=ToolRegistry([tool]),
        clock=clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: "legacy-run-a",
    )
    try:
        report = coordinator.run_once()
        assert report.blocked_reason == "approval_pending"
        assert tool.calls == []
        with sqlite.SQLiteUnitOfWork(engine) as uow:
            current_pending = uow.approvals.pending("legacy-run-a")
            assert current_pending is not None
            assert current_pending.status == "pending"
            assert len(uow.approvals.list("legacy-run-a")) == 4
    finally:
        coordinator.close(wait=True)

    with pytest.raises(IntegrityError):
        with engine.begin() as connection:
            connection.exec_driver_sql(
                "UPDATE approval_requests SET binding_digest = NULL "
                "WHERE run_id = ? AND approval_key = ?",
                ("legacy-run-a", current_pending.id),
            )

    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE approval_requests SET version = 1, status = 'approved', "
            "decision = 'approved' WHERE run_id = ? AND approval_key = ?",
            ("legacy-run-a", current_pending.id),
        )
    with pytest.raises(PersistenceError, match="structured approval columns"):
        sqlite.SQLiteUnitOfWork(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "UPDATE approval_requests SET version = 0, status = 'pending', "
            "decision = NULL WHERE run_id = ? AND approval_key = ?",
            ("legacy-run-a", current_pending.id),
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
        downgraded_rows = connection.exec_driver_sql(
            "SELECT run_id, approval_key, record_module, record_qualname, "
            "payload_json, decision FROM approval_requests "
            "WHERE approval_key IN ('pending-a', 'pending-b', 'decided', 'shared-key') "
            "ORDER BY run_id, approval_key"
        ).all()
        assert downgraded_rows == sorted(
            legacy_rows,
            key=lambda row: (row[0], row[1]),
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
