from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from pathlib import Path
from threading import Lock

from pydantic import BaseModel, ConfigDict

from changepilot.workflow.adapters.persistence.schema import metadata
from changepilot.workflow.adapters.persistence.sqlite import (
    SQLiteUnitOfWork,
    create_sqlite_engine,
)
from changepilot.workflow.application.approvals import ApprovalService
from changepilot.workflow.application.coordinator import Coordinator
from changepilot.workflow.application.recovery import RecoveryService
from changepilot.workflow.application.services import (
    QueryService,
    WorkflowDriver,
    WorkflowService,
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
from tests.support.fakes import DeterministicIdentifiers, FakeClock


class OrderUpgradeArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: str


class OrderUpgradeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    value: str


class OrderUpgradeTool:
    def __init__(
        self,
        name: str,
        calls: list[str],
        *,
        risk: ToolRisk = ToolRisk.LOW,
        outcomes: tuple[str, ...] = ("success",),
        effect_applied: bool = False,
    ) -> None:
        self.descriptor = ToolDescriptor(
            name=name,
            version="1.0.0",
            input_model=OrderUpgradeArguments,
            output_model=OrderUpgradeOutput,
            risk=risk,
            idempotency=ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=30,
        )
        self._calls = calls
        self._outcomes = deque(outcomes)
        self._effect_applied = effect_applied
        self._lock = Lock()
        self.contexts: list[ToolExecutionContext] = []

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: OrderUpgradeArguments,
    ) -> ToolExecutionResult:
        with self._lock:
            self.contexts.append(context)
            self._calls.append(self.descriptor.name)
            outcome = self._outcomes.popleft() if self._outcomes else "success"
        if outcome != "success":
            raise DomainError(
                f"scripted {outcome}",
                error_class=ErrorClass(outcome),
            )
        return ToolExecutionResult(
            output=OrderUpgradeOutput(value=arguments.value),
            effect_applied=self._effect_applied,
        )

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        return ToolProbeResult(status=ToolProbeStatus.INCONCLUSIVE)


def order_upgrade_definition() -> dict[str, object]:
    def step(
        step_id: str,
        tool: str,
        *,
        depends_on: list[str] | None = None,
        risk: str = "low",
        compensation_tool: str | None = None,
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "id": step_id,
            "tool": {"name": tool, "version": "1.0.0"},
            "arguments": {"value": step_id},
            "depends_on": depends_on or [],
            "risk": risk,
            "retry": {"max_attempts": 1, "initial_backoff_seconds": 1.0},
        }
        if compensation_tool is not None:
            item["compensation_tool"] = {
                "name": compensation_tool,
                "version": "1.0.0",
            }
        return item

    return {
        "definition_id": "order-service-upgrade",
        "version": 1,
        "steps": [
            step("inspect-service", "service.inspect"),
            step("inspect-db", "schema.inspect"),
            step(
                "precheck",
                "upgrade.precheck",
                depends_on=["inspect-service", "inspect-db"],
            ),
            step(
                "migrate-schema",
                "schema.migrate",
                depends_on=["precheck"],
                risk="high",
                compensation_tool="schema.rollback",
            ),
            step(
                "deploy-v2",
                "service.deploy-v2",
                depends_on=["migrate-schema"],
                compensation_tool="service.restore",
            ),
            step(
                "health-check",
                "service.health-check",
                depends_on=["deploy-v2"],
            ),
            step(
                "smoke-test",
                "service.smoke-test",
                depends_on=["health-check"],
            ),
        ],
    }


@dataclass(slots=True)
class OrderUpgradeRuntime:
    database_path: Path
    engine: object
    calls: list[str]
    tools: dict[str, OrderUpgradeTool]
    workflow: WorkflowService
    approvals: ApprovalService
    query: QueryService
    recovery: RecoveryService
    coordinator: Coordinator
    driver: WorkflowDriver
    selected_run: list[str | None]

    def close(self) -> None:
        self.coordinator.close(wait=True)
        self.engine.dispose()


def make_order_upgrade_runtime(
    database_path: Path,
    *,
    health_outcomes: tuple[str, ...] = ("success",),
) -> OrderUpgradeRuntime:
    engine = create_sqlite_engine(database_path)
    metadata.create_all(engine)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    calls: list[str] = []
    tools = {
        "service.inspect": OrderUpgradeTool("service.inspect", calls),
        "schema.inspect": OrderUpgradeTool("schema.inspect", calls),
        "upgrade.precheck": OrderUpgradeTool("upgrade.precheck", calls),
        "schema.migrate": OrderUpgradeTool(
            "schema.migrate",
            calls,
            risk=ToolRisk.HIGH,
            effect_applied=True,
        ),
        "service.deploy-v2": OrderUpgradeTool(
            "service.deploy-v2",
            calls,
            effect_applied=True,
        ),
        "service.health-check": OrderUpgradeTool(
            "service.health-check",
            calls,
            outcomes=health_outcomes,
        ),
        "service.smoke-test": OrderUpgradeTool("service.smoke-test", calls),
        "service.restore": OrderUpgradeTool("service.restore", calls),
        "schema.rollback": OrderUpgradeTool("schema.rollback", calls),
    }
    registry = ToolRegistry(tools.values())
    clock = FakeClock()
    identifiers = DeterministicIdentifiers()
    selected_run: list[str | None] = [None]
    recovery = RecoveryService(uow_factory=uow_factory, tools=registry, clock=clock)
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=identifiers,
        run_id_selector=lambda: selected_run[0],
        concurrency=1,
    )
    return OrderUpgradeRuntime(
        database_path=database_path,
        engine=engine,
        calls=calls,
        tools=tools,
        workflow=WorkflowService(
            uow_factory=uow_factory,
            tools=registry,
            clock=clock,
            identifiers=identifiers,
        ),
        approvals=ApprovalService(uow_factory=uow_factory, clock=clock),
        query=QueryService(uow_factory=uow_factory, tools=registry),
        recovery=recovery,
        coordinator=coordinator,
        driver=WorkflowDriver(recovery=recovery, coordinator=coordinator),
        selected_run=selected_run,
    )


def drive_until_state(
    runtime: OrderUpgradeRuntime,
    expected_state: str,
    *,
    limit: int = 100,
) -> None:
    for _ in range(limit):
        if runtime.query.get_run(runtime.selected_run[0]).state == expected_state:
            return
        runtime.driver.run_once()
    raise AssertionError(f"run did not reach {expected_state!r} within {limit} cycles")
