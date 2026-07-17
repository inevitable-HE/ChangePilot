from __future__ import annotations

from enum import StrEnum


class RunState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    CANCELLED = "cancelled"
    MANUAL_INTERVENTION = "manual_intervention"


class StepState(StrEnum):
    PENDING = "pending"
    READY = "ready"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    RESULT_UNKNOWN = "result_unknown"
    MANUAL_INTERVENTION = "manual_intervention"
