from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from typing import Callable

from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.clock import Clock
from changepilot.workflow.ports.persistence import OptimisticLockError


_SENSITIVE_TEXT_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?key|authorization|password|secret(?:[_-]?key)?|token)"
    r"(\s*[:=]\s*)([^\s,;]+)"
)


def approval_binding_digest(
    plan_digest: str,
    step_id: str,
    tool_name: str,
    tool_version: str,
    redacted_arguments: object,
) -> str:
    payload = {
        "plan_digest": plan_digest,
        "step_id": step_id,
        "tool": [tool_name, tool_version],
        "arguments": redacted_arguments,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class ApprovalDecisionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ApprovalRequest:
    id: str
    run_id: str
    definition_digest: str
    step_id: str
    tool_name: str
    tool_version: str
    redacted_arguments: object
    binding_digest: str
    risk_reasons: tuple[str, ...]
    created_at: str
    version: int = 0
    status: str = "pending"
    decision: str | None = None
    actor: str | None = None
    reason: str | None = None
    decided_at: str | None = None

    @property
    def approval_key(self) -> str:
        return self.id

    def matches(
        self,
        *,
        definition_digest: str,
        step_id: str,
        tool_name: str,
        tool_version: str,
        binding_digest: str,
    ) -> bool:
        return (
            self.definition_digest == definition_digest
            and self.step_id == step_id
            and self.tool_name == tool_name
            and self.tool_version == tool_version
            and self.binding_digest == binding_digest
        )


class ApprovalService:
    def __init__(
        self,
        *,
        uow_factory: Callable[[], object],
        clock: Clock,
    ) -> None:
        self._uow_factory = uow_factory
        self._clock = clock

    def decide(
        self,
        request_id: str,
        decision: str,
        *,
        expected_version: int,
        binding_digest: str,
        actor: str,
        reason: str,
    ) -> ApprovalRequest:
        if decision not in {"approved", "rejected"}:
            raise ApprovalDecisionError("invalid_decision")

        occurred_at = self._clock.now()
        safe_actor = _redact_approval_text(actor)
        safe_reason = _redact_approval_text(reason)
        with self._uow_factory() as uow:
            request = uow.approvals.get(request_id)
            if request is None:
                raise LookupError(f"unknown approval request {request_id}")
            if request.version != expected_version:
                raise OptimisticLockError(
                    aggregate_type="approval",
                    identifier=request_id,
                    expected_revision=expected_version,
                    actual_revision=request.version,
                )
            if request.status != "pending":
                raise ApprovalDecisionError("already_decided")
            if request.binding_digest != binding_digest:
                raise ApprovalDecisionError("binding_mismatch")

            run = uow.runs.get(request.run_id)
            if run is None:
                raise LookupError(f"unknown run {request.run_id}")
            if run.state is not RunState.WAITING_APPROVAL:
                raise ApprovalDecisionError("run_not_waiting_approval")

            target_state = (
                RunState.RUNNING
                if decision == "approved"
                else self._rejection_target(uow, run)
            )
            decided = replace(
                request,
                version=request.version + 1,
                status=decision,
                decision=decision,
                actor=safe_actor,
                reason=safe_reason,
                decided_at=occurred_at,
            )
            changed_run, run_event = run.transition(
                target_state,
                occurred_at=occurred_at,
            )
            decision_event = AuditEvent.approval_changed(
                run_id=run.run_id,
                step_id=request.step_id,
                event_type=f"approval.{decision}",
                occurred_at=occurred_at,
                previous_state=run.state.value,
                new_state=target_state.value,
                previous_revision=run.revision,
                revision=changed_run.revision,
                request_id=request.id,
                binding_digest=request.binding_digest,
                status=decision,
                actor=safe_actor,
                reason=safe_reason,
            )
            uow.approvals.save(decided, expected_version=request.version)
            uow.runs.save(changed_run, expected_revision=run.revision)
            uow.events.append(decision_event)
            uow.events.append(run_event)
            uow.commit()
            return decided

    @staticmethod
    def _rejection_target(uow: object, run: object) -> RunState:
        definition = uow.definitions.get(run.definition_id, run.definition_version)
        if definition is None:
            raise LookupError(
                f"unknown definition {run.definition_id}@{run.definition_version}"
            )
        for step in definition.steps:
            step_run = uow.steps.get(run.run_id, step.id)
            if (
                step_run is not None
                and step_run.state is StepState.SUCCEEDED
                and step.compensation_tool is not None
            ):
                return RunState.COMPENSATING
        return RunState.CANCELLED


def _redact_approval_text(value: str) -> str:
    return _SENSITIVE_TEXT_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]",
        value,
    )
