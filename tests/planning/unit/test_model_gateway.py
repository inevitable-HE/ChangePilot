from __future__ import annotations

import pytest

from changepilot.planning.adapters.models.mock import MockModelGateway
from changepilot.planning.application.model_gateway import (
    BudgetedModelGateway,
    InMemoryModelCache,
    InMemoryUsageRecorder,
    ModelBudget,
    ModelPricing,
)
from changepilot.planning.domain.failures import (
    ModelBudgetExceeded,
    TransientModelError,
)
from changepilot.planning.ports.models import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(ModelMessage(role="user", content="plan upgrade"),),
        response_schema_name="ChangePlan",
        response_schema={"type": "object"},
        prompt_version="planning-v1",
        knowledge_snapshot_digest="knowledge-v1",
        max_output_tokens=500,
    )


def _response(total_tokens: int = 20) -> ModelResponse:
    return ModelResponse(
        payload={"plan_id": "plan-1"},
        usage=ModelUsage(
            input_tokens=total_tokens // 2,
            output_tokens=total_tokens - total_tokens // 2,
            total_tokens=total_tokens,
        ),
        model="mock-model",
        latency_ms=1,
        finish_reason="stop",
    )


def _gateway(
    scripted: list[ModelResponse | Exception],
    *,
    max_calls: int = 2,
    max_total_tokens: int = 100,
    max_retries: int = 1,
    max_estimated_cost_microunits: int | None = None,
    pricing: ModelPricing | None = None,
) -> tuple[BudgetedModelGateway, MockModelGateway, InMemoryUsageRecorder]:
    inner = MockModelGateway(scripted)
    usage = InMemoryUsageRecorder()
    gateway = BudgetedModelGateway(
        inner=inner,
        budget=ModelBudget(
            max_calls=max_calls,
            max_total_tokens=max_total_tokens,
            max_estimated_cost_microunits=max_estimated_cost_microunits,
        ),
        cache=InMemoryModelCache(),
        usage=usage,
        cache_namespace="mock:planning-v1",
        pricing=pricing,
        max_retries=max_retries,
    )
    return gateway, inner, usage


def test_same_request_hits_cache_without_new_model_call() -> None:
    gateway, inner, usage = _gateway([_response()])

    first = gateway.generate(_request())
    second = gateway.generate(_request())

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert inner.call_count == 1
    assert usage.records[-1].cache_hit is True


def test_call_budget_stops_before_second_distinct_request() -> None:
    gateway, _, _ = _gateway([_response()], max_calls=1)
    gateway.generate(_request())
    changed = _request().model_copy(update={"request_id": "request-2"})

    with pytest.raises(ModelBudgetExceeded, match="call budget"):
        gateway.generate(changed)


def test_token_budget_rejects_response_that_exceeds_limit() -> None:
    gateway, _, _ = _gateway([_response(101)], max_total_tokens=100)

    with pytest.raises(ModelBudgetExceeded, match="token budget"):
        gateway.generate(_request())


def test_transient_error_is_retried_once_and_recorded() -> None:
    gateway, inner, usage = _gateway(
        [TransientModelError("retry"), _response()],
        max_calls=2,
    )

    assert gateway.generate(_request()).payload["plan_id"] == "plan-1"
    assert inner.call_count == 2
    assert usage.records[0].retry_count == 1


def test_estimated_cost_budget_is_enforced() -> None:
    gateway, _, _ = _gateway(
        [_response(20)],
        max_estimated_cost_microunits=1,
        pricing=ModelPricing(
            input_microunits_per_million_tokens=100_000,
            output_microunits_per_million_tokens=100_000,
        ),
    )

    with pytest.raises(ModelBudgetExceeded, match="cost budget"):
        gateway.generate(_request())
