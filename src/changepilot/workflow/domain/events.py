from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class AuditEvent:
    run_id: str
    step_id: str | None
    event_type: str
    occurred_at: str
    previous_state: str
    new_state: str
    previous_revision: int
    revision: int
    attempt_id: str | None = None
    attempt_no: int | None = None
    phase: str | None = None
    error_class: str | None = None
    summary: Any | None = None

    @classmethod
    def run_state_changed(
        cls,
        *,
        run_id: str,
        previous_state: str,
        new_state: str,
        occurred_at: str,
        previous_revision: int,
        revision: int,
    ) -> "AuditEvent":
        return cls(
            run_id=run_id,
            step_id=None,
            event_type="run.state_changed",
            occurred_at=occurred_at,
            previous_state=previous_state,
            new_state=new_state,
            previous_revision=previous_revision,
            revision=revision,
        )

    @classmethod
    def step_state_changed(
        cls,
        *,
        run_id: str,
        step_id: str,
        previous_state: str,
        new_state: str,
        occurred_at: str,
        previous_revision: int,
        revision: int,
    ) -> "AuditEvent":
        return cls(
            run_id=run_id,
            step_id=step_id,
            event_type="step.state_changed",
            occurred_at=occurred_at,
            previous_state=previous_state,
            new_state=new_state,
            previous_revision=previous_revision,
            revision=revision,
        )

    @classmethod
    def tool_attempt_started(
        cls,
        *,
        run_id: str,
        step_id: str,
        attempt_id: str,
        attempt_no: int,
        phase: str,
        occurred_at: str,
        state: str,
        revision: int,
    ) -> "AuditEvent":
        return cls(
            run_id=run_id,
            step_id=step_id,
            event_type="tool_attempt_started",
            occurred_at=occurred_at,
            previous_state=state,
            new_state=state,
            previous_revision=revision,
            revision=revision,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            phase=phase,
            summary={"status": "running"},
        )

    @classmethod
    def tool_attempt_completed(
        cls,
        *,
        run_id: str,
        step_id: str,
        attempt_id: str,
        attempt_no: int,
        phase: str,
        occurred_at: str,
        state: str,
        revision: int,
        error_class: str | None,
        summary: object,
    ) -> "AuditEvent":
        return cls(
            run_id=run_id,
            step_id=step_id,
            event_type="tool_attempt_completed",
            occurred_at=occurred_at,
            previous_state=state,
            new_state=state,
            previous_revision=revision,
            revision=revision,
            attempt_id=attempt_id,
            attempt_no=attempt_no,
            phase=phase,
            error_class=error_class,
            summary=summary,
        )

    @classmethod
    def approval_changed(
        cls,
        *,
        run_id: str,
        step_id: str,
        event_type: str,
        occurred_at: str,
        previous_state: str,
        new_state: str,
        previous_revision: int,
        revision: int,
        request_id: str,
        binding_digest: str,
        status: str,
        actor: str | None = None,
        reason: str | None = None,
        redacted_arguments: object | None = None,
    ) -> "AuditEvent":
        summary = {
            "request_id": request_id,
            "binding_digest": binding_digest,
            "status": status,
        }
        if actor is not None:
            summary["actor"] = actor
        if reason is not None:
            summary["reason"] = reason
        if redacted_arguments is not None:
            summary["arguments"] = redacted_arguments
        return cls(
            run_id=run_id,
            step_id=step_id,
            event_type=event_type,
            occurred_at=occurred_at,
            previous_state=previous_state,
            new_state=new_state,
            previous_revision=previous_revision,
            revision=revision,
            summary=summary,
        )
