from __future__ import annotations

from types import SimpleNamespace

from pydantic import SecretStr

from changepilot.planning.adapters.models.deepseek import (
    DeepSeekConfig,
    DeepSeekModelGateway,
)
from changepilot.planning.ports.models import ModelMessage, ModelRequest


class _FakeCompletions:
    def __init__(self) -> None:
        self.kwargs: dict[str, object] | None = None

    def create(self, **kwargs: object) -> object:
        self.kwargs = kwargs
        return SimpleNamespace(
            model="deepseek-v4-flash",
            choices=(
                SimpleNamespace(
                    message=SimpleNamespace(content='{"plan_id":"plan-1"}'),
                    finish_reason="stop",
                ),
            ),
            usage=SimpleNamespace(
                prompt_tokens=10,
                completion_tokens=5,
                total_tokens=15,
            ),
        )


class _FakeClient:
    def __init__(self) -> None:
        self.completions = _FakeCompletions()
        self.chat = SimpleNamespace(completions=self.completions)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(
            ModelMessage(role="system", content="Return JSON."),
            ModelMessage(role="user", content="Plan an upgrade."),
        ),
        response_schema_name="ChangePlan",
        response_schema={"type": "object"},
        prompt_version="planning-v1",
        knowledge_snapshot_digest="knowledge-v1",
        max_output_tokens=500,
    )


def test_deepseek_adapter_requests_json_without_exposing_key() -> None:
    client = _FakeClient()
    config = DeepSeekConfig(api_key=SecretStr("super-secret"))
    gateway = DeepSeekModelGateway(config, client=client)

    response = gateway.generate(_request())

    assert response.payload == {"plan_id": "plan-1"}
    assert response.usage.total_tokens == 15
    assert client.completions.kwargs == {
        "model": "deepseek-v4-flash",
        "messages": [
            {"role": "system", "content": "Return JSON."},
            {"role": "user", "content": "Plan an upgrade."},
        ],
        "response_format": {"type": "json_object"},
        "max_tokens": 500,
        "timeout": 30.0,
    }
    assert "super-secret" not in repr(config)
