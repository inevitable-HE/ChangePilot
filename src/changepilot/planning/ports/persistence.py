from __future__ import annotations

from typing import Protocol

from changepilot.planning.ports.models import ModelResponse


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
