from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Protocol

from pydantic import (
    BaseModel,
    ConfigDict,
    SecretStr,
    ValidationInfo,
    field_serializer,
    field_validator,
    model_validator,
)


def _freeze_value(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_value(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def normalize_sensitive_path(path: str) -> tuple[str, ...]:
    if not isinstance(path, str) or not path.strip():
        raise ValueError("sensitive paths must be non-empty dotted strings")
    segments = tuple(segment.strip() for segment in path.split("."))
    if any(not segment for segment in segments):
        raise ValueError("sensitive paths cannot contain empty segments")
    return segments


class BoundaryModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        arbitrary_types_allowed=True,
    )


class ToolRisk(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ToolIdempotency(str, Enum):
    SUPPORTED = "supported"
    PROBE_ONLY = "probe_only"
    NONE = "none"


class ToolExecutionPhase(str, Enum):
    FORWARD = "forward"
    PROBE = "probe"
    COMPENSATION = "compensation"


class ToolProbeStatus(str, Enum):
    FOUND = "found"
    NOT_FOUND = "not_found"
    INCONCLUSIVE = "inconclusive"


class SecretRef(BoundaryModel):
    provider: str
    name: SecretStr

    @field_validator("provider")
    @classmethod
    def _validate_provider(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("provider must be a non-empty string")
        return value

    @field_serializer("name", when_used="always")
    def _serialize_name(self, value: SecretStr) -> str:
        return "[REDACTED]"

    def reveal(self) -> str:
        return self.name.get_secret_value()

    def __repr__(self) -> str:
        return f"SecretRef(provider={self.provider!r}, name='[REDACTED]')"

    def __str__(self) -> str:
        return self.__repr__()


class ToolDescriptor(BoundaryModel):
    name: str
    version: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    risk: ToolRisk
    idempotency: ToolIdempotency
    default_timeout_seconds: int
    max_timeout_seconds: int
    sensitive_argument_paths: tuple[str, ...] = ()

    @field_validator("name", "version")
    @classmethod
    def _validate_identity(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("input_model", "output_model")
    @classmethod
    def _validate_boundary_model_type(
        cls,
        value: type[BaseModel],
        info: ValidationInfo,
    ) -> type[BaseModel]:
        if not isinstance(value, type) or not issubclass(value, BaseModel):
            raise ValueError(f"{info.field_name} must be a Pydantic model type")

        config = value.model_config
        if config.get("strict") is not True:
            raise ValueError(f"{info.field_name} must declare model_config.strict=True")
        if config.get("frozen") is not True:
            raise ValueError(f"{info.field_name} must declare model_config.frozen=True")
        if config.get("extra") != "forbid":
            raise ValueError(f"{info.field_name} must declare model_config.extra='forbid'")

        return value

    @field_validator("default_timeout_seconds", "max_timeout_seconds")
    @classmethod
    def _validate_timeout(cls, value: int, info: ValidationInfo) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError(f"{info.field_name} must be a positive integer")
        return value

    @field_validator("sensitive_argument_paths")
    @classmethod
    def _validate_sensitive_paths(
        cls,
        value: tuple[str, ...],
    ) -> tuple[str, ...]:
        return tuple(".".join(normalize_sensitive_path(path)) for path in value)

    @model_validator(mode="after")
    def _validate_timeout_window(self) -> ToolDescriptor:
        if self.max_timeout_seconds < self.default_timeout_seconds:
            raise ValueError(
                "max_timeout_seconds must be greater than or equal to default_timeout_seconds"
            )
        return self


class ToolExecutionContext(BoundaryModel):
    run_id: str
    step_id: str
    attempt_number: int
    phase: ToolExecutionPhase
    logical_idempotency_key: str
    deadline: datetime
    metadata: Mapping[str, Any] = MappingProxyType({})

    @field_validator("run_id", "step_id", "logical_idempotency_key")
    @classmethod
    def _validate_non_empty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("attempt_number")
    @classmethod
    def _validate_attempt_number(cls, value: int) -> int:
        if isinstance(value, bool) or value <= 0:
            raise ValueError("attempt_number must be a positive integer")
        return value

    @field_validator("deadline")
    @classmethod
    def _validate_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def _freeze_metadata(cls, value: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise ValueError("metadata must be a mapping")
        return _freeze_value(dict(value))


class RecoveryQuery(BoundaryModel):
    run_id: str
    step_id: str
    phase: ToolExecutionPhase
    logical_idempotency_key: str
    deadline: datetime
    metadata: Mapping[str, Any] = MappingProxyType({})

    @field_validator("run_id", "step_id", "logical_idempotency_key")
    @classmethod
    def _validate_non_empty(cls, value: str, info: ValidationInfo) -> str:
        if not value.strip():
            raise ValueError(f"{info.field_name} must be a non-empty string")
        return value

    @field_validator("deadline")
    @classmethod
    def _validate_deadline(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("deadline must be timezone-aware")
        return value

    @field_validator("metadata", mode="before")
    @classmethod
    def _freeze_metadata(cls, value: Mapping[str, Any] | None) -> Mapping[str, Any]:
        if value is None:
            return MappingProxyType({})
        if not isinstance(value, Mapping):
            raise ValueError("metadata must be a mapping")
        return _freeze_value(dict(value))


class ToolExecutionResult(BoundaryModel):
    output: BaseModel
    effect_applied: bool


class ToolProbeResult(BoundaryModel):
    status: ToolProbeStatus
    output: BaseModel | None = None

    @model_validator(mode="after")
    def _validate_output_presence(self) -> ToolProbeResult:
        if self.status is ToolProbeStatus.FOUND and self.output is None:
            raise ValueError("output is required when probe status is FOUND")
        if self.status is ToolProbeStatus.NOT_FOUND and self.output is not None:
            raise ValueError("output must be omitted when probe status is NOT_FOUND")
        return self


class Tool(Protocol):
    descriptor: ToolDescriptor

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: BaseModel,
    ) -> ToolExecutionResult:
        """Execute the tool using validated arguments."""

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        """Probe for an existing effect using the logical idempotency key."""


class ToolCatalog(Protocol):
    def contains(self, name: str, version: str) -> bool:
        """Return whether the tool version is registered."""

    def validate_arguments(
        self,
        name: str,
        version: str,
        arguments: dict[str, Any],
    ) -> None:
        """Raise when the provided arguments are not valid for the tool."""
