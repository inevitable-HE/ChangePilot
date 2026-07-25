from __future__ import annotations

import os

import pytest

from changepilot.planning.adapters.models.deepseek import (
    DeepSeekConfig,
    DeepSeekModelGateway,
)
from changepilot.planning.application.model_gateway import (
    BudgetedModelGateway,
    InMemoryModelCache,
    InMemoryUsageRecorder,
    ModelBudget,
)
from changepilot.planning.ports.models import (
    ModelMessage,
    ModelRequest,
)


pytestmark = [
    pytest.mark.live_llm,
    pytest.mark.skipif(
        os.getenv("CHANGEPILOT_RUN_LIVE_LLM") != "1",
        reason="live DeepSeek test is opt-in",
    ),
    pytest.mark.skipif(
        not os.getenv("CHANGEPILOT_DEEPSEEK_API_KEY"),
        reason="DeepSeek API key is unavailable",
    ),
]


def test_deepseek_returns_one_small_json_object() -> None:
    gateway = BudgetedModelGateway(
        inner=DeepSeekModelGateway(DeepSeekConfig.from_env()),
        budget=ModelBudget(max_calls=1, max_total_tokens=512),
        cache=InMemoryModelCache(),
        usage=InMemoryUsageRecorder(),
        cache_namespace="deepseek:live-smoke-v1",
        max_retries=0,
    )
    response = gateway.generate(
        ModelRequest(
            request_id="live-smoke-1",
            messages=(
                ModelMessage(
                    role="system",
                    content="Return only a small JSON object.",
                ),
                ModelMessage(
                    role="user",
                    content='Return {"status":"ok"}.',
                ),
            ),
            response_schema_name="SmokeResponse",
            response_schema={
                "type": "object",
                "properties": {"status": {"type": "string"}},
                "required": ["status"],
                "additionalProperties": False,
            },
            prompt_version="live-smoke-v1",
            knowledge_snapshot_digest="none",
            max_output_tokens=64,
        )
    )

    assert response.payload.get("status") == "ok"
    assert response.usage.total_tokens >= 0
    assert gateway.calls_used == 1
