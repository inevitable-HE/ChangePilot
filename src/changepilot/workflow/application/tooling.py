from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from pydantic import BaseModel, ValidationError

from changepilot.workflow.ports.tools import (
    SecretRef,
    Tool,
    ToolDescriptor,
    ToolExecutionPhase,
    normalize_sensitive_path,
)


REDACTED = "[REDACTED]"
_COMMON_SENSITIVE_KEYS = frozenset(
    {
        "access_key",
        "api_key",
        "apikey",
        "authorization",
        "password",
        "secret",
        "secret_key",
        "token",
    }
)


class ToolRegistry:
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._tools: dict[tuple[str, str], Tool] = {}
        for tool in tools:
            self.register(tool)

    def register(
        self,
        tool: Tool,
        *,
        descriptor: ToolDescriptor | None = None,
    ) -> ToolRegistry:
        expected = descriptor or tool.descriptor
        if expected != tool.descriptor:
            raise ValueError("descriptor/tool mismatch")
        key = (expected.name, expected.version)
        if key in self._tools:
            raise ValueError(f"duplicate tool registration for {expected.name}@{expected.version}")
        self._tools[key] = tool
        return self

    def contains(self, name: str, version: str) -> bool:
        return (name, version) in self._tools

    def resolve(self, name: str, version: str) -> Tool:
        try:
            return self._tools[(name, version)]
        except KeyError as exc:
            raise LookupError(f"unknown tool {name}@{version}") from exc

    def descriptor_for(self, name: str, version: str) -> ToolDescriptor:
        return self.resolve(name, version).descriptor

    def validate_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> None:
        self.coerce_arguments(name, version, arguments)

    def coerce_arguments(
        self,
        name: str,
        version: str,
        arguments: Mapping[str, Any],
    ) -> BaseModel:
        descriptor = self.descriptor_for(name, version)
        payload = self._require_mapping(arguments, label="arguments")
        self._reject_unexpected_fields(payload, descriptor.input_model, label="arguments")
        return self._validate_model(descriptor.input_model, payload, label="arguments")

    def validate_output(
        self,
        name: str,
        version: str,
        output: Mapping[str, Any] | BaseModel,
    ) -> BaseModel:
        descriptor = self.descriptor_for(name, version)
        payload = self._coerce_payload(output, label="output")
        self._reject_unexpected_fields(payload, descriptor.output_model, label="output")
        return self._validate_model(descriptor.output_model, payload, label="output")

    @staticmethod
    def _require_mapping(value: Mapping[str, Any], *, label: str) -> dict[str, Any]:
        if not isinstance(value, Mapping):
            raise ValueError(f"{label} must be a mapping")
        return dict(value)

    @staticmethod
    def _coerce_payload(
        value: Mapping[str, Any] | BaseModel,
        *,
        label: str,
    ) -> dict[str, Any]:
        if isinstance(value, BaseModel):
            return value.model_dump(mode="python")
        return ToolRegistry._require_mapping(value, label=label)

    @staticmethod
    def _reject_unexpected_fields(
        payload: Mapping[str, Any],
        model_type: type[BaseModel],
        *,
        label: str,
    ) -> None:
        unexpected = sorted(set(payload) - set(model_type.model_fields))
        if unexpected:
            raise ValueError(f"{label} contains undeclared fields: {unexpected}")

    @staticmethod
    def _validate_model(
        model_type: type[BaseModel],
        payload: Mapping[str, Any],
        *,
        label: str,
    ) -> BaseModel:
        try:
            return model_type.model_validate(payload)
        except ValidationError as exc:
            raise ValueError(f"invalid {label}: {exc}") from exc


def logical_idempotency_key(
    *,
    run_id: str,
    step_id: str,
    tool_name: str,
    tool_version: str,
    phase: ToolExecutionPhase,
) -> str:
    payload = {
        "phase": _require_identity(phase.value, field_name="phase"),
        "run_id": _require_identity(run_id, field_name="run_id"),
        "step_id": _require_identity(step_id, field_name="step_id"),
        "tool": {
            "name": _require_identity(tool_name, field_name="tool_name"),
            "version": _require_identity(tool_version, field_name="tool_version"),
        },
    }
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def redact(
    value: object,
    *,
    sensitive_paths: Sequence[str] = (),
) -> object:
    normalized_paths = {normalize_sensitive_path(path) for path in sensitive_paths}
    return _redact_value(value, path=(), sensitive_paths=normalized_paths)


def _redact_value(
    value: object,
    *,
    path: tuple[str, ...],
    sensitive_paths: set[tuple[str, ...]],
) -> object:
    if path in sensitive_paths:
        return REDACTED
    if isinstance(value, SecretRef):
        return REDACTED
    if isinstance(value, BaseModel):
        output: dict[str, object] = {}
        for field_name in value.__class__.model_fields:
            child_path = path + (field_name,)
            field_value = getattr(value, field_name)
            if _is_sensitive_segment(field_name) or child_path in sensitive_paths:
                output[field_name] = REDACTED
            else:
                output[field_name] = _redact_value(
                    field_value,
                    path=child_path,
                    sensitive_paths=sensitive_paths,
                )
        return output
    if isinstance(value, Exception):
        return {
            "type": value.__class__.__name__,
            "args": [
                _redact_exception_argument(
                    argument,
                    path=path + ("args", str(index)),
                    sensitive_paths=sensitive_paths,
                )
                for index, argument in enumerate(value.args)
            ],
        }
    if isinstance(value, Mapping):
        output = {}
        for raw_key, raw_value in value.items():
            key = str(raw_key)
            child_path = path + (key,)
            if _is_sensitive_segment(key) or child_path in sensitive_paths:
                output[key] = REDACTED
            else:
                output[key] = _redact_value(
                    raw_value,
                    path=child_path,
                    sensitive_paths=sensitive_paths,
                )
        return output
    if isinstance(value, list):
        return [
            _redact_value(
                item,
                path=path + (str(index),),
                sensitive_paths=sensitive_paths,
            )
            for index, item in enumerate(value)
        ]
    if isinstance(value, tuple):
        return tuple(
            _redact_value(
                item,
                path=path + (str(index),),
                sensitive_paths=sensitive_paths,
            )
            for index, item in enumerate(value)
        )
    return value


def _is_sensitive_segment(segment: str) -> bool:
    return segment.casefold() in _COMMON_SENSITIVE_KEYS


def _redact_exception_argument(
    value: object,
    *,
    path: tuple[str, ...],
    sensitive_paths: set[tuple[str, ...]],
) -> object:
    if isinstance(value, (str, bytes, bytearray)):
        return REDACTED
    return _redact_value(value, path=path, sensitive_paths=sensitive_paths)


def _require_identity(value: str, *, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value
