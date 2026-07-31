from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, ValidationError

from changepilot.planning.domain.failures import PermanentModelError
from changepilot.planning.ports.models import (
    AsyncChatModel,
    ChatToolDefinition,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class ReadOnlyTool(Protocol):
    definition: ChatToolDefinition
    input_model: type[BaseModel]

    async def execute(self, arguments: BaseModel) -> Mapping[str, Any]:
        """Read external context without producing a side effect."""


class ToolCallTrace(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    round: int
    tool_call_id: str
    tool_name: str
    status: str
    latency_ms: int
    arguments: dict[str, Any]
    output_summary: str


class ReadOnlyToolRegistry:
    def __init__(self, tools: tuple[ReadOnlyTool, ...] = ()) -> None:
        self._tools: dict[str, ReadOnlyTool] = {}
        for tool in tools:
            name = tool.definition.name
            if name in self._tools:
                raise ValueError(f"duplicate read-only tool {name!r}")
            self._tools[name] = tool

    def definitions(self) -> tuple[ChatToolDefinition, ...]:
        return tuple(
            self._tools[name].definition for name in sorted(self._tools)
        )

    def resolve(self, name: str) -> ReadOnlyTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise PermanentModelError(
                f"model requested unregistered tool {name!r}"
            ) from exc


class AsyncToolCallingModelGateway:
    """Run a bounded native tool-calling loop around an async chat model."""

    def __init__(
        self,
        *,
        model: AsyncChatModel,
        tools: ReadOnlyToolRegistry,
        max_tool_rounds: int = 2,
    ) -> None:
        if max_tool_rounds < 0:
            raise ValueError("max_tool_rounds cannot be negative")
        self._model = model
        self._tools = tools
        self._max_tool_rounds = max_tool_rounds
        self._traces: list[ToolCallTrace] = []

    @property
    def traces(self) -> tuple[ToolCallTrace, ...]:
        return tuple(self._traces)

    async def generate(self, request: ModelRequest) -> ModelResponse:
        messages: list[Mapping[str, Any]] = [
            {"role": message.role, "content": message.content}
            for message in request.messages
        ]
        total_input = 0
        total_output = 0
        total_tokens = 0
        total_latency = 0
        model_name = "unknown"
        finish_reason = "unknown"

        for round_number in range(1, self._max_tool_rounds + 2):
            turn = await self._model.complete(
                messages=tuple(messages),
                tools=self._tools.definitions(),
                max_output_tokens=request.max_output_tokens,
            )
            total_input += turn.usage.input_tokens
            total_output += turn.usage.output_tokens
            total_tokens += turn.usage.total_tokens
            total_latency += turn.latency_ms
            model_name = turn.model
            finish_reason = turn.finish_reason

            if turn.tool_calls:
                if round_number > self._max_tool_rounds:
                    raise PermanentModelError(
                        "native tool-calling round limit exceeded"
                    )
                messages.append(
                    {
                        "role": "assistant",
                        "content": turn.content,
                        "tool_calls": [
                            {
                                "id": call.tool_call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(
                                        call.arguments,
                                        ensure_ascii=False,
                                        sort_keys=True,
                                    ),
                                },
                            }
                            for call in turn.tool_calls
                        ],
                    }
                )
                for call in turn.tool_calls:
                    output = await self._execute_tool(
                        round_number=round_number,
                        tool_call_id=call.tool_call_id,
                        name=call.name,
                        arguments=call.arguments,
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.tool_call_id,
                            "content": json.dumps(
                                output,
                                ensure_ascii=False,
                                sort_keys=True,
                            ),
                        }
                    )
                continue

            if not turn.content:
                raise PermanentModelError(
                    "model returned neither JSON content nor tool calls"
                )
            try:
                payload = json.loads(turn.content)
            except json.JSONDecodeError as exc:
                raise PermanentModelError(
                    "model returned invalid JSON after tool calling"
                ) from exc
            if not isinstance(payload, dict):
                raise PermanentModelError(
                    "model JSON response must be an object"
                )
            return ModelResponse(
                payload=payload,
                usage=ModelUsage(
                    input_tokens=total_input,
                    output_tokens=total_output,
                    total_tokens=total_tokens,
                ),
                model=model_name,
                latency_ms=total_latency,
                finish_reason=finish_reason,
            )

        raise PermanentModelError("model did not produce a final plan")

    async def _execute_tool(
        self,
        *,
        round_number: int,
        tool_call_id: str,
        name: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        tool = self._tools.resolve(name)
        started = time.perf_counter()
        try:
            validated = tool.input_model.model_validate(arguments)
            output = dict(await tool.execute(validated))
        except (ValidationError, OSError, ValueError) as exc:
            latency_ms = max(
                0,
                int((time.perf_counter() - started) * 1000),
            )
            self._traces.append(
                ToolCallTrace(
                    round=round_number,
                    tool_call_id=tool_call_id,
                    tool_name=name,
                    status="failed",
                    latency_ms=latency_ms,
                    arguments=arguments,
                    output_summary=str(exc)[:500],
                )
            )
            raise PermanentModelError(
                f"read-only tool {name!r} failed"
            ) from exc
        latency_ms = max(0, int((time.perf_counter() - started) * 1000))
        summary = json.dumps(
            output,
            ensure_ascii=False,
            sort_keys=True,
        )[:500]
        self._traces.append(
            ToolCallTrace(
                round=round_number,
                tool_call_id=tool_call_id,
                tool_name=name,
                status="succeeded",
                latency_ms=latency_ms,
                arguments=arguments,
                output_summary=summary,
            )
        )
        return output
