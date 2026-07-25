from __future__ import annotations

from changepilot.sandbox.adapters.sqlite import (
    SandboxDatabaseError,
    SQLiteSandboxDatabase,
)
from changepilot.sandbox.application.manager import SandboxManager
from changepilot.sandbox.domain.models import (
    OrderRecord,
    SchemaVersion,
    ServiceHealth,
    ServiceVersion,
)


class OrderService:
    def __init__(
        self,
        manager: SandboxManager,
        sandbox_id: str,
        *,
        database: SQLiteSandboxDatabase | None = None,
    ) -> None:
        self._manager = manager
        self._sandbox_id = sandbox_id
        self._database = database or SQLiteSandboxDatabase()

    def health(self) -> ServiceHealth:
        state = self._manager.inspect(self._sandbox_id)
        compatible = (
            state.service_version is ServiceVersion.V1
            and state.schema_version in {SchemaVersion.V1, SchemaVersion.V2}
        ) or (
            state.service_version is ServiceVersion.V2
            and state.schema_version is SchemaVersion.V2
        )
        return ServiceHealth(
            healthy=compatible,
            service_version=state.service_version,
            schema_version=state.schema_version,
            detail=(
                "service and database are compatible"
                if compatible
                else "service v2 requires schema v2"
            ),
        )

    def list_orders(self) -> tuple[OrderRecord, ...]:
        if not self.health().healthy:
            raise SandboxDatabaseError("service and database are incompatible")
        return self._database.list_orders(
            self._manager.database_path(self._sandbox_id)
        )

    def create_order(self, order: OrderRecord) -> OrderRecord:
        state = self._manager.inspect(self._sandbox_id)
        if state.service_version is ServiceVersion.V1 and order.priority is not None:
            raise SandboxDatabaseError("service v1 does not accept priority")
        if not self.health().healthy:
            raise SandboxDatabaseError("service and database are incompatible")
        self._database.insert_order(
            self._manager.database_path(self._sandbox_id),
            order,
        )
        return order
