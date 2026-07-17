from __future__ import annotations

from dataclasses import dataclass


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
