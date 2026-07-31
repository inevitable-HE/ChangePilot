from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal, Protocol

from pydantic import Field

from changepilot.planning.domain.models import PlanningBoundaryModel


class ModelMessage(PlanningBoundaryModel):
    role: Literal["system", "user", "assistant"]
    content: str


class ModelRequest(PlanningBoundaryModel):
    request_id: str
    messages: tuple[ModelMessage, ...]
    response_schema_name: str
    response_schema: dict[str, Any]
    prompt_version: str
    knowledge_snapshot_digest: str
    max_output_tokens: int = Field(gt=0)


class ModelUsage(PlanningBoundaryModel):
    input_tokens: int = Field(ge=0)
    output_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class ModelResponse(PlanningBoundaryModel):
    payload: dict[str, Any]
    usage: ModelUsage
    model: str
    latency_ms: int = Field(ge=0)
    finish_reason: str
    cache_hit: bool = False


class ModelGateway(Protocol):
    def generate(self, request: ModelRequest) -> ModelResponse:
        """Return structured output without executing tools."""


class AsyncModelGateway(Protocol):
    async def generate(self, request: ModelRequest) -> ModelResponse:
        """Return structured output through a non-blocking model path."""


class ChatToolDefinition(PlanningBoundaryModel):
    name: str
    description: str
    input_schema: dict[str, Any]


class ChatToolCall(PlanningBoundaryModel):
    tool_call_id: str
    name: str
    arguments: dict[str, Any]


class AsyncChatTurn(PlanningBoundaryModel):
    content: str | None = None
    tool_calls: tuple[ChatToolCall, ...] = ()
    usage: ModelUsage
    model: str
    latency_ms: int = Field(ge=0)
    finish_reason: str


class AsyncChatModel(Protocol):
    async def complete(
        self,
        *,
        messages: tuple[Mapping[str, Any], ...],
        tools: tuple[ChatToolDefinition, ...],
        max_output_tokens: int,
    ) -> AsyncChatTurn:
        """Return one assistant turn, including native tool calls."""
