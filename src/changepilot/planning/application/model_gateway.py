from __future__ import annotations

import asyncio
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from pydantic import Field

from changepilot.planning.domain.failures import (
    ModelBudgetExceeded,
    TransientModelError,
)
from changepilot.planning.domain.models import PlanningBoundaryModel
from changepilot.planning.ports.models import (
    AsyncModelGateway,
    ModelGateway,
    ModelRequest,
    ModelResponse,
)
from changepilot.planning.ports.persistence import ModelCache, UsageRecorder


class ModelBudget(PlanningBoundaryModel):
    max_calls: int = Field(gt=0)
    max_total_tokens: int = Field(gt=0)
    max_estimated_cost_microunits: int | None = Field(default=None, gt=0)


class ModelPricing(PlanningBoundaryModel):
    input_microunits_per_million_tokens: int = Field(ge=0)
    output_microunits_per_million_tokens: int = Field(ge=0)


class RecordedModelUsage(PlanningBoundaryModel):
    request_digest: str
    model: str
    latency_ms: int
    input_tokens: int
    output_tokens: int
    total_tokens: int
    cache_hit: bool
    retry_count: int
    estimated_cost_microunits: int


@dataclass(slots=True)
class InMemoryModelCache:
    entries: dict[str, ModelResponse]

    def __init__(self) -> None:
        self.entries = {}

    def get(self, key: str) -> ModelResponse | None:
        return self.entries.get(key)

    def put(self, key: str, response: ModelResponse) -> None:
        self.entries[key] = response


@dataclass(slots=True)
class InMemoryUsageRecorder:
    records: list[RecordedModelUsage]

    def __init__(self) -> None:
        self.records = []

    def record(self, record: RecordedModelUsage) -> None:
        self.records.append(record)


class BudgetedModelGateway:
    def __init__(
        self,
        *,
        inner: ModelGateway,
        budget: ModelBudget,
        cache: ModelCache,
        usage: UsageRecorder,
        cache_namespace: str,
        pricing: ModelPricing | None = None,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._inner = inner
        self._budget = budget
        self._cache = cache
        self._usage = usage
        self._cache_namespace = cache_namespace
        self._pricing = pricing or ModelPricing(
            input_microunits_per_million_tokens=0,
            output_microunits_per_million_tokens=0,
        )
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._sleep = sleep
        self._calls = 0
        self._total_tokens = 0
        self._estimated_cost_microunits = 0

    @property
    def calls_used(self) -> int:
        return self._calls

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    def generate(self, request: ModelRequest) -> ModelResponse:
        request_digest = self._request_digest(request)
        if cached := self._cache.get(request_digest):
            response = cached.model_copy(update={"cache_hit": True})
            self._record(request_digest, response, retry_count=0)
            return response

        if self._calls >= self._budget.max_calls:
            raise ModelBudgetExceeded("model call budget exhausted")

        retry_count = 0
        while True:
            self._calls += 1
            try:
                response = self._inner.generate(request)
                break
            except TransientModelError:
                if retry_count >= self._max_retries:
                    raise
                retry_count += 1
                if self._calls >= self._budget.max_calls:
                    raise ModelBudgetExceeded("model call budget exhausted during retry")
                self._sleep(self._retry_delay_seconds)

        projected_tokens = self._total_tokens + response.usage.total_tokens
        if projected_tokens > self._budget.max_total_tokens:
            raise ModelBudgetExceeded("model token budget exhausted")
        response_cost = self._estimate_cost(response)
        projected_cost = self._estimated_cost_microunits + response_cost
        if (
            self._budget.max_estimated_cost_microunits is not None
            and projected_cost > self._budget.max_estimated_cost_microunits
        ):
            raise ModelBudgetExceeded("model cost budget exhausted")
        self._total_tokens = projected_tokens
        self._estimated_cost_microunits = projected_cost
        self._cache.put(request_digest, response)
        self._record(request_digest, response, retry_count=retry_count)
        return response

    def _request_digest(self, request: ModelRequest) -> str:
        payload = {
            "namespace": self._cache_namespace,
            "request": request.model_dump(mode="json"),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _record(
        self,
        request_digest: str,
        response: ModelResponse,
        *,
        retry_count: int,
    ) -> None:
        self._usage.record(
            RecordedModelUsage(
                request_digest=request_digest,
                model=response.model,
                latency_ms=response.latency_ms,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
                cache_hit=response.cache_hit,
                retry_count=retry_count,
                estimated_cost_microunits=(
                    0 if response.cache_hit else self._estimate_cost(response)
                ),
            )
        )

    def _estimate_cost(self, response: ModelResponse) -> int:
        numerator = (
            response.usage.input_tokens
            * self._pricing.input_microunits_per_million_tokens
            + response.usage.output_tokens
            * self._pricing.output_microunits_per_million_tokens
        )
        return (numerator + 999_999) // 1_000_000


class AsyncBudgetedModelGateway:
    """Async budget, retry, cache, and usage boundary for model calls."""

    def __init__(
        self,
        *,
        inner: AsyncModelGateway,
        budget: ModelBudget,
        cache: ModelCache,
        usage: UsageRecorder,
        cache_namespace: str,
        pricing: ModelPricing | None = None,
        max_retries: int = 1,
        retry_delay_seconds: float = 0.0,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries cannot be negative")
        self._inner = inner
        self._budget = budget
        self._cache = cache
        self._usage = usage
        self._cache_namespace = cache_namespace
        self._pricing = pricing or ModelPricing(
            input_microunits_per_million_tokens=0,
            output_microunits_per_million_tokens=0,
        )
        self._max_retries = max_retries
        self._retry_delay_seconds = retry_delay_seconds
        self._calls = 0
        self._total_tokens = 0
        self._estimated_cost_microunits = 0

    @property
    def calls_used(self) -> int:
        return self._calls

    @property
    def total_tokens(self) -> int:
        return self._total_tokens

    @property
    def traces(self) -> tuple[object, ...]:
        return tuple(getattr(self._inner, "traces", ()))

    async def generate(self, request: ModelRequest) -> ModelResponse:
        request_digest = self._request_digest(request)
        if cached := self._cache.get(request_digest):
            response = cached.model_copy(update={"cache_hit": True})
            self._record(request_digest, response, retry_count=0)
            return response

        if self._calls >= self._budget.max_calls:
            raise ModelBudgetExceeded("model call budget exhausted")

        retry_count = 0
        while True:
            self._calls += 1
            try:
                response = await self._inner.generate(request)
                break
            except TransientModelError:
                if retry_count >= self._max_retries:
                    raise
                retry_count += 1
                if self._calls >= self._budget.max_calls:
                    raise ModelBudgetExceeded(
                        "model call budget exhausted during retry"
                    )
                await asyncio.sleep(self._retry_delay_seconds)

        projected_tokens = self._total_tokens + response.usage.total_tokens
        if projected_tokens > self._budget.max_total_tokens:
            raise ModelBudgetExceeded("model token budget exhausted")
        response_cost = self._estimate_cost(response)
        projected_cost = self._estimated_cost_microunits + response_cost
        if (
            self._budget.max_estimated_cost_microunits is not None
            and projected_cost
            > self._budget.max_estimated_cost_microunits
        ):
            raise ModelBudgetExceeded("model cost budget exhausted")
        self._total_tokens = projected_tokens
        self._estimated_cost_microunits = projected_cost
        self._cache.put(request_digest, response)
        self._record(request_digest, response, retry_count=retry_count)
        return response

    def _request_digest(self, request: ModelRequest) -> str:
        payload = {
            "namespace": self._cache_namespace,
            "request": request.model_dump(mode="json"),
        }
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def _record(
        self,
        request_digest: str,
        response: ModelResponse,
        *,
        retry_count: int,
    ) -> None:
        self._usage.record(
            RecordedModelUsage(
                request_digest=request_digest,
                model=response.model,
                latency_ms=response.latency_ms,
                input_tokens=response.usage.input_tokens,
                output_tokens=response.usage.output_tokens,
                total_tokens=response.usage.total_tokens,
                cache_hit=response.cache_hit,
                retry_count=retry_count,
                estimated_cost_microunits=(
                    0 if response.cache_hit else self._estimate_cost(response)
                ),
            )
        )

    def _estimate_cost(self, response: ModelResponse) -> int:
        numerator = (
            response.usage.input_tokens
            * self._pricing.input_microunits_per_million_tokens
            + response.usage.output_tokens
            * self._pricing.output_microunits_per_million_tokens
        )
        return (numerator + 999_999) // 1_000_000
