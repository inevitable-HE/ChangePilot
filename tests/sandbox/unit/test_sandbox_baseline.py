from __future__ import annotations

from pathlib import Path

import pytest

from changepilot.sandbox.adapters.sqlite import (
    SandboxDatabaseError,
    SQLiteSandboxDatabase,
)
from changepilot.sandbox.application.manager import (
    SandboxBoundaryError,
    SandboxManager,
)
from changepilot.sandbox.application.orders import OrderService
from changepilot.sandbox.domain.models import (
    OrderRecord,
    SchemaVersion,
    ServiceVersion,
)


def test_create_produces_known_v1_fixture(tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")

    state = manager.create("demo-1")
    orders = OrderService(manager, "demo-1").list_orders()

    assert state.service_version is ServiceVersion.V1
    assert state.schema_version is SchemaVersion.V1
    assert state.migration_ids == ()
    assert [(item.order_id, item.customer, item.total_cents) for item in orders] == [
        ("order-1001", "alice", 1299),
        ("order-1002", "bob", 2599),
    ]
    assert all(item.priority is None for item in orders)


def test_sandboxes_are_isolated_and_reset_is_deterministic(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    first = manager.create("first")
    second = manager.create("second")
    service = OrderService(manager, "first")
    service.create_order(
        OrderRecord(
            order_id="order-extra",
            customer="carol",
            total_cents=999,
        )
    )

    assert len(service.list_orders()) == 3
    assert len(OrderService(manager, "second").list_orders()) == 2

    reset = manager.reset("first")

    assert len(OrderService(manager, "first").list_orders()) == 2
    assert reset.database_fingerprint == first.database_fingerprint
    assert reset.database_fingerprint == second.database_fingerprint


@pytest.mark.parametrize(
    "value",
    ("../outside.db", "..\\outside.db"),
)
def test_resource_path_cannot_escape_sandbox(
    tmp_path: Path,
    value: str,
) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    manager.create("demo")

    with pytest.raises(SandboxBoundaryError, match="escapes"):
        manager.resolve_resource("demo", value)

    with pytest.raises(SandboxBoundaryError, match="absolute"):
        manager.resolve_resource("demo", tmp_path / "outside.db")


@pytest.mark.parametrize(
    "value",
    ("/etc/outside.db", "C:\\outside.db", "\\\\server\\share\\outside.db"),
)
def test_resource_path_rejects_cross_platform_absolute_paths(
    tmp_path: Path,
    value: str,
) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    manager.create("demo")

    with pytest.raises(SandboxBoundaryError, match="absolute"):
        manager.resolve_resource("demo", value)


def test_v1_to_v2_migration_uses_ledger_and_fingerprint(
    tmp_path: Path,
) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    initial = manager.create("demo")
    database = SQLiteSandboxDatabase()

    migrated = database.migrate_v1_to_v2(
        manager.database_path("demo"),
        idempotency_key="run-1:migrate",
        expected_fingerprint=initial.database_fingerprint,
    )
    repeated = database.migrate_v1_to_v2(
        manager.database_path("demo"),
        idempotency_key="run-1:migrate",
        expected_fingerprint="ignored-on-replay",
    )

    assert migrated.already_applied is False
    assert repeated.already_applied is True
    assert repeated.database_fingerprint == migrated.database_fingerprint
    assert manager.inspect("demo").migration_ids == ("orders-v1-to-v2",)


def test_migration_rejects_wrong_database_fingerprint(tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    manager.create("demo")

    with pytest.raises(SandboxDatabaseError, match="fingerprint"):
        SQLiteSandboxDatabase().migrate_v1_to_v2(
            manager.database_path("demo"),
            idempotency_key="run-1:migrate",
            expected_fingerprint="wrong",
        )

    assert manager.inspect("demo").schema_version is SchemaVersion.V1


def test_service_v2_requires_schema_v2(tmp_path: Path) -> None:
    manager = SandboxManager(tmp_path / "sandboxes")
    manager.create("demo")
    manager.set_service_version("demo", ServiceVersion.V2)

    health = OrderService(manager, "demo").health()

    assert health.healthy is False
    assert "requires schema v2" in health.detail
