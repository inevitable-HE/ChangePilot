from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from sqlalchemy import select

from changepilot.workflow.adapters.persistence.memory import MemoryStore, MemoryUnitOfWork
from changepilot.workflow.adapters.persistence.schema import (
    approval_requests,
    audit_events,
    metadata,
)
from changepilot.workflow.adapters.persistence.sqlite import (
    SQLiteUnitOfWork,
    create_sqlite_engine,
)
from changepilot.workflow.application.approvals import (
    ApprovalDecisionError,
    ApprovalRequest,
    ApprovalService,
    approval_binding_digest,
)
from changepilot.workflow.application.coordinator import Coordinator
from changepilot.workflow.application.tooling import ToolRegistry, redact
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.persistence import OptimisticLockError
from changepilot.workflow.ports.tools import ToolRisk
from tests.support.fakes import (
    BlockingTool,
    DeterministicIdentifiers,
    FailOnCommitUnitOfWorkFactory,
    FakeClock,
    ScriptedTool,
)


@dataclass
class Runtime:
    coordinator: Coordinator
    approvals: ApprovalService
    uow_factory: object
    raw_uow_factory: object
    clock: FakeClock
    registry: ToolRegistry
    tools: dict[str, ScriptedTool]
    store: object

    def run(self):
        with self.raw_uow_factory() as uow:
            return uow.runs.get("run-1")

    def step(self, step_id: str):
        with self.raw_uow_factory() as uow:
            return uow.steps.get("run-1", step_id)

    def pending(self):
        with self.raw_uow_factory() as uow:
            return uow.approvals.pending("run-1")

    def approval_records(self):
        with self.raw_uow_factory() as uow:
            return uow.approvals.list("run-1")

    def events(self):
        with self.raw_uow_factory() as uow:
            return tuple(entry.event for entry in uow.events.list("run-1"))


def _tool(
    name: str,
    *,
    risk: ToolRisk = ToolRisk.LOW,
    version: str = "1.0.0",
    sensitive_paths: tuple[str, ...] = (),
) -> ScriptedTool:
    tool = ScriptedTool(name, version)
    tool.descriptor = tool.descriptor.model_copy(
        update={"risk": risk, "sensitive_argument_paths": sensitive_paths}
    )
    return tool


def _make_runtime(
    *,
    steps: list[dict[str, object]],
    tools: list[ScriptedTool],
    sqlite_path: Path | None = None,
    existing_store: object | None = None,
    uow_factory_wrapper=None,
) -> Runtime:
    clock = FakeClock()
    registry = ToolRegistry(tools)
    tool_map = {tool.descriptor.name: tool for tool in tools}
    if sqlite_path is not None:
        engine = existing_store or create_sqlite_engine(sqlite_path)
        metadata.create_all(engine)
        raw_uow_factory = lambda: SQLiteUnitOfWork(engine)
        store = engine
    else:
        store = existing_store or MemoryStore()
        raw_uow_factory = lambda: MemoryUnitOfWork(store)

    with raw_uow_factory() as uow:
        existing = uow.runs.get("run-1")
    if existing is None:
        definition = WorkflowDefinition.from_mapping(
            {
                "definition_id": "workflow-1",
                "version": 1,
                "steps": steps,
            },
            registry,
        )
        run = WorkflowRun.new(
            run_id="run-1",
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_digest=definition.digest,
        )
        with raw_uow_factory() as uow:
            uow.definitions.add(definition)
            uow.runs.add(run)
            for step in definition.steps:
                state = StepState.READY if not step.depends_on else StepState.PENDING
                revision = 1 if state is StepState.READY else 0
                uow.steps.add(
                    StepRun(
                        run_id="run-1",
                        step_id=step.id,
                        state=state,
                        revision=revision,
                    )
                )
            uow.commit()

    uow_factory = (
        uow_factory_wrapper(raw_uow_factory)
        if uow_factory_wrapper is not None
        else raw_uow_factory
    )
    identifiers = DeterministicIdentifiers()
    coordinator = Coordinator(
        uow_factory=uow_factory,
        tools=registry,
        clock=clock,
        identifiers=identifiers,
        run_id_selector=lambda: "run-1",
        concurrency=4,
    )
    return Runtime(
        coordinator=coordinator,
        approvals=ApprovalService(uow_factory=uow_factory, clock=clock),
        uow_factory=uow_factory,
        raw_uow_factory=raw_uow_factory,
        clock=clock,
        registry=registry,
        tools=tool_map,
        store=store,
    )


def _protected_step(
    *,
    step_risk: str = "low",
    tool_version: str = "1.0.0",
    arguments: dict[str, object] | None = None,
    depends_on: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": "protected",
        "tool": {"name": "schema.apply", "version": tool_version},
        "arguments": arguments or {"value": "v2"},
        "risk": step_risk,
        "depends_on": depends_on or [],
    }


@pytest.mark.parametrize("risk_source", ["step", "tool"])
def test_any_high_risk_source_creates_barrier_without_calling_tool(
    risk_source: str,
) -> None:
    tool = _tool(
        "schema.apply",
        risk=ToolRisk.HIGH if risk_source == "tool" else ToolRisk.LOW,
    )
    runtime = _make_runtime(
        steps=[_protected_step(step_risk="high" if risk_source == "step" else "low")],
        tools=[tool],
    )
    try:
        report = runtime.coordinator.run_once()

        assert report.blocked_reason == "approval_pending"
        assert runtime.run().state is RunState.WAITING_APPROVAL
        assert runtime.pending().step_id == "protected"
        assert tool.calls == []
    finally:
        runtime.coordinator.close(wait=True)


def test_high_risk_barrier_drains_existing_future_before_becoming_waiting() -> None:
    slow = BlockingTool("inspect")
    fast = _tool("precheck")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "slow",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "slow"},
            },
            {
                "id": "precheck",
                "tool": {"name": "precheck", "version": "1.0.0"},
                "arguments": {"value": "ok"},
            },
            _protected_step(depends_on=["precheck"]),
        ],
        tools=[slow, fast, protected],
    )
    try:
        first = runtime.coordinator.run_once()
        assert slow.started.wait(timeout=1)
        assert fast.wait_for_calls(1)

        draining = runtime.coordinator.run_once()
        assert set(first.dispatched) == {"precheck", "slow"}
        assert draining.blocked_reason == "approval_draining"
        assert runtime.run().state is RunState.RUNNING
        assert runtime.pending() is None
        assert protected.calls == []

        slow.release.set()
        assert slow.finished.wait(timeout=1)
        blocked = runtime.coordinator.run_once()
        assert blocked.blocked_reason == "approval_pending"
        assert runtime.run().state is RunState.WAITING_APPROVAL
        assert runtime.pending() is not None
        assert protected.calls == []
    finally:
        slow.release.set()
        runtime.coordinator.close(wait=True)


def test_pending_request_survives_sqlite_restart_without_duplicate_or_execution(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "approval-restart.db"
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[_protected_step()],
        tools=[tool],
        sqlite_path=database_path,
    )
    runtime.coordinator.run_once()
    request = runtime.pending()
    runtime.coordinator.close(wait=True)

    restarted = _make_runtime(
        steps=[_protected_step()],
        tools=[tool],
        sqlite_path=database_path,
        existing_store=runtime.store,
    )
    try:
        report = restarted.coordinator.run_once()
        assert restarted.pending().id == request.id
        assert len(restarted.approval_records()) == 1
        assert report.blocked_reason == "run_not_forward"
        assert tool.calls == []
    finally:
        restarted.coordinator.close(wait=True)
        restarted.store.dispose()


def test_matching_approval_resumes_and_only_dispatches_bound_step() -> None:
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    sibling = _tool("inspect")
    runtime = _make_runtime(
        steps=[
            _protected_step(),
            {
                "id": "sibling",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "read-only"},
            },
        ],
        tools=[protected, sibling],
    )
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        decided = runtime.approvals.decide(
            request.id,
            "approved",
            expected_version=request.version,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="change window open",
        )

        report = runtime.coordinator.run_once()
        assert decided.status == "approved"
        assert report.dispatched == ("protected",)
        assert protected.wait_for_calls(1)
        assert sibling.calls == []
        runtime.coordinator.run_once()
        assert runtime.step("protected").state is StepState.SUCCEEDED
    finally:
        runtime.coordinator.close(wait=True)


def test_rejection_without_completed_side_effect_cancels_run() -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(steps=[_protected_step()], tools=[tool])
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()

        runtime.approvals.decide(
            request.id,
            "rejected",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="unsafe window",
        )

        assert runtime.run().state is RunState.CANCELLED
        assert tool.calls == []
    finally:
        runtime.coordinator.close(wait=True)


def test_rejection_after_compensable_success_enters_compensating() -> None:
    precheck = _tool("precheck")
    compensation = _tool("precheck.undo")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "precheck",
                "tool": {"name": "precheck", "version": "1.0.0"},
                "arguments": {"value": "applied"},
                "compensation_tool": {
                    "name": "precheck.undo",
                    "version": "1.0.0",
                },
            },
            _protected_step(depends_on=["precheck"]),
        ],
        tools=[precheck, compensation, protected],
    )
    try:
        runtime.coordinator.run_once()
        assert precheck.wait_for_calls(1)
        runtime.coordinator.run_once()
        request = runtime.pending()

        runtime.approvals.decide(
            request.id,
            "rejected",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="rollback requested",
        )

        assert runtime.run().state is RunState.COMPENSATING
        assert compensation.calls == []
        assert protected.calls == []
    finally:
        runtime.coordinator.close(wait=True)


def test_decision_rejects_stale_mismatched_and_duplicate_requests() -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(steps=[_protected_step()], tools=[tool])
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()

        with pytest.raises(OptimisticLockError):
            runtime.approvals.decide(
                request.id,
                "approved",
                expected_version=9,
                binding_digest=request.binding_digest,
                actor="ops-user",
                reason="stale browser",
            )
        with pytest.raises(ApprovalDecisionError, match="binding_mismatch"):
            runtime.approvals.decide(
                request.id,
                "approved",
                expected_version=0,
                binding_digest="wrong-binding",
                actor="ops-user",
                reason="wrong plan",
            )

        runtime.approvals.decide(
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved once",
        )
        with pytest.raises(ApprovalDecisionError, match="already_decided"):
            runtime.approvals.decide(
                request.id,
                "approved",
                expected_version=1,
                binding_digest=request.binding_digest,
                actor="ops-user",
                reason="duplicate click",
            )
    finally:
        runtime.coordinator.close(wait=True)


@pytest.mark.parametrize("changed_component", ["arguments", "tool_version", "definition"])
def test_changed_binding_invalidates_old_approval_and_creates_new_pending(
    changed_component: str,
) -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(steps=[_protected_step()], tools=[tool])
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        approved = runtime.approvals.decide(
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved old snapshot",
        )
        old_values = {
            "plan_digest": request.definition_digest,
            "step_id": request.step_id,
            "tool_name": request.tool_name,
            "tool_version": request.tool_version,
            "redacted_arguments": request.redacted_arguments,
        }
        if changed_component == "arguments":
            old_values["redacted_arguments"] = {"value": "v1"}
        elif changed_component == "tool_version":
            old_values["tool_version"] = "0.9.0"
        else:
            old_values["plan_digest"] = "old-definition-digest"
        stale = replace(
            approved,
            binding_digest=approval_binding_digest(**old_values),
            version=approved.version + 1,
        )
        with runtime.raw_uow_factory() as uow:
            uow.approvals.save(stale, expected_version=approved.version)
            uow.commit()

        report = runtime.coordinator.run_once()
        records = runtime.approval_records()
        assert report.blocked_reason == "approval_pending"
        assert len(records) == 2
        assert records[0].status == "invalidated"
        assert runtime.pending().binding_digest == request.binding_digest
        assert runtime.pending().id != request.id
        assert runtime.run().state is RunState.WAITING_APPROVAL
        assert tool.calls == []
    finally:
        runtime.coordinator.close(wait=True)


def test_sqlite_replaces_invalid_approval_with_new_pending_in_one_transaction(
    tmp_path: Path,
) -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[_protected_step()],
        tools=[tool],
        sqlite_path=tmp_path / "approval-invalidation.db",
    )
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        approved = runtime.approvals.decide(
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved old snapshot",
        )
        stale = replace(
            approved,
            binding_digest=approval_binding_digest(
                "old-definition-digest",
                request.step_id,
                request.tool_name,
                request.tool_version,
                request.redacted_arguments,
            ),
            version=approved.version + 1,
        )
        with runtime.raw_uow_factory() as uow:
            uow.approvals.save(stale, expected_version=approved.version)
            uow.commit()

        report = runtime.coordinator.run_once()

        assert report.blocked_reason == "approval_pending"
        assert runtime.pending().id != request.id
        assert [item.status for item in runtime.approval_records()] == [
            "invalidated",
            "pending",
        ]
    finally:
        runtime.coordinator.close(wait=True)
        runtime.store.dispose()


def test_approval_create_decide_and_invalidate_append_structured_events() -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(steps=[_protected_step()], tools=[tool])
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        approved = runtime.approvals.decide(
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved snapshot",
        )
        stale = replace(
            approved,
            binding_digest="stale-binding-digest",
            version=approved.version + 1,
        )
        with runtime.raw_uow_factory() as uow:
            uow.approvals.save(stale, expected_version=approved.version)
            uow.commit()

        runtime.coordinator.run_once()
        approval_events = [
            event for event in runtime.events() if event.event_type.startswith("approval.")
        ]

        assert [event.event_type for event in approval_events] == [
            "approval.created",
            "approval.approved",
            "approval.invalidated",
            "approval.created",
        ]
        assert approval_events[0].summary == {
            "request_id": request.id,
            "binding_digest": request.binding_digest,
            "status": "pending",
            "arguments": {"value": "v2"},
        }
        assert approval_events[1].summary["actor"] == "ops-user"
        assert approval_events[1].summary["reason"] == "approved snapshot"
        assert approval_events[2].summary["status"] == "invalidated"
        assert approval_events[3].summary["request_id"] != request.id
    finally:
        runtime.coordinator.close(wait=True)


def test_barrier_creation_rolls_back_request_state_and_event_together() -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[_protected_step()],
        tools=[tool],
        uow_factory_wrapper=lambda factory: FailOnCommitUnitOfWorkFactory(
            factory,
            fail_on=1,
        ),
    )
    try:
        with pytest.raises(RuntimeError, match="injected persistence failure"):
            runtime.coordinator.run_once()

        assert runtime.run().state is RunState.PENDING
        assert runtime.pending() is None
        assert not any(event.event_type.startswith("approval.") for event in runtime.events())
        assert tool.calls == []
    finally:
        runtime.coordinator.close(wait=True)


def test_sqlite_approval_and_audit_payloads_never_store_raw_sensitive_value(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "approval-secrets.db"
    secret = "prod-secret-key=raw-secret-value"
    tool = _tool(
        "schema.apply",
        risk=ToolRisk.HIGH,
        sensitive_paths=("value",),
    )
    runtime = _make_runtime(
        steps=[_protected_step(arguments={"value": secret})],
        tools=[tool],
        sqlite_path=database_path,
    )
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        assert request.redacted_arguments == {"value": "[REDACTED]"}
        runtime.approvals.decide(
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="emergency token=decision-secret-value",
        )

        with runtime.store.connect() as connection:
            stored = [
                row[0]
                for row in connection.execute(select(approval_requests.c.payload_json))
            ] + [
                row[0] for row in connection.execute(select(audit_events.c.payload_json))
            ]
        assert secret not in "\n".join(stored)
        assert "prod-secret-key" not in "\n".join(stored)
        assert "raw-secret-value" not in "\n".join(stored)
        assert "decision-secret-value" not in "\n".join(stored)
    finally:
        runtime.coordinator.close(wait=True)
        runtime.store.dispose()
