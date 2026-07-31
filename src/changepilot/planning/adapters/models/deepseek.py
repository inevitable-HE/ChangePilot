from __future__ import annotations

import json
import os
import time
from collections.abc import Mapping
from typing import Any

import openai
from openai import AsyncOpenAI, OpenAI
from pydantic import Field, SecretStr

from changepilot.planning.domain.failures import (
    PermanentModelError,
    TransientModelError,
)
from changepilot.planning.domain.models import PlanningBoundaryModel
from changepilot.planning.ports.models import (
    AsyncChatTurn,
    ChatToolCall,
    ChatToolDefinition,
    ModelRequest,
    ModelResponse,
    ModelUsage,
)


class DeepSeekConfig(PlanningBoundaryModel):
    api_key: SecretStr
    base_url: str = "https://api.deepseek.com"
    model: str = "deepseek-v4-flash"
    timeout_seconds: float = Field(gt=0, default=30.0)

    @classmethod
    def from_env(cls) -> DeepSeekConfig:
        api_key = os.getenv("CHANGEPILOT_DEEPSEEK_API_KEY")
        if not api_key:
            raise ValueError("CHANGEPILOT_DEEPSEEK_API_KEY is required")
        return cls(
            api_key=SecretStr(api_key),
            base_url=os.getenv(
                "CHANGEPILOT_LLM_BASE_URL",
                "https://api.deepseek.com",
            ),
            model=os.getenv("CHANGEPILOT_LLM_MODEL", "deepseek-v4-flash"),
            timeout_seconds=float(
                os.getenv("CHANGEPILOT_LLM_TIMEOUT_SECONDS", "30")
            ),
        )


class DeepSeekModelGateway:
    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client or OpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            max_retries=0,
        )

    def generate(self, request: ModelRequest) -> ModelResponse:
        started = time.perf_counter()
        try:
            result = self._client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": message.role, "content": message.content}
                    for message in request.messages
                ],
                response_format={"type": "json_object"},
                max_tokens=request.max_output_tokens,
                timeout=self._config.timeout_seconds,
            )
        except (
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APIConnectionError,
        ) as exc:
            raise TransientModelError("DeepSeek temporarily unavailable") from exc
        except openai.APIError as exc:
            raise PermanentModelError("DeepSeek request failed") from exc

        content = result.choices[0].message.content
        if not content:
            raise PermanentModelError("DeepSeek returned empty JSON content")
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise PermanentModelError("DeepSeek returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise PermanentModelError("DeepSeek JSON response must be an object")

        usage = result.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", input_tokens + output_tokens)
            or input_tokens + output_tokens
        )
        return ModelResponse(
            payload=payload,
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
            model=str(getattr(result, "model", self._config.model)),
            latency_ms=max(0, int((time.perf_counter() - started) * 1000)),
            finish_reason=str(result.choices[0].finish_reason or "unknown"),
        )


class DeepSeekAsyncChatModel:
    """OpenAI-compatible DeepSeek adapter with native function calling."""

    def __init__(
        self,
        config: DeepSeekConfig,
        *,
        client: Any | None = None,
    ) -> None:
        self._config = config
        self._client = client or AsyncOpenAI(
            api_key=config.api_key.get_secret_value(),
            base_url=config.base_url,
            max_retries=0,
        )

    async def complete(
        self,
        *,
        messages: tuple[Mapping[str, Any], ...],
        tools: tuple[ChatToolDefinition, ...],
        max_output_tokens: int,
    ) -> AsyncChatTurn:
        started = time.perf_counter()
        kwargs: dict[str, Any] = {
            "model": self._config.model,
            "messages": [dict(message) for message in messages],
            "response_format": {"type": "json_object"},
            "max_tokens": max_output_tokens,
            "timeout": self._config.timeout_seconds,
        }
        if tools:
            kwargs["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": tool.name,
                        "description": tool.description,
                        "parameters": tool.input_schema,
                    },
                }
                for tool in tools
            ]
            kwargs["tool_choice"] = "auto"
        try:
            result = await self._client.chat.completions.create(**kwargs)
        except (
            openai.APITimeoutError,
            openai.RateLimitError,
            openai.InternalServerError,
            openai.APIConnectionError,
        ) as exc:
            raise TransientModelError(
                "DeepSeek temporarily unavailable"
            ) from exc
        except openai.APIError as exc:
            raise PermanentModelError("DeepSeek request failed") from exc

        choice = result.choices[0]
        message = choice.message
        tool_calls = []
        for call in getattr(message, "tool_calls", ()) or ():
            try:
                arguments = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                raise PermanentModelError(
                    "DeepSeek returned invalid tool arguments"
                ) from exc
            if not isinstance(arguments, dict):
                raise PermanentModelError(
                    "DeepSeek tool arguments must be an object"
                )
            tool_calls.append(
                ChatToolCall(
                    tool_call_id=str(call.id),
                    name=str(call.function.name),
                    arguments=arguments,
                )
            )

        usage = result.usage
        input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
        output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
        total_tokens = int(
            getattr(usage, "total_tokens", input_tokens + output_tokens)
            or input_tokens + output_tokens
        )
        return AsyncChatTurn(
            content=message.content,
            tool_calls=tuple(tool_calls),
            usage=ModelUsage(
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                total_tokens=total_tokens,
            ),
            model=str(getattr(result, "model", self._config.model)),
            latency_ms=max(
                0,
                int((time.perf_counter() - started) * 1000),
            ),
            finish_reason=str(choice.finish_reason or "unknown"),
        )
