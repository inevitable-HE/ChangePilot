from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType

import pytest
from pydantic import BaseModel, ConfigDict
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
from changepilot.workflow.application.coordinator import Coordinator, StepAttempt
from changepilot.workflow.application.tooling import ToolRegistry, redact
from changepilot.workflow.domain.definitions import WorkflowDefinition
from changepilot.workflow.domain.runs import StepRun, WorkflowRun
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.persistence import OptimisticLockError
from changepilot.workflow.ports.tools import SecretRef, ToolRisk, UnavailableSecretRef
from tests.support.fakes import (
    BlockingTool,
    DeterministicIdentifiers,
    FailOnCommitUnitOfWorkFactory,
    FakeClock,
    ScriptedTool,
)


@dataclass
class Runtime:
    run_id: str
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
            return uow.runs.get(self.run_id)

    def step(self, step_id: str):
        with self.raw_uow_factory() as uow:
            return uow.steps.get(self.run_id, step_id)

    def pending(self):
        with self.raw_uow_factory() as uow:
            return uow.approvals.pending(self.run_id)

    def approval_records(self):
        with self.raw_uow_factory() as uow:
            return uow.approvals.list(self.run_id)

    def attempts(self, step_id: str):
        with self.raw_uow_factory() as uow:
            return uow.attempts.list(self.run_id, step_id=step_id)

    def events(self):
        with self.raw_uow_factory() as uow:
            return tuple(entry.event for entry in uow.events.list(self.run_id))


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


class _NoEffectTool(ScriptedTool):
    def execute(self, context, arguments):
        return super().execute(context, arguments).model_copy(
            update={"effect_applied": False}
        )


class _ProbeForbiddenTool(ScriptedTool):
    def probe(self, query):
        raise AssertionError("Task 7 must not probe during approval recovery gating")


class _SecretReferenceArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    credential: SecretRef


class _ProviderNameResource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    provider: str
    name: str


class _ProviderNameArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    resource: _ProviderNameResource


def _make_runtime(
    *,
    steps: list[dict[str, object]],
    tools: list[ScriptedTool],
    sqlite_path: Path | None = None,
    existing_store: object | None = None,
    uow_factory_wrapper=None,
    run_id: str = "run-1",
    definition_id: str = "workflow-1",
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
        existing = uow.runs.get(run_id)
    if existing is None:
        definition = WorkflowDefinition.from_mapping(
            {
                "definition_id": definition_id,
                "version": 1,
                "steps": steps,
            },
            registry,
        )
        run = WorkflowRun.new(
            run_id=run_id,
            definition_id=definition.definition_id,
            definition_version=definition.version,
            definition_digest=definition.digest,
        )
        with raw_uow_factory() as uow:
            if uow.definitions.get(definition.definition_id, definition.version) is None:
                uow.definitions.add(definition)
            uow.runs.add(run)
            for step in definition.steps:
                state = StepState.READY if not step.depends_on else StepState.PENDING
                revision = 1 if state is StepState.READY else 0
                uow.steps.add(
                    StepRun(
                        run_id=run_id,
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
        run_id_selector=lambda: run_id,
        concurrency=4,
    )
    return Runtime(
        run_id=run_id,
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


def _switch_current_definition(runtime: Runtime, changed_component: str) -> None:
    definition_id = "workflow-2" if changed_component == "definition" else "workflow-1"
    version = 1 if changed_component == "definition" else 2
    tool_version = "2.0.0" if changed_component == "tool_version" else "1.0.0"
    arguments = {"value": "v3"} if changed_component == "arguments" else {"value": "v2"}
    changed = WorkflowDefinition.from_mapping(
        {
            "definition_id": definition_id,
            "version": version,
            "steps": [
                _protected_step(
                    tool_version=tool_version,
                    arguments=arguments,
                )
            ],
        },
        runtime.registry,
    )
    with runtime.raw_uow_factory() as uow:
        run = uow.runs.get(runtime.run_id)
        uow.definitions.add(changed)
        uow.runs.save(
            replace(
                run,
                definition_id=changed.definition_id,
                definition_version=changed.version,
                definition_digest=changed.digest,
                revision=run.revision + 1,
            ),
            expected_revision=run.revision,
        )
        uow.commit()


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


def test_approval_snapshot_rejects_non_string_keys_before_redaction() -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(steps=[_protected_step()], tools=[tool])
    try:
        with runtime.raw_uow_factory() as uow:
            run = uow.runs.get(runtime.run_id)
            definition = uow.definitions.get(
                run.definition_id,
                run.definition_version,
            )
        step = replace(
            definition.steps[0],
            arguments=MappingProxyType({1: "integer", "1": "string"}),
        )

        with pytest.raises(ValueError, match="canonical JSON"):
            runtime.coordinator._approval_snapshot(definition, step)
    finally:
        runtime.coordinator.close(wait=True)


def test_definition_secret_reference_mapping_is_redacted_before_sqlite_approval_persistence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "secret-reference.db"
    secret_provider = "env"
    secret_name = "PROD_DATABASE_PASSWORD"
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    tool.descriptor = tool.descriptor.model_copy(
        update={"input_model": _SecretReferenceArguments}
    )
    runtime = _make_runtime(
        steps=[_protected_step(arguments={
            "credential": {"provider": secret_provider, "name": secret_name}
        })],
        tools=[tool],
        sqlite_path=database_path,
    )
    try:
        report = runtime.coordinator.run_once()
        request = runtime.pending()
        approval_event = next(
            event for event in runtime.events() if event.event_type == "approval.created"
        )

        assert report.blocked_reason == "approval_pending"
        assert request.redacted_arguments == {"credential": "[REDACTED]"}
        assert len(request.binding_digest) == 64
        assert secret_provider not in repr(request)
        assert secret_name not in repr(request)
        assert secret_provider not in repr(approval_event.summary)
        assert secret_name not in repr(approval_event.summary)
        assert tool.calls == []

        with runtime.store.connect() as connection:
            persisted_payloads = "\n".join(
                row[0]
                for row in connection.execute(
                    select(approval_requests.c.payload_json).union_all(
                        select(audit_events.c.payload_json)
                    )
                )
            )
        assert secret_provider not in persisted_payloads
        assert secret_name not in persisted_payloads

    finally:
        runtime.coordinator.close(wait=True)
        runtime.store.dispose()
        database_bytes = database_path.read_bytes()
        assert secret_provider.encode("utf-8") not in database_bytes
        assert secret_name.encode("utf-8") not in database_bytes

@pytest.mark.parametrize("sqlite_path", [None, "provider-name.db"])
def test_provider_name_business_object_round_trips_without_digest_collision(
    tmp_path: Path,
    sqlite_path: str | None,
) -> None:
    database_path = None if sqlite_path is None else tmp_path / sqlite_path
    tool = _tool("schema.apply")
    tool.descriptor = tool.descriptor.model_copy(
        update={"input_model": _ProviderNameArguments}
    )
    first = _make_runtime(
        steps=[_protected_step(arguments={
            "resource": {"provider": "catalog", "name": "orders"}
        })],
        tools=[tool],
        sqlite_path=database_path,
    )
    changed = WorkflowDefinition.from_mapping(
        {
            "definition_id": "workflow-1",
            "version": 1,
            "steps": [_protected_step(arguments={
                "resource": {"provider": "catalog", "name": "payments"}
            })],
        },
        first.registry,
    )
    try:
        with first.raw_uow_factory() as uow:
            persisted = uow.definitions.get("workflow-1", 1)

        assert persisted.digest != changed.digest
        assert dict(persisted.steps[0].arguments["resource"]) == {
            "provider": "catalog",
            "name": "orders",
        }
    finally:
        first.coordinator.close(wait=True)
        if database_path is not None:
            first.store.dispose()


def test_sqlite_reloaded_secret_reference_is_unavailable_and_cannot_execute(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "reloaded-secret-reference.db"
    secret_provider = "env"
    secret_name = "PROD_DATABASE_PASSWORD"
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    tool.descriptor = tool.descriptor.model_copy(
        update={"input_model": _SecretReferenceArguments}
    )
    runtime = _make_runtime(
        steps=[_protected_step(arguments={
            "credential": {"provider": secret_provider, "name": secret_name}
        })],
        tools=[tool],
        sqlite_path=database_path,
    )
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        runtime.approvals.decide(
            runtime.run_id,
            request.id,
            "approved",
            expected_version=request.version,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved",
        )
    finally:
        runtime.coordinator.close(wait=True)
        runtime.store.dispose()

    restarted_tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    restarted_tool.descriptor = restarted_tool.descriptor.model_copy(
        update={"input_model": _SecretReferenceArguments}
    )
    restarted = _make_runtime(
        steps=[],
        tools=[restarted_tool],
        sqlite_path=database_path,
    )
    try:
        with restarted.raw_uow_factory() as uow:
            definition = uow.definitions.get("workflow-1", 1)
        arguments = definition.steps[0].arguments

        assert isinstance(arguments["credential"], UnavailableSecretRef)
        with pytest.raises(ValueError, match="secret reference unavailable"):
            restarted.registry.coerce_arguments("schema.apply", "1.0.0", arguments)

        report = restarted.coordinator.run_once()
        assert report.blocked_reason == "internal_consistency"
        assert restarted_tool.calls == []
        assert restarted.attempts("protected") == ()
        database_bytes = database_path.read_bytes()
        assert secret_provider.encode("utf-8") not in database_bytes
        assert secret_name.encode("utf-8") not in database_bytes
    finally:
        restarted.coordinator.close(wait=True)
        restarted.store.dispose()


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


@pytest.mark.parametrize("persisted_fact", ["step_run", "step_attempt"])
def test_fresh_coordinator_requires_recovery_for_persisted_running_work(
    persisted_fact: str,
) -> None:
    store = MemoryStore()
    inspect = _ProbeForbiddenTool("inspect")
    protected = _ProbeForbiddenTool("schema.apply")
    protected.descriptor = protected.descriptor.model_copy(
        update={"risk": ToolRisk.HIGH}
    )
    seeded = _make_runtime(
        steps=[
            {
                "id": "slow",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "slow"},
            },
            _protected_step(),
        ],
        tools=[inspect, protected],
        existing_store=store,
    )
    with seeded.raw_uow_factory() as uow:
        run = uow.runs.get(seeded.run_id)
        running_run, run_event = run.transition(
            RunState.RUNNING,
            occurred_at=seeded.clock.now(),
        )
        uow.runs.save(running_run, expected_revision=run.revision)
        uow.events.append(run_event)
        slow_step = uow.steps.get(seeded.run_id, "slow")
        if persisted_fact == "step_run":
            uow.steps.save(
                replace(
                    slow_step,
                    state=StepState.RUNNING,
                    revision=slow_step.revision + 1,
                ),
                expected_revision=slow_step.revision,
            )
        else:
            uow.attempts.add(
                StepAttempt(
                    run_id=seeded.run_id,
                    step_id="slow",
                    attempt_no=1,
                    phase="forward",
                    attempt_id="attempt-before-restart",
                    status="running",
                    idempotency_key="logical-key",
                    started_at=seeded.clock.now(),
                )
            )
        uow.commit()
    seeded.coordinator.close(wait=True)

    restarted = _make_runtime(
        steps=[],
        tools=[inspect, protected],
        existing_store=store,
    )
    try:
        report = restarted.coordinator.run_once()

        assert report.blocked_reason == "approval_recovery_required"
        assert restarted.run().state is RunState.RUNNING
        assert restarted.pending() is None
        assert protected.calls == []
    finally:
        restarted.coordinator.close(wait=True)


@pytest.mark.parametrize("recovery_fact", ["step_state", "attempt_only"])
def test_sqlite_restart_recovery_blocks_independent_low_risk_dispatch(
    tmp_path: Path,
    recovery_fact: str,
) -> None:
    database_path = tmp_path / f"global-recovery-{recovery_fact}.db"
    uncertain = _ProbeForbiddenTool("inspect")
    ready = _ProbeForbiddenTool("safe.check")
    seeded = _make_runtime(
        steps=[
            {
                "id": "uncertain",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "uncertain"},
            },
            {
                "id": "independent-ready",
                "tool": {"name": "safe.check", "version": "1.0.0"},
                "arguments": {"value": "safe"},
            },
        ],
        tools=[uncertain, ready],
        sqlite_path=database_path,
    )
    with seeded.raw_uow_factory() as uow:
        run = uow.runs.get(seeded.run_id)
        running_run, run_event = run.transition(
            RunState.RUNNING,
            occurred_at=seeded.clock.now(),
        )
        uow.runs.save(running_run, expected_revision=run.revision)
        uow.events.append(run_event)
        uncertain_step = uow.steps.get(seeded.run_id, "uncertain")
        uow.steps.save(
            replace(
                uncertain_step,
                state=(
                    StepState.RESULT_UNKNOWN
                    if recovery_fact == "step_state"
                    else StepState.SUCCEEDED
                ),
                revision=uncertain_step.revision + 1,
            ),
            expected_revision=uncertain_step.revision,
        )
        if recovery_fact == "attempt_only":
            uow.attempts.add(
                StepAttempt(
                    run_id=seeded.run_id,
                    step_id="uncertain",
                    attempt_no=1,
                    phase="forward",
                    attempt_id="attempt-before-restart",
                    status="result_unknown",
                    idempotency_key="logical-key",
                    started_at=seeded.clock.now(),
                    completed_at=seeded.clock.now(),
                    error_class="result_unknown",
                )
            )
        uow.commit()
    seeded.coordinator.close(wait=True)
    seeded.store.dispose()

    restarted = _make_runtime(
        steps=[],
        tools=[uncertain, ready],
        sqlite_path=database_path,
    )
    try:
        with restarted.raw_uow_factory() as uow:
            attempts_before = len(uow.attempts.list(restarted.run_id))
            events_before = len(uow.events.list(restarted.run_id))

        report = restarted.coordinator.run_once()

        with restarted.raw_uow_factory() as uow:
            assert len(uow.attempts.list(restarted.run_id)) == attempts_before
            assert len(uow.events.list(restarted.run_id)) == events_before
        assert report.blocked_reason == "approval_recovery_required"
        assert restarted.run().state is RunState.RUNNING
        assert restarted.pending() is None
        assert uncertain.calls == []
        assert ready.calls == []
    finally:
        restarted.coordinator.close(wait=True)
        restarted.store.dispose()


def test_persisted_result_unknown_blocks_new_dispatch_while_owned_future_drains() -> None:
    blocker = BlockingTool("blocker")
    gate = _tool("gate")
    uncertain = _tool("uncertain")
    uncertain.script(["result_unknown"])
    later = _tool("later")
    runtime = _make_runtime(
        steps=[
            {
                "id": "blocker",
                "tool": {"name": "blocker", "version": "1.0.0"},
                "arguments": {"value": "block"},
            },
            {
                "id": "gate",
                "tool": {"name": "gate", "version": "1.0.0"},
                "arguments": {"value": "gate"},
            },
            {
                "id": "uncertain",
                "tool": {"name": "uncertain", "version": "1.0.0"},
                "arguments": {"value": "uncertain"},
            },
            {
                "id": "later",
                "tool": {"name": "later", "version": "1.0.0"},
                "arguments": {"value": "later"},
                "depends_on": ["gate"],
            },
        ],
        tools=[blocker, gate, uncertain, later],
    )
    try:
        first = runtime.coordinator.run_once()
        assert set(first.dispatched) == {"blocker", "gate", "uncertain"}
        assert blocker.started.wait(timeout=1)
        assert gate.wait_for_calls(1)
        assert uncertain.wait_for_calls(1)

        with runtime.raw_uow_factory() as uow:
            attempts_before = len(uow.attempts.list(runtime.run_id))
            events_before = len(uow.events.list(runtime.run_id))

        report = runtime.coordinator.run_once()

        with runtime.raw_uow_factory() as uow:
            assert len(uow.attempts.list(runtime.run_id)) == attempts_before
            assert len(uow.events.list(runtime.run_id)) == events_before + 2
        assert report.blocked_reason == "result_unknown"
        assert report.dispatched == ()
        assert runtime.step("later").state is StepState.PENDING
        assert later.calls == []
    finally:
        blocker.release.set()
        runtime.coordinator.close(wait=True)


def _seed_approval_with_recovery_fact(
    runtime: Runtime,
    *,
    approval_status: str,
    recovery_fact: str,
) -> ApprovalRequest:
    with runtime.raw_uow_factory() as uow:
        run = uow.runs.get(runtime.run_id)
        definition = uow.definitions.get(run.definition_id, run.definition_version)
        protected = next(step for step in definition.steps if step.id == "protected")
        redacted_arguments, binding_digest, risk_reasons = (
            runtime.coordinator._approval_snapshot(definition, protected)
        )
        request = ApprovalRequest(
            id=f"request-{approval_status}-{recovery_fact}",
            run_id=runtime.run_id,
            definition_digest=definition.digest,
            step_id=protected.id,
            tool_name=protected.tool.name,
            tool_version=protected.tool.version,
            redacted_arguments=redacted_arguments,
            binding_digest=binding_digest,
            risk_reasons=risk_reasons,
            created_at=runtime.clock.now(),
        )
        if approval_status != "pending":
            request = replace(
                request,
                version=1,
                status=approval_status,
                decision=approval_status,
                actor={"algorithm": "sha256", "digest": "a" * 64},
                reason={"provided": False, "algorithm": None, "digest": None},
                decided_at=runtime.clock.now(),
            )

        target_run_state = {
            "pending": RunState.WAITING_APPROVAL,
            "approved": RunState.RUNNING,
            "rejected": RunState.CANCELLED,
        }[approval_status]
        uow.runs.save(
            replace(
                run,
                state=target_run_state,
                revision=run.revision + 1,
            ),
            expected_revision=run.revision,
        )
        uncertain = uow.steps.get(runtime.run_id, "uncertain")
        if recovery_fact == "step_state":
            uow.steps.save(
                replace(
                    uncertain,
                    state=StepState.RESULT_UNKNOWN,
                    revision=uncertain.revision + 1,
                ),
                expected_revision=uncertain.revision,
            )
        else:
            uow.steps.save(
                replace(
                    uncertain,
                    state=StepState.SUCCEEDED,
                    revision=uncertain.revision + 1,
                ),
                expected_revision=uncertain.revision,
            )
            uow.attempts.add(
                StepAttempt(
                    run_id=runtime.run_id,
                    step_id="uncertain",
                    attempt_no=1,
                    phase="forward",
                    attempt_id="attempt-before-restart",
                    status=(
                        "result_unknown"
                        if recovery_fact == "attempt_status"
                        else "failed"
                    ),
                    idempotency_key="logical-key",
                    started_at=runtime.clock.now(),
                    completed_at=runtime.clock.now(),
                    error_class=(
                        "result_unknown"
                        if recovery_fact == "attempt_error_class"
                        else None
                    ),
                )
            )
        uow.approvals.add(request)
        uow.commit()
    return request


@pytest.mark.parametrize("approval_status", ["pending", "approved", "rejected"])
@pytest.mark.parametrize("decision", ["approved", "rejected"])
def test_sqlite_restart_result_unknown_blocks_every_approval_decision(
    tmp_path: Path,
    approval_status: str,
    decision: str,
) -> None:
    database_path = tmp_path / f"result-unknown-{approval_status}-{decision}.db"
    uncertain = _tool("inspect")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "uncertain",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "first"},
            },
            _protected_step(),
        ],
        tools=[uncertain, protected],
        sqlite_path=database_path,
    )
    request = _seed_approval_with_recovery_fact(
        runtime,
        approval_status=approval_status,
        recovery_fact="step_state",
    )
    runtime.coordinator.close(wait=True)
    runtime.store.dispose()

    restarted = _make_runtime(
        steps=[],
        tools=[uncertain, protected],
        sqlite_path=database_path,
    )
    try:
        before_run = restarted.run()
        before_request = next(
            item
            for item in restarted.approval_records()
            if item.id == request.id
        )
        with pytest.raises(ApprovalDecisionError, match="approval_recovery_required"):
            restarted.approvals.decide(
                restarted.run_id,
                request.id,
                decision,
                expected_version=request.version,
                binding_digest=request.binding_digest,
                actor="ops-user",
                reason="decision must wait for recovery",
            )

        assert restarted.run() == before_run
        assert next(
            item
            for item in restarted.approval_records()
            if item.id == request.id
        ) == before_request
        report = restarted.coordinator.run_once()
        assert report.blocked_reason == (
            "run_terminal"
            if approval_status == "rejected"
            else "approval_recovery_required"
        )
        assert protected.calls == []
    finally:
        restarted.coordinator.close(wait=True)
        restarted.store.dispose()


@pytest.mark.parametrize("recovery_fact", ["attempt_status", "attempt_error_class"])
def test_sqlite_restart_attempt_result_unknown_blocks_approval_and_dispatch(
    tmp_path: Path,
    recovery_fact: str,
) -> None:
    database_path = tmp_path / f"result-unknown-{recovery_fact}.db"
    uncertain = _tool("inspect")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "uncertain",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "first"},
            },
            _protected_step(),
        ],
        tools=[uncertain, protected],
        sqlite_path=database_path,
    )
    request = _seed_approval_with_recovery_fact(
        runtime,
        approval_status="pending",
        recovery_fact=recovery_fact,
    )
    runtime.coordinator.close(wait=True)
    runtime.store.dispose()

    restarted = _make_runtime(
        steps=[],
        tools=[uncertain, protected],
        sqlite_path=database_path,
    )
    try:
        with pytest.raises(ApprovalDecisionError, match="approval_recovery_required"):
            restarted.approvals.decide(
                restarted.run_id,
                request.id,
                "approved",
                expected_version=request.version,
                binding_digest=request.binding_digest,
                actor="ops-user",
                reason="decision must wait for recovery",
            )
        assert restarted.coordinator.run_once().blocked_reason == (
            "approval_recovery_required"
        )
        assert protected.calls == []
    finally:
        restarted.coordinator.close(wait=True)
        restarted.store.dispose()


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
            runtime.run_id,
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
            runtime.run_id,
            request.id,
            "rejected",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="unsafe window",
        )

        assert runtime.run().state is RunState.CANCELLED
        assert tool.calls == []
        rejected_event = next(
            event for event in runtime.events() if event.event_type == "approval.rejected"
        )
        assert rejected_event.summary["reason"]["provided"] is True
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
            runtime.run_id,
            request.id,
            "rejected",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="rollback requested",
        )

        assert runtime.run().state is RunState.COMPENSATING
        assert runtime.attempts("precheck")[-1].effect_applied is True
        assert compensation.calls == []
        assert protected.calls == []
    finally:
        runtime.coordinator.close(wait=True)


def test_rejection_after_success_without_effect_cancels_run() -> None:
    precheck = _NoEffectTool("precheck")
    compensation = _tool("precheck.undo")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "precheck",
                "tool": {"name": "precheck", "version": "1.0.0"},
                "arguments": {"value": "checked"},
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
            runtime.run_id,
            request.id,
            "rejected",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="no effect observed",
        )

        attempt = runtime.attempts("precheck")[-1]
        completed_event = next(
            event
            for event in runtime.events()
            if event.event_type == "tool_attempt_completed"
        )
        assert attempt.effect_applied is False
        assert completed_event.summary["effect_applied"] is False
        assert runtime.run().state is RunState.CANCELLED
    finally:
        runtime.coordinator.close(wait=True)


def test_rejection_after_non_compensable_effect_requires_manual_intervention() -> None:
    precheck = _tool("precheck")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "precheck",
                "tool": {"name": "precheck", "version": "1.0.0"},
                "arguments": {"value": "applied"},
            },
            _protected_step(depends_on=["precheck"]),
        ],
        tools=[precheck, protected],
    )
    try:
        runtime.coordinator.run_once()
        assert precheck.wait_for_calls(1)
        runtime.coordinator.run_once()
        request = runtime.pending()

        runtime.approvals.decide(
            runtime.run_id,
            request.id,
            "rejected",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="effect cannot be compensated",
        )

        assert runtime.attempts("precheck")[-1].effect_applied is True
        assert runtime.run().state is RunState.MANUAL_INTERVENTION
        rejected_event = next(
            event for event in runtime.events() if event.event_type == "approval.rejected"
        )
        assert rejected_event.new_state == RunState.MANUAL_INTERVENTION.value
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
                runtime.run_id,
                request.id,
                "approved",
                expected_version=9,
                binding_digest=request.binding_digest,
                actor="ops-user",
                reason="stale browser",
            )
        with pytest.raises(ApprovalDecisionError, match="binding_mismatch"):
            runtime.approvals.decide(
                runtime.run_id,
                request.id,
                "approved",
                expected_version=0,
                binding_digest="wrong-binding",
                actor="ops-user",
                reason="wrong plan",
            )

        runtime.approvals.decide(
            runtime.run_id,
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved once",
        )
        with pytest.raises(ApprovalDecisionError, match="already_decided"):
            runtime.approvals.decide(
                runtime.run_id,
                request.id,
                "approved",
                expected_version=1,
                binding_digest=request.binding_digest,
                actor="ops-user",
                reason="duplicate click",
            )
    finally:
        runtime.coordinator.close(wait=True)


def test_same_request_id_is_decided_independently_for_each_run() -> None:
    store = MemoryStore()
    first_tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    second_tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    first = _make_runtime(
        steps=[_protected_step()],
        tools=[first_tool],
        existing_store=store,
        run_id="run-1",
    )
    second = _make_runtime(
        steps=[_protected_step()],
        tools=[second_tool],
        existing_store=store,
        run_id="run-2",
    )
    try:
        first.coordinator.run_once()
        second.coordinator.run_once()
        first_request = first.pending()
        second_request = second.pending()
        assert first_request.id == second_request.id == "id-1"

        first.approvals.decide(
            "run-1",
            first_request.id,
            "approved",
            expected_version=0,
            binding_digest=first_request.binding_digest,
            actor="first-operator",
            reason="first approval",
        )

        assert first.run().state is RunState.RUNNING
        assert second.run().state is RunState.WAITING_APPROVAL
        assert second.pending() == second_request
    finally:
        first.coordinator.close(wait=True)
        second.coordinator.close(wait=True)


def test_multiple_high_risk_steps_require_distinct_sequential_approvals() -> None:
    first_tool = _tool("schema.first", risk=ToolRisk.HIGH)
    second_tool = _tool("schema.second", risk=ToolRisk.HIGH)
    runtime = _make_runtime(
        steps=[
            {
                "id": "first-protected",
                "tool": {"name": "schema.first", "version": "1.0.0"},
                "arguments": {"value": "first"},
            },
            {
                "id": "second-protected",
                "tool": {"name": "schema.second", "version": "1.0.0"},
                "arguments": {"value": "second"},
            },
        ],
        tools=[first_tool, second_tool],
    )
    try:
        runtime.coordinator.run_once()
        first_request = runtime.pending()
        assert first_request.step_id == "first-protected"
        runtime.approvals.decide(
            runtime.run_id,
            first_request.id,
            "approved",
            expected_version=0,
            binding_digest=first_request.binding_digest,
            actor="ops-user",
            reason="approve first",
        )
        runtime.coordinator.run_once()
        assert first_tool.wait_for_calls(1)

        second_request = None
        for _ in range(10):
            runtime.coordinator.run_once()
            second_request = runtime.pending()
            if second_request is not None:
                break
        assert second_request is not None
        assert second_request.id != first_request.id
        assert second_request.step_id == "second-protected"
        assert second_tool.calls == []

        runtime.approvals.decide(
            runtime.run_id,
            second_request.id,
            "approved",
            expected_version=0,
            binding_digest=second_request.binding_digest,
            actor="ops-user",
            reason="approve second",
        )
        runtime.coordinator.run_once()
        assert second_tool.wait_for_calls(1)
    finally:
        runtime.coordinator.close(wait=True)


def test_selector_barrier_ignores_running_work_owned_by_another_run() -> None:
    store = MemoryStore()
    slow = BlockingTool("inspect")
    protected = _tool("schema.apply", risk=ToolRisk.HIGH)
    first = _make_runtime(
        steps=[
            {
                "id": "slow",
                "tool": {"name": "inspect", "version": "1.0.0"},
                "arguments": {"value": "slow"},
            }
        ],
        tools=[slow],
        existing_store=store,
        run_id="run-1",
        definition_id="workflow-1",
    )
    second = _make_runtime(
        steps=[_protected_step()],
        tools=[protected],
        existing_store=store,
        run_id="run-2",
        definition_id="workflow-2",
    )
    first.coordinator.close(wait=True)
    second.coordinator.close(wait=True)
    selected = {"run_id": "run-1"}
    coordinator = Coordinator(
        uow_factory=first.raw_uow_factory,
        tools=ToolRegistry([slow, protected]),
        clock=first.clock,
        identifiers=DeterministicIdentifiers(),
        run_id_selector=lambda: selected["run_id"],
        concurrency=2,
    )
    try:
        first_report = coordinator.run_once()
        assert first_report.dispatched == ("slow",)
        assert slow.started.wait(timeout=1)

        selected["run_id"] = "run-2"
        second_report = coordinator.run_once()
        with first.raw_uow_factory() as uow:
            first_run = uow.runs.get("run-1")
            second_run = uow.runs.get("run-2")
            second_pending = uow.approvals.pending("run-2")

        assert second_report.blocked_reason == "approval_pending"
        assert first_run.state is RunState.RUNNING
        assert second_run.state is RunState.WAITING_APPROVAL
        assert second_pending.step_id == "protected"
        assert protected.calls == []
    finally:
        slow.release.set()
        coordinator.close(wait=True)


@pytest.mark.parametrize("changed_component", ["arguments", "tool_version", "definition"])
def test_changed_binding_invalidates_old_approval_and_creates_new_pending(
    changed_component: str,
) -> None:
    tool = _tool("schema.apply", risk=ToolRisk.HIGH)
    upgraded_tool = _tool("schema.apply", risk=ToolRisk.HIGH, version="2.0.0")
    runtime = _make_runtime(
        steps=[_protected_step()],
        tools=[tool, upgraded_tool],
    )
    try:
        runtime.coordinator.run_once()
        request = runtime.pending()
        approved = runtime.approvals.decide(
            runtime.run_id,
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved old snapshot",
        )
        _switch_current_definition(runtime, changed_component)

        report = runtime.coordinator.run_once()
        records = runtime.approval_records()
        assert report.blocked_reason == "approval_pending"
        assert len(records) == 2
        assert records[0].status == "invalidated"
        assert runtime.pending().binding_digest != request.binding_digest
        assert runtime.pending().id != request.id
        if changed_component == "arguments":
            assert runtime.pending().redacted_arguments == {"value": "v3"}
        elif changed_component == "tool_version":
            assert runtime.pending().tool_version == "2.0.0"
        else:
            assert runtime.pending().definition_digest != request.definition_digest
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
            runtime.run_id,
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
            runtime.run_id,
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor="ops-user",
            reason="approved snapshot",
        )
        stale = replace(
            approved,
            binding_digest="b" * 64,
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
        assert approval_events[1].summary["actor"]["algorithm"] == "sha256"
        assert len(approval_events[1].summary["actor"]["digest"]) == 64
        assert approval_events[1].summary["reason"]["provided"] is True
        assert approval_events[1].summary["reason"]["algorithm"] == "sha256"
        assert len(approval_events[1].summary["reason"]["digest"]) == 64
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
    actor = (
        "ops-user\nAuthorization: Bearer bearer-actor-secret\n"
        '{"session":"json-actor-secret"}'
    )
    reason = (
        "emergency review\n"
        '{"credential":"json-reason-secret"}\n'
        "UNLABELED-MULTILINE-SECRET"
    )
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
        decided = runtime.approvals.decide(
            runtime.run_id,
            request.id,
            "approved",
            expected_version=0,
            binding_digest=request.binding_digest,
            actor=actor,
            reason=reason,
        )

        assert decided.actor.algorithm == "sha256"
        assert len(decided.actor.digest) == 64
        assert decided.reason.provided is True
        assert decided.reason.algorithm == "sha256"
        assert len(decided.reason.digest) == 64

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
        persisted_text = "\n".join(stored)
        for raw_value in (secret, actor, reason, "bearer-actor-secret", "json-actor-secret", "json-reason-secret", "UNLABELED-MULTILINE-SECRET"):
            assert raw_value not in persisted_text

        runtime.coordinator.close(wait=True)
        runtime.store.dispose()
        database_bytes = database_path.read_bytes()
        for raw_value in (
            actor,
            reason,
            "bearer-actor-secret",
            "json-actor-secret",
            "json-reason-secret",
            "UNLABELED-MULTILINE-SECRET",
        ):
            assert raw_value.encode("utf-8") not in database_bytes
    finally:
        runtime.coordinator.close(wait=True)
        runtime.store.dispose()
