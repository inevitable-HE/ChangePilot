from __future__ import annotations

from enum import StrEnum


class ErrorClass(StrEnum):
    RETRYABLE = "retryable"
    PERMANENT = "permanent"
    RESULT_UNKNOWN = "result_unknown"
    INTERNAL_CONSISTENCY = "internal_consistency"


class DomainError(ValueError):
    def __init__(self, message: str, *, error_class: ErrorClass) -> None:
        super().__init__(message)
        self.message = message
        self.error_class = error_class


class InvalidTransition(DomainError):
    def __init__(
        self,
        aggregate_type: str,
        current_state: str,
        target_state: str,
    ) -> None:
        super().__init__(
            f"invalid {aggregate_type} transition: {current_state} -> {target_state}",
            error_class=ErrorClass.INTERNAL_CONSISTENCY,
        )
        self.aggregate_type = aggregate_type
        self.current_state = current_state
        self.target_state = target_state
