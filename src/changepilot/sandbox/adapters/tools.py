from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping

from pydantic import BaseModel

from changepilot.sandbox.adapters.sqlite import (
    SandboxDatabaseError,
    SQLiteSandboxDatabase,
)
from changepilot.sandbox.application.manager import (
    SandboxBoundaryError,
    SandboxManager,
)
from changepilot.sandbox.application.faults import (
    DeterministicFaultInjector,
    FaultInjectingTool,
)
from changepilot.sandbox.application.orders import OrderService
from changepilot.sandbox.domain.models import (
    ActionOutput,
    EnvironmentOutput,
    MigrationArguments,
    SandboxArguments,
    SandboxState,
    SchemaVersion,
    ServiceVersion,
    ValidationOutput,
)
from changepilot.workflow.application.tooling import ToolRegistry
from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.ports.tools import (
    RecoveryQuery,
    ToolDescriptor,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolIdempotency,
    ToolProbeResult,
    ToolProbeStatus,
    ToolRisk,
)


_VERSION = "1.0.0"


def _descriptor(
    name: str,
    input_model: type[BaseModel],
    output_model: type[BaseModel],
    *,
    risk: ToolRisk,
    idempotency: ToolIdempotency,
) -> ToolDescriptor:
    return ToolDescriptor(
        name=name,
        version=_VERSION,
        input_model=input_model,
        output_model=output_model,
        risk=risk,
        idempotency=idempotency,
        default_timeout_seconds=30,
        max_timeout_seconds=30,
    )


def _digest(state: SandboxState) -> str:
    encoded = json.dumps(
        state.model_dump(mode="json"),
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _permanent(exc: Exception) -> DomainError:
    return DomainError(str(exc), error_class=ErrorClass.PERMANENT)


class EnvironmentInspectTool:
    def __init__(self, name: str, manager: SandboxManager) -> None:
        self.descriptor = _descriptor(
            name,
            SandboxArguments,
            EnvironmentOutput,
            risk=ToolRisk.LOW,
            idempotency=ToolIdempotency.NONE,
        )
        self._manager = manager

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: SandboxArguments,
    ) -> ToolExecutionResult:
        try:
            state = self._manager.inspect(arguments.sandbox_id)
        except (SandboxBoundaryError, SandboxDatabaseError, OSError) as exc:
            raise _permanent(exc) from exc
        return ToolExecutionResult(
            output=EnvironmentOutput(
                state=state,
                summary=(
                    f"{state.sandbox_id}: service={state.service_version.value}, "
                    f"schema={state.schema_version.value}, "
                    f"migrations={len(state.migration_ids)}"
                ),
                evidence_digest=_digest(state),
            ),
            effect_applied=False,
        )

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)


class UpgradePrecheckTool(EnvironmentInspectTool):
    def __init__(self, manager: SandboxManager) -> None:
        super().__init__("upgrade.precheck", manager)

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: SandboxArguments,
    ) -> ToolExecutionResult:
        result = super().execute(context, arguments)
        output = EnvironmentOutput.model_validate(
            result.output.model_dump(mode="python")
        )
        state = output.state
        if (
            state.service_version is not ServiceVersion.V1
            or state.schema_version is not SchemaVersion.V1
        ):
            raise DomainError(
                "upgrade precheck requires service v1 and schema v1",
                error_class=ErrorClass.PERMANENT,
            )
        return result


class SchemaMigrateTool:
    descriptor = _descriptor(
        "schema.migrate",
        MigrationArguments,
        ActionOutput,
        risk=ToolRisk.HIGH,
        idempotency=ToolIdempotency.SUPPORTED,
    )

    def __init__(
        self,
        manager: SandboxManager,
        database: SQLiteSandboxDatabase,
    ) -> None:
        self._manager = manager
        self._database = database

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: MigrationArguments,
    ) -> ToolExecutionResult:
        try:
            before = self._manager.inspect(arguments.sandbox_id)
            migration = self._database.migrate_v1_to_v2(
                self._manager.database_path(arguments.sandbox_id),
                idempotency_key=context.logical_idempotency_key,
                expected_fingerprint=arguments.expected_fingerprint,
            )
            after = self._manager.inspect(arguments.sandbox_id)
        except (SandboxBoundaryError, SandboxDatabaseError, OSError) as exc:
            raise _permanent(exc) from exc
        return ToolExecutionResult(
            output=ActionOutput(
                sandbox_id=arguments.sandbox_id,
                action="schema.migrate",
                before=before,
                after=after,
                summary=(
                    "schema migration already applied"
                    if migration.already_applied
                    else "schema migrated from v1 to v2"
                ),
                already_applied=migration.already_applied,
            ),
            effect_applied=not migration.already_applied,
        )

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        sandbox_id = _sandbox_id_from_query(query)
        if sandbox_id is None:
            return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)
        try:
            migration = self._database.migration_for_key(
                self._manager.database_path(sandbox_id),
                query.logical_idempotency_key,
            )
            if migration is None or migration.to_version is not SchemaVersion.V2:
                return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)
            after = self._manager.inspect(sandbox_id)
        except Exception:
            return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)
        return ToolProbeResult(
            status=ToolProbeStatus.FOUND,
            output=ActionOutput(
                sandbox_id=sandbox_id,
                action="schema.migrate",
                before=None,
                after=after,
                summary="migration found in the sandbox ledger",
                already_applied=True,
            ),
        )


class _ServiceVersionTool:
    target_version: ServiceVersion

    def __init__(
        self,
        *,
        name: str,
        target_version: ServiceVersion,
        manager: SandboxManager,
        database: SQLiteSandboxDatabase,
    ) -> None:
        self.descriptor = _descriptor(
            name,
            SandboxArguments,
            ActionOutput,
            risk=ToolRisk.MEDIUM,
            idempotency=ToolIdempotency.SUPPORTED,
        )
        self.target_version = target_version
        self._manager = manager
        self._database = database

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: SandboxArguments,
    ) -> ToolExecutionResult:
        try:
            path = self._manager.database_path(arguments.sandbox_id)
            existing = self._database.effect_output(
                path,
                logical_idempotency_key=context.logical_idempotency_key,
                tool_name=self.descriptor.name,
            )
            if existing is not None:
                return ToolExecutionResult(
                    output=ActionOutput.model_validate_json(
                        json.dumps(existing)
                    ),
                    effect_applied=False,
                )
            before = self._manager.inspect(arguments.sandbox_id)
            if (
                self.target_version is ServiceVersion.V2
                and before.schema_version is not SchemaVersion.V2
            ):
                raise SandboxDatabaseError(
                    "service v2 requires schema v2"
                )
            after = self._manager.set_service_version(
                arguments.sandbox_id,
                self.target_version,
            )
            output = ActionOutput(
                sandbox_id=arguments.sandbox_id,
                action=self.descriptor.name,
                before=before,
                after=after,
                summary=(
                    f"service set to {self.target_version.value}"
                    if before.service_version is not self.target_version
                    else f"service already at {self.target_version.value}"
                ),
                already_applied=before.service_version is self.target_version,
            )
            self._database.record_effect(
                path,
                logical_idempotency_key=context.logical_idempotency_key,
                tool_name=self.descriptor.name,
                output=output.model_dump(mode="json"),
            )
        except (SandboxBoundaryError, SandboxDatabaseError, OSError) as exc:
            raise _permanent(exc) from exc
        return ToolExecutionResult(
            output=output,
            effect_applied=not output.already_applied,
        )

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        sandbox_id = _sandbox_id_from_query(query)
        if sandbox_id is None:
            return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)
        try:
            payload = self._database.effect_output(
                self._manager.database_path(sandbox_id),
                logical_idempotency_key=query.logical_idempotency_key,
                tool_name=self.descriptor.name,
            )
        except Exception:
            return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)
        if payload is None:
            return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)
        return ToolProbeResult(
            status=ToolProbeStatus.FOUND,
            output=ActionOutput.model_validate_json(json.dumps(payload)),
        )


class ValidationTool:
    def __init__(
        self,
        *,
        name: str,
        manager: SandboxManager,
        smoke_test: bool,
    ) -> None:
        self.descriptor = _descriptor(
            name,
            SandboxArguments,
            ValidationOutput,
            risk=ToolRisk.LOW,
            idempotency=ToolIdempotency.NONE,
        )
        self._manager = manager
        self._smoke_test = smoke_test

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: SandboxArguments,
    ) -> ToolExecutionResult:
        try:
            state = self._manager.inspect(arguments.sandbox_id)
            service = OrderService(self._manager, arguments.sandbox_id)
            health = service.health()
            checks = ["service_database_compatible"]
            if not health.healthy:
                raise SandboxDatabaseError(health.detail)
            if self._smoke_test:
                orders = service.list_orders()
                if not orders:
                    raise SandboxDatabaseError("order fixture is empty")
                if (
                    state.service_version is ServiceVersion.V2
                    and any(order.priority is None for order in orders)
                ):
                    raise SandboxDatabaseError(
                        "v2 order response lacks priority"
                    )
                checks.extend(("orders_readable", "versioned_contract_valid"))
        except (SandboxBoundaryError, SandboxDatabaseError, OSError) as exc:
            raise _permanent(exc) from exc
        return ToolExecutionResult(
            output=ValidationOutput(
                state=state,
                success=True,
                checks=tuple(checks),
                summary=(
                    "order smoke test passed"
                    if self._smoke_test
                    else "service health check passed"
                ),
            ),
            effect_applied=False,
        )

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)


class SchemaRollbackTool:
    descriptor = _descriptor(
        "schema.rollback",
        MigrationArguments,
        ActionOutput,
        risk=ToolRisk.HIGH,
        idempotency=ToolIdempotency.SUPPORTED,
    )

    def __init__(
        self,
        manager: SandboxManager,
        database: SQLiteSandboxDatabase,
    ) -> None:
        self._manager = manager
        self._database = database

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: MigrationArguments,
    ) -> ToolExecutionResult:
        try:
            before = self._manager.inspect(arguments.sandbox_id)
            if before.service_version is not ServiceVersion.V1:
                raise SandboxDatabaseError(
                    "schema rollback requires service v1"
                )
            migration = self._database.rollback_v2_to_v1(
                self._manager.database_path(arguments.sandbox_id),
                idempotency_key=context.logical_idempotency_key,
            )
            after = self._manager.inspect(arguments.sandbox_id)
        except (SandboxBoundaryError, SandboxDatabaseError, OSError) as exc:
            raise _permanent(exc) from exc
        return ToolExecutionResult(
            output=ActionOutput(
                sandbox_id=arguments.sandbox_id,
                action="schema.rollback",
                before=before,
                after=after,
                summary=(
                    "schema rollback already applied"
                    if migration.already_applied
                    else "schema rolled back from v2 to v1"
                ),
                already_applied=migration.already_applied,
            ),
            effect_applied=not migration.already_applied,
        )

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        sandbox_id = _sandbox_id_from_query(query)
        if sandbox_id is None:
            return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)
        try:
            migration = self._database.migration_for_key(
                self._manager.database_path(sandbox_id),
                query.logical_idempotency_key,
            )
            if migration is None or migration.to_version is not SchemaVersion.V1:
                return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND)
            state = self._manager.inspect(sandbox_id)
        except Exception:
            return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)
        return ToolProbeResult(
            status=ToolProbeStatus.FOUND,
            output=ActionOutput(
                sandbox_id=sandbox_id,
                action="schema.rollback",
                before=None,
                after=state,
                summary="rollback found in the sandbox ledger",
                already_applied=True,
            ),
        )


@dataclass(frozen=True, slots=True)
class SandboxToolSuite:
    registry: ToolRegistry
    compensations: Mapping[str, str]


def build_sandbox_tool_suite(
    manager: SandboxManager,
    *,
    fault_injector: DeterministicFaultInjector | None = None,
) -> SandboxToolSuite:
    database = SQLiteSandboxDatabase()
    tools = (
        EnvironmentInspectTool("service.inspect", manager),
        EnvironmentInspectTool("schema.inspect", manager),
        UpgradePrecheckTool(manager),
        SchemaMigrateTool(manager, database),
        _ServiceVersionTool(
            name="service.deploy-v2",
            target_version=ServiceVersion.V2,
            manager=manager,
            database=database,
        ),
        ValidationTool(
            name="service.health-check",
            manager=manager,
            smoke_test=False,
        ),
        ValidationTool(
            name="service.smoke-test",
            manager=manager,
            smoke_test=True,
        ),
        _ServiceVersionTool(
            name="service.restore",
            target_version=ServiceVersion.V1,
            manager=manager,
            database=database,
        ),
        SchemaRollbackTool(manager, database),
    )
    registered_tools = (
        tuple(
            FaultInjectingTool(tool, fault_injector)
            for tool in tools
        )
        if fault_injector is not None
        else tools
    )
    return SandboxToolSuite(
        registry=ToolRegistry(registered_tools),
        compensations=MappingProxyType(
            {
                "schema.migrate": "schema.rollback",
                "service.deploy-v2": "service.restore",
            }
        ),
    )


def _sandbox_id_from_query(query: RecoveryQuery) -> str | None:
    value = query.metadata.get("arguments")
    if not isinstance(value, Mapping):
        return None
    sandbox_id = value.get("sandbox_id")
    return sandbox_id if isinstance(sandbox_id, str) else None
