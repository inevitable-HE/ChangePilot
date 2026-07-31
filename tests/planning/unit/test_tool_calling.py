from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping

import pytest
from pydantic import BaseModel, ConfigDict

from changepilot.planning.application.tool_calling import (
    AsyncToolCallingModelGateway,
    ReadOnlyToolRegistry,
)
from changepilot.planning.domain.failures import PermanentModelError
from changepilot.planning.ports.models import (
    AsyncChatTurn,
    ChatToolCall,
    ChatToolDefinition,
    ModelMessage,
    ModelRequest,
    ModelUsage,
)


class _ToolInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    pull_request_url: str


class _PullRequestTool:
    input_model = _ToolInput
    definition = ChatToolDefinition(
        name="github.inspect_pull_request",
        description="Inspect a pull request.",
        input_schema=_ToolInput.model_json_schema(),
    )

    async def execute(
        self,
        arguments: BaseModel,
    ) -> Mapping[str, object]:
        assert isinstance(arguments, _ToolInput)
        return {"title": "Add migration", "checks": ["test:success"]}


class _ScriptedChatModel:
    def __init__(self, turns: list[AsyncChatTurn]) -> None:
        self.turns = turns
        self.messages: list[tuple[Mapping[str, object], ...]] = []

    async def complete(
        self,
        *,
        messages: tuple[Mapping[str, object], ...],
        tools: tuple[ChatToolDefinition, ...],
        max_output_tokens: int,
    ) -> AsyncChatTurn:
        assert tools[0].name == "github.inspect_pull_request"
        assert max_output_tokens == 500
        self.messages.append(messages)
        return self.turns.pop(0)


def _usage() -> ModelUsage:
    return ModelUsage(input_tokens=5, output_tokens=3, total_tokens=8)


def _request() -> ModelRequest:
    return ModelRequest(
        request_id="request-1",
        messages=(
            ModelMessage(role="system", content="Use tools, then JSON."),
            ModelMessage(
                role="user",
                content=(
                    '{"pull_request_url":'
                    '"https://github.com/acme/orders/pull/7"}'
                ),
            ),
        ),
        response_schema_name="ChangePlan",
        response_schema={"type": "object"},
        prompt_version="planning-v2",
        knowledge_snapshot_digest="snapshot-1",
        max_output_tokens=500,
    )


def test_native_tool_result_is_returned_to_model_before_final_json() -> None:
    model = _ScriptedChatModel(
        [
            AsyncChatTurn(
                tool_calls=(
                    ChatToolCall(
                        tool_call_id="call-1",
                        name="github.inspect_pull_request",
                        arguments={
                            "pull_request_url": (
                                "https://github.com/acme/orders/pull/7"
                            )
                        },
                    ),
                ),
                usage=_usage(),
                model="mock-chat",
                latency_ms=2,
                finish_reason="tool_calls",
            ),
            AsyncChatTurn(
                content='{"plan_id":"plan-1"}',
                usage=_usage(),
                model="mock-chat",
                latency_ms=3,
                finish_reason="stop",
            ),
        ]
    )
    gateway = AsyncToolCallingModelGateway(
        model=model,
        tools=ReadOnlyToolRegistry((_PullRequestTool(),)),
    )

    response = asyncio.run(gateway.generate(_request()))

    assert response.payload == {"plan_id": "plan-1"}
    assert response.usage.total_tokens == 16
    tool_message = model.messages[1][-1]
    assert tool_message["role"] == "tool"
    assert json.loads(str(tool_message["content"]))["title"] == "Add migration"
    assert gateway.traces[0].status == "succeeded"


def test_unregistered_model_tool_call_is_rejected() -> None:
    model = _ScriptedChatModel(
        [
            AsyncChatTurn(
                tool_calls=(
                    ChatToolCall(
                        tool_call_id="call-1",
                        name="shell.execute",
                        arguments={},
                    ),
                ),
                usage=_usage(),
                model="mock-chat",
                latency_ms=1,
                finish_reason="tool_calls",
            )
        ]
    )
    gateway = AsyncToolCallingModelGateway(
        model=model,
        tools=ReadOnlyToolRegistry((_PullRequestTool(),)),
    )

    with pytest.raises(PermanentModelError, match="unregistered tool"):
        asyncio.run(gateway.generate(_request()))
