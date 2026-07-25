from __future__ import annotations

from typing import Protocol, TYPE_CHECKING

from changepilot.planning.ports.models import ModelResponse

if TYPE_CHECKING:
    from changepilot.planning.domain.models import (
        ChangePlan,
        ChangeRequest,
        PlanningResult,
        PreparedWorkflow,
    )


class ModelCache(Protocol):
    def get(self, key: str) -> ModelResponse | None:
        """Return a cached response by its opaque digest."""

    def put(self, key: str, response: ModelResponse) -> None:
        """Persist a response under its opaque digest."""


class UsageRecorder(Protocol):
    def record(self, record: ModelUsageRecord) -> None:
        """Persist non-sensitive model usage metadata."""


class ModelUsageRecord(Protocol):
    request_digest: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit: bool
    retry_count: int
    estimated_cost_microunits: int


class PlanningRepository(Protocol):
    def create_session(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> None:
        """Create one planning session."""

    def get_request(self, session_id: str) -> ChangeRequest:
        """Return the current normalized request."""

    def update_request(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> None:
        """Replace a request after clarification."""

    def next_attempt_number(self, session_id: str) -> int:
        """Return the next monotonically increasing attempt number."""

    def next_plan_version(self, session_id: str) -> int:
        """Return the next monotonically increasing plan version."""

    def save_attempt(
        self,
        session_id: str,
        attempt_no: int,
        request: ChangeRequest,
        result: PlanningResult,
    ) -> None:
        """Persist an observable planning attempt."""

    def save_plan(self, session_id: str, plan: ChangePlan) -> None:
        """Persist a new latest plan and invalidate older versions."""

    def get_latest_plan(self, session_id: str) -> ChangePlan:
        """Return the latest plan for a session."""

    def get_latest_result(self, session_id: str) -> PlanningResult:
        """Return the latest planning result and its evidence."""

    def bind_prepared_workflow(
        self,
        session_id: str,
        prepared: PreparedWorkflow,
    ) -> None:
        """Record the workflow identity derived from the latest plan."""
