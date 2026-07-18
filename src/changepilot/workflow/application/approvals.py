from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Callable

from changepilot.workflow.domain.events import AuditEvent
from changepilot.workflow.domain.failures import ErrorClass
from changepilot.workflow.domain.states import RunState, StepState
from changepilot.workflow.ports.clock import Clock
from changepilot.workflow.ports.persistence import (
    LegacyApprovalRecord,
    OptimisticLockError,
)


def approval_binding_digest(
    plan_digest: str,
    step_id: str,
    tool_name: str,
    tool_version: str,
    redacted_arguments: object,
) -> str:
    canonical_arguments = canonical_json_value(redacted_arguments)
    payload = {
        "plan_digest": plan_digest,
        "step_id": step_id,
        "tool": [tool_name, tool_version],
        "arguments": canonical_arguments,
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def canonical_json_value(value: object) -> object:
    return _canonical_json_value(value, seen=set())


def _canonical_json_value(value: object, *, seen: set[int]) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("approval binding must contain canonical JSON values")
        return value
    if isinstance(value, Mapping):
        identity = id(value)
        if identity in seen:
            raise ValueError("approval binding must contain canonical JSON values")
        seen.add(identity)
        try:
            output: dict[str, object] = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError(
                        "approval binding must contain canonical JSON string keys"
                    )
                output[key] = _canonical_json_value(item, seen=seen)
            return output
        finally:
            seen.remove(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in seen:
            raise ValueError("approval binding must contain canonical JSON values")
        seen.add(identity)
        try:
            return [_canonical_json_value(item, seen=seen) for item in value]
        finally:
            seen.remove(identity)
    raise ValueError("approval binding must contain canonical JSON values")


class ApprovalDecisionError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    INVALIDATED = "invalidated"


class ApprovalDecision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class ApprovalActorDigest:
    algorithm: str
    digest: str

    def __post_init__(self) -> None:
        if self.algorithm != "sha256" or not _is_sha256_digest(self.digest):
            raise ValueError("invalid approval actor digest")

    def as_mapping(self) -> dict[str, object]:
        return {"algorithm": self.algorithm, "digest": self.digest}


@dataclass(frozen=True, slots=True)
class ApprovalReasonDigest:
    provided: bool
    algorithm: str | None
    digest: str | None

    def __post_init__(self) -> None:
        if not isinstance(self.provided, bool):
            raise ValueError("invalid approval reason digest")
        if self.provided:
            if self.algorithm != "sha256" or not _is_sha256_digest(self.digest):
                raise ValueError("invalid approval reason digest")
        elif self.algorithm is not None or self.digest is not None:
            raise ValueError("invalid approval reason digest")

    def as_mapping(self) -> dict[str, object]:
        return {
            "provided": self.provided,
            "algorithm": self.algorithm,
            "digest": self.digest,
        }


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
    status: ApprovalStatus = ApprovalStatus.PENDING
    decision: ApprovalDecision | None = None
    actor: ApprovalActorDigest | None = None
    reason: ApprovalReasonDigest | None = None
    decided_at: str | None = None

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int):
            raise ValueError("approval version must be a non-negative integer")
        if self.version < 0:
            raise ValueError("approval version must be a non-negative integer")
        try:
            status = ApprovalStatus(self.status)
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid approval status") from exc
        try:
            decision = (
                None if self.decision is None else ApprovalDecision(self.decision)
            )
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid approval decision") from exc
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "decision", decision)
        object.__setattr__(self, "risk_reasons", tuple(self.risk_reasons))
        actor = _coerce_actor_digest(self.actor)
        reason = _coerce_reason_digest(self.reason)
        object.__setattr__(self, "actor", actor)
        object.__setattr__(self, "reason", reason)

        if status is ApprovalStatus.PENDING:
            if (
                self.version != 0
                or decision is not None
                or actor is not None
                or reason is not None
                or self.decided_at is not None
            ):
                raise ValueError("pending approval fields are inconsistent")
            return
        if status in {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED}:
            if (
                self.version < 1
                or decision is None
                or decision.value != status.value
                or actor is None
                or reason is None
                or self.decided_at is None
            ):
                raise ValueError("decided approval fields are inconsistent")
            return
        if (
            self.version < 1
            or decision is not None
            or actor is not None
            or reason is not None
            or self.decided_at is None
        ):
            raise ValueError("invalidated approval fields are inconsistent")

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
        run_id: str,
        request_id: str,
        decision: ApprovalDecision | str,
        *,
        expected_version: int,
        binding_digest: str,
        actor: str,
        reason: str,
    ) -> ApprovalRequest:
        try:
            normalized_decision = ApprovalDecision(decision)
        except (TypeError, ValueError):
            raise ApprovalDecisionError("invalid_decision")

        occurred_at = self._clock.now()
        safe_actor = ApprovalActorDigest(
            algorithm="sha256",
            digest=_text_digest("approval-actor", actor),
        )
        reason_provided = bool(reason.strip())
        safe_reason = ApprovalReasonDigest(
            provided=reason_provided,
            algorithm="sha256" if reason_provided else None,
            digest=(
                _text_digest("approval-reason", reason)
                if reason_provided
                else None
            ),
        )
        with self._uow_factory() as uow:
            request = uow.approvals.get(run_id, request_id)
            if request is None:
                raise LookupError(f"unknown approval request {run_id}:{request_id}")
            if isinstance(request, LegacyApprovalRecord):
                raise ApprovalDecisionError("legacy_approval")
            run = uow.runs.get(request.run_id)
            if run is None:
                raise LookupError(f"unknown run {request.run_id}")
            if approval_recovery_required(uow, run):
                raise ApprovalDecisionError("approval_recovery_required")
            if request.version != expected_version:
                raise OptimisticLockError(
                    aggregate_type="approval",
                    identifier=f"{run_id}:{request_id}",
                    expected_revision=expected_version,
                    actual_revision=request.version,
                )
            if request.status != "pending":
                raise ApprovalDecisionError("already_decided")
            if request.binding_digest != binding_digest:
                raise ApprovalDecisionError("binding_mismatch")

            if run.state is not RunState.WAITING_APPROVAL:
                raise ApprovalDecisionError("run_not_waiting_approval")

            target_state = (
                RunState.RUNNING
                if normalized_decision is ApprovalDecision.APPROVED
                else self._rejection_target(uow, run)
            )
            decided = replace(
                request,
                version=request.version + 1,
                status=ApprovalStatus(normalized_decision.value),
                decision=normalized_decision,
                actor=safe_actor.as_mapping(),
                reason=safe_reason.as_mapping(),
                decided_at=occurred_at,
            )
            changed_run, run_event = run.transition(
                target_state,
                occurred_at=occurred_at,
            )
            decision_event = AuditEvent.approval_changed(
                run_id=run.run_id,
                step_id=request.step_id,
                event_type=f"approval.{normalized_decision.value}",
                occurred_at=occurred_at,
                previous_state=run.state.value,
                new_state=target_state.value,
                previous_revision=run.revision,
                revision=changed_run.revision,
                request_id=request.id,
                binding_digest=request.binding_digest,
                status=normalized_decision.value,
                actor=safe_actor.as_mapping(),
                reason=safe_reason.as_mapping(),
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
        has_compensable_effect = False
        for step in definition.steps:
            step_run = uow.steps.get(run.run_id, step.id)
            if step_run is None or step_run.state is not StepState.SUCCEEDED:
                continue
            effect_applied = any(
                attempt.status == "success"
                and getattr(attempt, "effect_applied", False)
                for attempt in uow.attempts.list(run.run_id, step_id=step.id)
            )
            if not effect_applied:
                continue
            if step.compensation_tool is None:
                return RunState.MANUAL_INTERVENTION
            has_compensable_effect = True
        if has_compensable_effect:
            return RunState.COMPENSATING
        return RunState.CANCELLED


def approval_recovery_required(uow: object, run: object) -> bool:
    definition = uow.definitions.get(run.definition_id, run.definition_version)
    if definition is None:
        raise LookupError(
            f"unknown definition {run.definition_id}@{run.definition_version}"
        )
    if any(
        uow.steps.get(run.run_id, step.id).state is StepState.RESULT_UNKNOWN
        for step in definition.steps
    ):
        return True
    return any(
        getattr(attempt, "status", None) == ErrorClass.RESULT_UNKNOWN.value
        or getattr(attempt, "error_class", None) == ErrorClass.RESULT_UNKNOWN.value
        for attempt in uow.attempts.list(run.run_id)
    )


def _text_digest(domain: str, value: str) -> str:
    if not isinstance(value, str):
        raise TypeError("approval text must be a string")
    return hashlib.sha256(f"{domain}\0{value}".encode("utf-8")).hexdigest()


def _is_sha256_digest(value: object) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def _coerce_actor_digest(value: object) -> ApprovalActorDigest | None:
    if value is None or isinstance(value, ApprovalActorDigest):
        return value
    if isinstance(value, Mapping):
        return ApprovalActorDigest(**dict(value))
    raise ValueError("approval actor must be a structured digest")


def _coerce_reason_digest(value: object) -> ApprovalReasonDigest | None:
    if value is None or isinstance(value, ApprovalReasonDigest):
        return value
    if isinstance(value, Mapping):
        return ApprovalReasonDigest(**dict(value))
    raise ValueError("approval reason must be a structured digest")
