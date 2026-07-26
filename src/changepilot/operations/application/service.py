from __future__ import annotations

import threading
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict

from changepilot.operations.domain.models import (
    ApprovalCommand,
    ChangeRequestInput,
    ChangeRequestView,
    DemoScenario,
    EventPage,
    PlanStepView,
    PlanToolView,
    PlanView,
    RecoveryCommand,
    RequestStatus,
    RunSnapshot,
    RunSummary,
)
from changepilot.sandbox.application.runtime import (
    SandboxWorkflowRuntime,
    make_sandbox_runtime,
    order_upgrade_sandbox_definition,
)
from changepilot.sandbox.domain.faults import FaultSpec, FaultType


_TERMINAL_STATES = {
    "succeeded",
    "failed",
    "cancelled",
    "compensated",
    "compensation_failed",
    "manual_intervention",
}
_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "password",
    "secret",
    "token",
}


class OperationsConflictError(RuntimeError):
    """Raised when a command does not match the authoritative run state."""


class _StoredRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    view: ChangeRequestView
    definition_payload: dict[str, Any] | None = None


class _Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    requests: tuple[_StoredRequest, ...] = ()


class OperationsService:
    def __init__(self, root: str | Path) -> None:
        self._root = Path(root).resolve()
        self._root.mkdir(parents=True, exist_ok=True)
        self._manifest_path = self._root / "operations.json"
        self._lock = threading.RLock()
        self._requests = self._load_manifest()
        self._runtimes: dict[str, SandboxWorkflowRuntime] = {}

    def submit_request(self, request: ChangeRequestInput) -> ChangeRequestView:
        with self._lock:
            request_id = str(uuid4())
            submitted_at = datetime.now(timezone.utc).isoformat()
            missing = tuple(
                field
                for field in ("service_id", "current_version", "target_version")
                if getattr(request, field) is None
            )
            if missing:
                view = ChangeRequestView(
                    request_id=request_id,
                    status=RequestStatus.CLARIFICATION_REQUIRED,
                    submitted_at=submitted_at,
                    request=request,
                    clarification_questions=tuple(
                        f"Please provide {field.replace('_', ' ')}."
                        for field in missing
                    ),
                )
                self._store(_StoredRequest(view=view))
                return view

            errors = self._validate_supported_change(request)
            if errors:
                view = ChangeRequestView(
                    request_id=request_id,
                    status=RequestStatus.REJECTED,
                    submitted_at=submitted_at,
                    request=request,
                    errors=errors,
                )
                self._store(_StoredRequest(view=view))
                return view

            runtime = make_sandbox_runtime(
                self._run_root(request_id),
                fault_specs=self._fault_specs(request.scenario),
            )
            try:
                runtime.driver.start()
                initial = runtime.manager.inspect(runtime.sandbox_id)
                definition = order_upgrade_sandbox_definition(
                    runtime.sandbox_id,
                    initial.database_fingerprint,
                )
                run_id = runtime.workflow.create_run(definition)
                runtime.selected_run[0] = run_id
                runtime.run_until("waiting_approval")
            except BaseException:
                runtime.close()
                raise
            self._runtimes[run_id] = runtime
            view = ChangeRequestView(
                request_id=request_id,
                status=RequestStatus.PLAN_READY,
                submitted_at=submitted_at,
                request=request,
                run_id=run_id,
            )
            self._store(
                _StoredRequest(
                    view=view,
                    definition_payload=definition,
                )
            )
            return view

    def list_requests(self) -> tuple[ChangeRequestView, ...]:
        with self._lock:
            return tuple(
                item.view
                for item in sorted(
                    self._requests.values(),
                    key=lambda stored: stored.view.submitted_at,
                    reverse=True,
                )
            )

    def list_runs(self, *, state: str | None = None) -> tuple[RunSummary, ...]:
        with self._lock:
            summaries = tuple(
                self._snapshot(stored.view.run_id).summary
                for stored in self._requests.values()
                if stored.view.run_id is not None
            )
            if state is not None:
                summaries = tuple(
                    summary for summary in summaries if summary.state == state
                )
            return tuple(
                sorted(summaries, key=lambda item: item.run_id, reverse=True)
            )

    def get_snapshot(self, run_id: str) -> RunSnapshot:
        with self._lock:
            return self._snapshot(run_id)

    def get_plan(self, run_id: str) -> PlanView:
        with self._lock:
            stored = self._stored_for_run(run_id)
            payload = stored.definition_payload
            if payload is None:
                raise LookupError(f"run {run_id} has no prepared plan")
            snapshot = self._snapshot(run_id)
            steps = tuple(
                PlanStepView(
                    step_id=str(step["id"]),
                    tool=PlanToolView(**step["tool"]),
                    depends_on=tuple(step.get("depends_on", ())),
                    risk=str(step.get("risk", "low")),
                    approval_required=str(step.get("risk", "low")) == "high",
                    arguments=_redact(step.get("arguments", {})),
                    compensation_tool=(
                        PlanToolView(**step["compensation_tool"])
                        if step.get("compensation_tool") is not None
                        else None
                    ),
                    rationale=_plan_annotations(str(step["tool"]["name"]))[0],
                    validation_intent=_plan_annotations(
                        str(step["tool"]["name"])
                    )[1],
                    evidence_refs=_plan_annotations(
                        str(step["tool"]["name"])
                    )[2],
                )
                for step in payload["steps"]
            )
            return PlanView(
                run_id=run_id,
                definition_id=snapshot.summary.definition_id,
                definition_version=snapshot.summary.definition_version,
                definition_digest=self._runtime_for(run_id)
                .query.get_run(run_id)
                .definition_digest,
                steps=steps,
            )

    def get_events(self, run_id: str, after_sequence: int = 0) -> EventPage:
        with self._lock:
            runtime = self._runtime_for(run_id)
            events = runtime.query.list_events(run_id, after_sequence)
            payloads = tuple(
                _redact(event.model_dump(mode="python")) for event in events
            )
            next_cursor = (
                int(payloads[-1]["sequence"]) if payloads else after_sequence
            )
            return EventPage(
                run_id=run_id,
                after_sequence=after_sequence,
                next_cursor=next_cursor,
                events=payloads,
            )

    def decide_approval(
        self,
        run_id: str,
        command: ApprovalCommand,
    ) -> RunSnapshot:
        with self._lock:
            runtime = self._runtime_for(run_id)
            runtime.driver.start()
            current = runtime.query.get_run(run_id)
            pending = current.pending_approval
            if pending is None:
                raise OperationsConflictError(
                    f"run {run_id} has no current pending approval"
                )
            runtime.approvals.decide(
                run_id,
                pending.request_id,
                command.decision,
                expected_version=command.expected_version,
                binding_digest=command.binding_digest,
                actor=command.actor,
                reason=command.reason,
            )
            if command.decision == "rejected":
                runtime.run_until("cancelled")
                return self._snapshot(run_id)

            scenario = self._stored_for_run(run_id).view.request.scenario
            if scenario is DemoScenario.COMPENSATION:
                runtime.run_until("compensated")
                return self._snapshot(run_id)
            if scenario is DemoScenario.RECOVERY:
                runtime.run_until_step("migrate-schema", "result_unknown")
                snapshot = self._snapshot(run_id)
                runtime.close()
                self._runtimes.pop(run_id, None)
                return snapshot
            runtime.run_until("succeeded")
            return self._snapshot(run_id)

    def recover(self, run_id: str, command: RecoveryCommand) -> RunSnapshot:
        with self._lock:
            current = self._snapshot(run_id)
            if current.summary.revision != command.expected_run_revision:
                raise OperationsConflictError(
                    "run revision changed; refresh the authoritative snapshot"
                )
            if not any(
                step["state"] == "result_unknown" for step in current.steps
            ):
                raise OperationsConflictError(
                    f"run {run_id} has no recoverable result-unknown step"
                )
            runtime = self._runtime_for(run_id)
            runtime.driver.start()
            runtime.run_until("succeeded")
            return self._snapshot(run_id)

    def render_report(self, run_id: str, *, markdown: bool) -> str | dict[str, Any]:
        with self._lock:
            snapshot = self._snapshot(run_id)
            if snapshot.summary.state not in _TERMINAL_STATES:
                raise OperationsConflictError(
                    "reports are available only for terminal runs"
                )
            stored = self._stored_for_run(run_id)
            plan = self.get_plan(run_id)
            events = self.get_events(run_id)
            report = _redact(
                {
                    "report_version": "1.0",
                    "request": stored.view.model_dump(mode="json"),
                    "plan": plan.model_dump(mode="json"),
                    "run": snapshot.model_dump(mode="json"),
                    "events": list(events.events),
                    "model_usage": {
                        "calls": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "estimated_cost": 0.0,
                    },
                }
            )
            if not markdown:
                return report
            return _markdown_report(report)

    def get_tool_call_count(self, run_id: str, tool_name: str) -> int:
        with self._lock:
            runtime = self._runtime_for(run_id)
            return runtime.faults.call_count(runtime.sandbox_id, tool_name)

    def close(self) -> None:
        with self._lock:
            for runtime in self._runtimes.values():
                runtime.close()
            self._runtimes.clear()

    def _snapshot(self, run_id: str) -> RunSnapshot:
        runtime = self._runtime_for(run_id)
        run = runtime.query.get_run(run_id)
        events = runtime.query.list_events(run_id)
        return RunSnapshot(
            summary=RunSummary(
                request_id=self._stored_for_run(run_id).view.request_id,
                run_id=run.run_id,
                definition_id=run.definition_id,
                definition_version=run.definition_version,
                state=run.state,
                revision=run.revision,
                pending_approval=run.pending_approval is not None,
                last_event_sequence=events[-1].sequence if events else 0,
            ),
            steps=tuple(
                step.model_dump(mode="json") for step in run.steps
            ),
            pending_approval=(
                _redact(run.pending_approval.model_dump(mode="python"))
                if run.pending_approval is not None
                else None
            ),
            original_error=(
                run.original_error.model_dump(mode="json")
                if run.original_error is not None
                else None
            ),
            compensation_error=(
                run.compensation_error.model_dump(mode="json")
                if run.compensation_error is not None
                else None
            ),
        )

    def _runtime_for(self, run_id: str) -> SandboxWorkflowRuntime:
        runtime = self._runtimes.get(run_id)
        if runtime is not None:
            return runtime
        stored = self._stored_for_run(run_id)
        runtime = make_sandbox_runtime(
            self._run_root(stored.view.request_id),
            fault_specs=None,
            selected_run_id=run_id,
        )
        self._runtimes[run_id] = runtime
        return runtime

    def _stored_for_run(self, run_id: str) -> _StoredRequest:
        for stored in self._requests.values():
            if stored.view.run_id == run_id:
                return stored
        raise LookupError(f"unknown run {run_id}")

    def _store(self, stored: _StoredRequest) -> None:
        self._requests[stored.view.request_id] = stored
        payload = _Manifest(
            requests=tuple(self._requests.values())
        ).model_dump_json(indent=2)
        temporary = self._manifest_path.with_suffix(".tmp")
        temporary.write_text(payload, encoding="utf-8")
        temporary.replace(self._manifest_path)

    def _load_manifest(self) -> dict[str, _StoredRequest]:
        if not self._manifest_path.exists():
            return {}
        manifest = _Manifest.model_validate_json(
            self._manifest_path.read_text(encoding="utf-8"),
            strict=False,
        )
        return {item.view.request_id: item for item in manifest.requests}

    def _run_root(self, request_id: str) -> Path:
        return self._root / "runs" / request_id

    @staticmethod
    def _validate_supported_change(
        request: ChangeRequestInput,
    ) -> tuple[str, ...]:
        errors: list[str] = []
        if request.service_id != "order-service":
            errors.append("V1 supports only the isolated order-service demo")
        if request.current_version != "v1" or request.target_version != "v2":
            errors.append("V1 supports only the v1 to v2 upgrade")
        return tuple(errors)

    @staticmethod
    def _fault_specs(
        scenario: DemoScenario,
    ) -> tuple[FaultSpec, ...]:
        if scenario is DemoScenario.COMPENSATION:
            return (
                FaultSpec(
                    tool_name="service.health-check",
                    call_number=1,
                    fault_type=FaultType.PERMANENT_FAILURE,
                ),
            )
        if scenario is DemoScenario.RECOVERY:
            return (
                FaultSpec(
                    tool_name="schema.migrate",
                    call_number=1,
                    fault_type=FaultType.AFTER_EFFECT,
                ),
            )
        return ()


def _redact(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _redact(value.model_dump(mode="python"))
    if isinstance(value, Mapping):
        return {
            str(key): (
                "***REDACTED***"
                if str(key).lower() in _SENSITIVE_KEYS
                else _redact(item)
            )
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return value


def _markdown_report(report: dict[str, Any]) -> str:
    run = report["run"]["summary"]
    request = report["request"]["request"]
    lines = [
        "# ChangePilot Run Report",
        "",
        f"- Run: `{run['run_id']}`",
        f"- State: `{run['state']}`",
        f"- Service: `{request['service_id']}`",
        f"- Change: {request['change_summary']}",
        "",
        "## Steps",
        "",
    ]
    for step in report["run"]["steps"]:
        lines.append(f"- `{step['step_id']}`: `{step['state']}`")
    lines.extend(
        [
            "",
            "## Audit",
            "",
            f"- Events: {len(report['events'])}",
            f"- Last sequence: {run['last_event_sequence']}",
            "",
        ]
    )
    return "\n".join(lines)


def _plan_annotations(
    tool_name: str,
) -> tuple[str, str, tuple[str, ...]]:
    annotations = {
        "service.inspect": (
            "Capture the deployed service version before making changes.",
            "Service reports the expected V1 baseline.",
            ("order-service-upgrade@1.0#prechecks",),
        ),
        "schema.inspect": (
            "Capture the schema version and immutable database fingerprint.",
            "Schema reports V1 and a valid precondition fingerprint.",
            ("schema-migration@1.0#preconditions",),
        ),
        "upgrade.precheck": (
            "Verify the service and database form a supported upgrade baseline.",
            "All compatibility and migration prerequisites pass.",
            (
                "order-service-upgrade@1.0#compatibility",
                "schema-migration@1.0#preconditions",
            ),
        ),
        "schema.migrate": (
            "Apply the versioned V1 to V2 order-schema migration.",
            "Migration ledger records one committed V2 transition.",
            (
                "schema-migration@1.0#procedure",
                "rollback-policy@1.0#database",
            ),
        ),
        "service.deploy-v2": (
            "Switch the isolated order service to the V2 contract.",
            "V2 starts only after the V2 schema is available.",
            (
                "order-service-upgrade@1.0#deployment",
                "rollback-policy@1.0#service",
            ),
        ),
        "service.health-check": (
            "Check service and database compatibility after deployment.",
            "The V2 health contract returns success.",
            ("order-service-upgrade@1.0#validation",),
        ),
        "service.smoke-test": (
            "Exercise order reads and the versioned business contract.",
            "Orders remain readable and the V2 contract is valid.",
            ("order-service-upgrade@1.0#smoke-test",),
        ),
    }
    return annotations.get(
        tool_name,
        (
            "Execute the validated plan step.",
            "The tool returns its declared success contract.",
            (),
        ),
    )
