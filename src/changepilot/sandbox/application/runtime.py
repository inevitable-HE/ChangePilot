from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from changepilot.sandbox.adapters.tools import (
    SandboxToolSuite,
    build_sandbox_tool_suite,
)
from changepilot.sandbox.application.faults import (
    DeterministicFaultInjector,
)
from changepilot.sandbox.application.manager import SandboxManager
from changepilot.sandbox.domain.faults import FaultSpec
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


class _SystemClock:
    def now(self) -> str:
        return datetime.now(timezone.utc).isoformat()


class _UUIDIdentifiers:
    def new(self) -> str:
        return str(uuid4())


@dataclass(slots=True)
class SandboxWorkflowRuntime:
    root: Path
    sandbox_id: str
    manager: SandboxManager
    faults: DeterministicFaultInjector
    tools: SandboxToolSuite
    engine: object
    workflow: WorkflowService
    approvals: ApprovalService
    query: QueryService
    recovery: RecoveryService
    coordinator: Coordinator
    driver: WorkflowDriver
    selected_run: list[str | None]

    def create_upgrade_run(self) -> str:
        state = self.manager.inspect(self.sandbox_id)
        run_id = self.workflow.create_run(
            order_upgrade_sandbox_definition(
                self.sandbox_id,
                state.database_fingerprint,
            )
        )
        self.selected_run[0] = run_id
        return run_id

    def approve_pending(self) -> None:
        view = self.query.get_run(self.selected_run[0])
        request = view.pending_approval
        if request is None:
            raise RuntimeError("run has no pending approval")
        self.approvals.decide(
            view.run_id,
            request.request_id,
            "approved",
            expected_version=request.version,
            binding_digest=request.binding_digest,
            actor="local-sandbox-operator",
            reason="approved for isolated sandbox execution",
        )

    def run_until(self, expected_state: str, *, limit: int = 400) -> None:
        for _ in range(limit):
            if self.query.get_run(self.selected_run[0]).state == expected_state:
                return
            self.driver.run_once()
            time.sleep(0.001)
        raise AssertionError(
            f"run did not reach {expected_state!r} within {limit} cycles"
        )

    def run_until_step(
        self,
        step_id: str,
        expected_state: str,
        *,
        limit: int = 400,
    ) -> None:
        for _ in range(limit):
            view = self.query.get_run(self.selected_run[0])
            step = next(item for item in view.steps if item.step_id == step_id)
            if step.state == expected_state:
                return
            self.driver.run_once()
            time.sleep(0.001)
        raise AssertionError(
            f"step {step_id!r} did not reach {expected_state!r}"
        )

    def close(self) -> None:
        self.coordinator.close(wait=True)
        self.engine.dispose()


def make_sandbox_runtime(
    root: str | Path,
    *,
    sandbox_id: str = "order-demo",
    fault_specs: tuple[FaultSpec, ...] | None = (),
    selected_run_id: str | None = None,
) -> SandboxWorkflowRuntime:
    selected_root = Path(root).resolve()
    selected_root.mkdir(parents=True, exist_ok=True)
    manager = SandboxManager(selected_root / "sandboxes")
    if fault_specs is None:
        manager.inspect(sandbox_id)
    else:
        try:
            manager.inspect(sandbox_id)
        except FileNotFoundError:
            manager.create(sandbox_id)
        else:
            manager.reset(sandbox_id)
    injector = DeterministicFaultInjector(manager)
    if fault_specs is not None:
        injector.configure(sandbox_id, fault_specs)
    tools = build_sandbox_tool_suite(
        manager,
        fault_injector=injector,
    )
    engine = create_sqlite_engine(selected_root / "workflow.db")
    metadata.create_all(engine)
    uow_factory = lambda: SQLiteUnitOfWork(engine)
    clock = _SystemClock()
    identifiers = _UUIDIdentifiers()
    selected_run = [selected_run_id]
    recovery = RecoveryService(
        uow_factory=uow_factory,
        tools=tools.registry,
        clock=clock,
    )
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=tools.registry,
        clock=clock,
        identifiers=identifiers,
        run_id_selector=lambda: selected_run[0],
        concurrency=2,
    )
    return SandboxWorkflowRuntime(
        root=selected_root,
        sandbox_id=sandbox_id,
        manager=manager,
        faults=injector,
        tools=tools,
        engine=engine,
        workflow=WorkflowService(
            uow_factory=uow_factory,
            tools=tools.registry,
            clock=clock,
            identifiers=identifiers,
        ),
        approvals=ApprovalService(
            uow_factory=uow_factory,
            clock=clock,
        ),
        query=QueryService(
            uow_factory=uow_factory,
            tools=tools.registry,
        ),
        recovery=recovery,
        coordinator=coordinator,
        driver=WorkflowDriver(
            recovery=recovery,
            coordinator=coordinator,
        ),
        selected_run=selected_run,
    )


def order_upgrade_sandbox_definition(
    sandbox_id: str,
    expected_fingerprint: str,
) -> dict[str, object]:
    common = {"sandbox_id": sandbox_id}

    def step(
        step_id: str,
        tool: str,
        *,
        arguments: dict[str, object] | None = None,
        depends_on: list[str] | None = None,
        risk: str = "low",
        compensation_tool: str | None = None,
    ) -> dict[str, object]:
        item: dict[str, object] = {
            "id": step_id,
            "tool": {"name": tool, "version": "1.0.0"},
            "arguments": arguments or common,
            "depends_on": depends_on or [],
            "risk": risk,
            "retry": {
                "max_attempts": 1,
                "initial_backoff_seconds": 0.01,
            },
        }
        if compensation_tool is not None:
            item["compensation_tool"] = {
                "name": compensation_tool,
                "version": "1.0.0",
            }
        return item

    migration_arguments = {
        "sandbox_id": sandbox_id,
        "expected_fingerprint": expected_fingerprint,
    }
    return {
        "definition_id": f"order-sandbox-upgrade-{sandbox_id}",
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
                arguments=migration_arguments,
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
