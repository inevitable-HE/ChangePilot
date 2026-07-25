from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from changepilot.sandbox.adapters.sqlite import SQLiteSandboxDatabase
from changepilot.sandbox.adapters.tools import build_sandbox_tool_suite
from changepilot.sandbox.application.manager import SandboxManager
from changepilot.sandbox.application.orders import OrderService
from changepilot.sandbox.domain.models import (
    ActionOutput,
    EnvironmentOutput,
    MigrationArguments,
    OrderRecord,
    SandboxArguments,
    SchemaVersion,
    ServiceVersion,
    ValidationOutput,
)
from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.ports.tools import (
    RecoveryQuery,
    ToolExecutionContext,
    ToolExecutionPhase,
    ToolProbeStatus,
    ToolRisk,
)


def _context(
    step_id: str,
    *,
    key: str | None = None,
) -> ToolExecutionContext:
    return ToolExecutionContext(
        run_id="run-1",
        step_id=step_id,
        attempt_number=1,
        phase=ToolExecutionPhase.FORWARD,
        logical_idempotency_key=key or f"run-1:{step_id}",
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
        metadata={"arguments": {"sandbox_id": "demo"}},
    )


def _query(step_id: str, key: str) -> RecoveryQuery:
    return RecoveryQuery(
        run_id="run-1",
        step_id=step_id,
        phase=ToolExecutionPhase.PROBE,
        logical_idempotency_key=key,
        deadline=datetime.now(timezone.utc) + timedelta(minutes=1),
        metadata={"arguments": {"sandbox_id": "demo"}},
    )


def _sandbox(tmp_path: Path):
    manager = SandboxManager(tmp_path / "sandboxes")
    initial = manager.create("demo")
    suite = build_sandbox_tool_suite(manager)
    return manager, initial, suite


def test_suite_registers_versioned_tools_risks_and_compensations(
    tmp_path: Path,
) -> None:
    _, _, suite = _sandbox(tmp_path)

    for name in (
        "service.inspect",
        "schema.inspect",
        "upgrade.precheck",
        "schema.migrate",
        "service.deploy-v2",
        "service.health-check",
        "service.smoke-test",
        "service.restore",
        "schema.rollback",
    ):
        assert suite.registry.contains(name, "1.0.0")
    assert (
        suite.registry.descriptor_for("schema.migrate", "1.0.0").risk
        is ToolRisk.HIGH
    )
    assert suite.compensations == {
        "schema.migrate": "schema.rollback",
        "service.deploy-v2": "service.restore",
    }


def test_environment_inspection_is_read_only_and_auditable(
    tmp_path: Path,
) -> None:
    _, initial, suite = _sandbox(tmp_path)
    tool = suite.registry.resolve("service.inspect", "1.0.0")

    result = tool.execute(
        _context("inspect"),
        SandboxArguments(sandbox_id="demo"),
    )

    assert result.effect_applied is False
    assert isinstance(result.output, EnvironmentOutput)
    assert result.output.state == initial
    assert len(result.output.evidence_digest) == 64
    assert "service=v1" in result.output.summary


def test_schema_migration_is_idempotent_and_probeable(tmp_path: Path) -> None:
    manager, initial, suite = _sandbox(tmp_path)
    tool = suite.registry.resolve("schema.migrate", "1.0.0")
    arguments = MigrationArguments(
        sandbox_id="demo",
        expected_fingerprint=initial.database_fingerprint,
    )
    context = _context("migrate", key="migration-key")

    first = tool.execute(context, arguments)
    second = tool.execute(context, arguments)
    probe = tool.probe(_query("migrate", "migration-key"))

    assert first.effect_applied is True
    assert second.effect_applied is False
    assert isinstance(second.output, ActionOutput)
    assert second.output.already_applied is True
    assert manager.inspect("demo").schema_version is SchemaVersion.V2
    assert probe.status is ToolProbeStatus.FOUND


def test_migration_precondition_failure_is_permanent(tmp_path: Path) -> None:
    _, _, suite = _sandbox(tmp_path)
    tool = suite.registry.resolve("schema.migrate", "1.0.0")

    with pytest.raises(DomainError) as raised:
        tool.execute(
            _context("migrate"),
            MigrationArguments(
                sandbox_id="demo",
                expected_fingerprint="wrong",
            ),
        )

    assert raised.value.error_class is ErrorClass.PERMANENT


def test_deploy_requires_compatible_schema_and_is_probeable(
    tmp_path: Path,
) -> None:
    manager, initial, suite = _sandbox(tmp_path)
    deploy = suite.registry.resolve("service.deploy-v2", "1.0.0")
    arguments = SandboxArguments(sandbox_id="demo")

    with pytest.raises(DomainError, match="requires schema v2"):
        deploy.execute(_context("deploy"), arguments)

    suite.registry.resolve("schema.migrate", "1.0.0").execute(
        _context("migrate"),
        MigrationArguments(
            sandbox_id="demo",
            expected_fingerprint=initial.database_fingerprint,
        ),
    )
    context = _context("deploy", key="deploy-key")
    deployed = deploy.execute(context, arguments)

    assert deployed.effect_applied is True
    assert manager.inspect("demo").service_version is ServiceVersion.V2
    assert deploy.probe(_query("deploy", "deploy-key")).status is ToolProbeStatus.FOUND


def test_health_and_smoke_test_return_machine_readable_evidence(
    tmp_path: Path,
) -> None:
    _, initial, suite = _sandbox(tmp_path)
    suite.registry.resolve("schema.migrate", "1.0.0").execute(
        _context("migrate"),
        MigrationArguments(
            sandbox_id="demo",
            expected_fingerprint=initial.database_fingerprint,
        ),
    )
    suite.registry.resolve("service.deploy-v2", "1.0.0").execute(
        _context("deploy"),
        SandboxArguments(sandbox_id="demo"),
    )

    health = suite.registry.resolve(
        "service.health-check",
        "1.0.0",
    ).execute(_context("health"), SandboxArguments(sandbox_id="demo"))
    smoke = suite.registry.resolve(
        "service.smoke-test",
        "1.0.0",
    ).execute(_context("smoke"), SandboxArguments(sandbox_id="demo"))

    assert isinstance(health.output, ValidationOutput)
    assert health.output.success is True
    assert smoke.output.checks == (
        "service_database_compatible",
        "orders_readable",
        "versioned_contract_valid",
    )


def test_compensation_restores_service_then_safely_rolls_back_schema(
    tmp_path: Path,
) -> None:
    manager, initial, suite = _sandbox(tmp_path)
    arguments = SandboxArguments(sandbox_id="demo")
    suite.registry.resolve("schema.migrate", "1.0.0").execute(
        _context("migrate"),
        MigrationArguments(
            sandbox_id="demo",
            expected_fingerprint=initial.database_fingerprint,
        ),
    )
    suite.registry.resolve("service.deploy-v2", "1.0.0").execute(
        _context("deploy"),
        arguments,
    )

    suite.registry.resolve("service.restore", "1.0.0").execute(
        _context("restore"),
        arguments,
    )
    rollback = suite.registry.resolve("schema.rollback", "1.0.0").execute(
        _context("rollback"),
        MigrationArguments(
            sandbox_id="demo",
            expected_fingerprint=initial.database_fingerprint,
        ),
    )

    state = manager.inspect("demo")
    assert rollback.effect_applied is True
    assert state.service_version is ServiceVersion.V1
    assert state.schema_version is SchemaVersion.V1


def test_schema_rollback_rejects_v2_only_data(tmp_path: Path) -> None:
    manager, initial, suite = _sandbox(tmp_path)
    arguments = SandboxArguments(sandbox_id="demo")
    suite.registry.resolve("schema.migrate", "1.0.0").execute(
        _context("migrate"),
        MigrationArguments(
            sandbox_id="demo",
            expected_fingerprint=initial.database_fingerprint,
        ),
    )
    suite.registry.resolve("service.deploy-v2", "1.0.0").execute(
        _context("deploy"),
        arguments,
    )
    OrderService(manager, "demo").create_order(
        OrderRecord(
            order_id="order-urgent",
            customer="dora",
            total_cents=5000,
            priority="urgent",
        )
    )
    suite.registry.resolve("service.restore", "1.0.0").execute(
        _context("restore"),
        arguments,
    )

    with pytest.raises(DomainError, match="discard"):
        suite.registry.resolve("schema.rollback", "1.0.0").execute(
            _context("rollback"),
            MigrationArguments(
                sandbox_id="demo",
                expected_fingerprint=initial.database_fingerprint,
            ),
        )

    assert manager.inspect("demo").schema_version is SchemaVersion.V2


def test_registry_rejects_arbitrary_path_argument(tmp_path: Path) -> None:
    _, _, suite = _sandbox(tmp_path)

    with pytest.raises(ValueError, match="undeclared fields"):
        suite.registry.validate_arguments(
            "service.inspect",
            "1.0.0",
            {"sandbox_id": "demo", "path": "C:/production.db"},
        )
