from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from changepilot.workflow.domain.validation import (
    DefinitionValidationError,
    find_cycle,
    require_mapping,
    require_positive_int,
    require_string,
    require_string_sequence,
)
from changepilot.workflow.ports.tools import ToolCatalog


MAX_STEPS = 100
MAX_EDGES = 1000
DEFAULT_RISK = "low"


@dataclass(frozen=True, slots=True)
class ToolReference:
    name: str
    version: str


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    max_attempts: int = 3
    initial_backoff_seconds: float = 1.0

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 10:
            raise DefinitionValidationError(
                "max_attempts must be within 1..10",
                field="max_attempts",
            )
        if self.initial_backoff_seconds <= 0:
            raise DefinitionValidationError(
                "initial_backoff_seconds must be positive",
                field="initial_backoff_seconds",
            )


@dataclass(frozen=True, slots=True)
class StepDefinition:
    id: str
    tool: ToolReference
    arguments: MappingProxyType
    depends_on: tuple[str, ...] = ()
    risk: str = DEFAULT_RISK
    retry: RetryPolicy = field(default_factory=RetryPolicy)
    compensation_tool: ToolReference | None = None


@dataclass(frozen=True, slots=True)
class WorkflowDefinition:
    definition_id: str
    version: int
    steps: tuple[StepDefinition, ...]
    digest: str

    @classmethod
    def from_mapping(
        cls,
        payload: dict[str, object],
        registry: ToolCatalog,
    ) -> WorkflowDefinition:
        mapping = require_mapping(payload, field="workflow")
        definition_id = require_string(mapping.get("definition_id"), field="definition_id")
        version = require_positive_int(mapping.get("version"), field="version")
        raw_steps = mapping.get("steps")
        if not isinstance(raw_steps, list):
            raise DefinitionValidationError("steps must be a list", field="steps")
        if len(raw_steps) > MAX_STEPS:
            raise DefinitionValidationError(
                f"workflow definition supports at most {MAX_STEPS} steps",
                field="steps",
            )

        seen_ids: set[str] = set()
        parsed_steps: list[StepDefinition] = []
        dependencies: dict[str, tuple[str, ...]] = {}
        edge_count = 0

        for raw_step in raw_steps:
            step_mapping = require_mapping(raw_step, field="steps")
            step_id = require_string(step_mapping.get("id"), field="id")
            if step_id in seen_ids:
                raise DefinitionValidationError(
                    f"duplicate step id {step_id!r}",
                    step_id=step_id,
                    field="id",
                )
            seen_ids.add(step_id)

            tool = _parse_tool_reference(step_mapping.get("tool"), field="tool", step_id=step_id)
            _ensure_registered(tool, registry, step_id=step_id, field="tool")

            arguments_mapping = require_mapping(
                step_mapping.get("arguments", {}),
                field="arguments",
                step_id=step_id,
            )
            arguments = {key: value for key, value in arguments_mapping.items()}
            try:
                registry.validate_arguments(tool.name, tool.version, arguments)
            except Exception as exc:
                raise DefinitionValidationError(
                    f"invalid arguments: {exc}",
                    step_id=step_id,
                    field="arguments",
                ) from exc

            depends_on = require_string_sequence(
                step_mapping.get("depends_on"),
                field="depends_on",
                step_id=step_id,
            )
            edge_count += len(depends_on)
            risk = require_string(
                step_mapping.get("risk", DEFAULT_RISK),
                field="risk",
                step_id=step_id,
            )
            retry = _parse_retry_policy(step_mapping.get("retry"), step_id=step_id)
            compensation_tool = _parse_optional_tool_reference(
                step_mapping.get("compensation_tool"),
                registry,
                step_id=step_id,
            )

            parsed_steps.append(
                StepDefinition(
                    id=step_id,
                    tool=tool,
                    arguments=_freeze_mapping(arguments),
                    depends_on=depends_on,
                    risk=risk,
                    retry=retry,
                    compensation_tool=compensation_tool,
                )
            )
            dependencies[step_id] = depends_on

        if edge_count > MAX_EDGES:
            raise DefinitionValidationError(
                f"workflow definition supports at most {MAX_EDGES} dependency edges",
                field="steps",
            )

        for step_id, depends_on in dependencies.items():
            for dependency in depends_on:
                if dependency not in seen_ids:
                    raise DefinitionValidationError(
                        f"unknown dependency target {dependency!r}",
                        step_id=step_id,
                        field="depends_on",
                    )

        cycle = find_cycle(dependencies)
        if cycle is not None:
            raise DefinitionValidationError(
                f"cycle detected among steps: {', '.join(cycle)}",
                step_id=cycle[0],
                field="depends_on",
            )

        normalized_steps = tuple(sorted(parsed_steps, key=lambda step: step.id))
        digest = _calculate_digest(definition_id, version, normalized_steps)
        return cls(
            definition_id=definition_id,
            version=version,
            steps=normalized_steps,
            digest=digest,
        )


def _parse_tool_reference(
    raw_value: object,
    *,
    field: str,
    step_id: str | None = None,
) -> ToolReference:
    mapping = require_mapping(raw_value, field=field, step_id=step_id)
    return ToolReference(
        name=require_string(mapping.get("name"), field=f"{field}.name", step_id=step_id),
        version=require_string(
            mapping.get("version"),
            field=f"{field}.version",
            step_id=step_id,
        ),
    )


def _parse_optional_tool_reference(
    raw_value: object,
    registry: ToolCatalog,
    *,
    step_id: str,
) -> ToolReference | None:
    if raw_value is None:
        return None
    tool = _parse_tool_reference(raw_value, field="compensation_tool", step_id=step_id)
    _ensure_registered(tool, registry, step_id=step_id, field="compensation_tool")
    return tool


def _ensure_registered(
    tool: ToolReference,
    registry: ToolCatalog,
    *,
    step_id: str,
    field: str,
) -> None:
    if not registry.contains(tool.name, tool.version):
        raise DefinitionValidationError(
            f"unknown tool {tool.name}@{tool.version}",
            step_id=step_id,
            field=field,
        )


def _parse_retry_policy(raw_value: object, *, step_id: str) -> RetryPolicy:
    if raw_value is None:
        return RetryPolicy()
    mapping = require_mapping(raw_value, field="retry", step_id=step_id)
    max_attempts = mapping.get("max_attempts", 3)
    initial_backoff_seconds = mapping.get("initial_backoff_seconds", 1.0)
    if not isinstance(max_attempts, int):
        raise DefinitionValidationError(
            "max_attempts must be an integer",
            step_id=step_id,
            field="retry.max_attempts",
        )
    if not isinstance(initial_backoff_seconds, (int, float)):
        raise DefinitionValidationError(
            "initial_backoff_seconds must be numeric",
            step_id=step_id,
            field="retry.initial_backoff_seconds",
        )
    try:
        return RetryPolicy(
            max_attempts=max_attempts,
            initial_backoff_seconds=float(initial_backoff_seconds),
        )
    except DefinitionValidationError as exc:
        raise DefinitionValidationError(
            exc.message,
            step_id=step_id,
            field=f"retry.{exc.field}" if exc.field else "retry",
        ) from exc


def _freeze_mapping(mapping: dict[str, object]) -> MappingProxyType:
    return MappingProxyType({key: _freeze_value(value) for key, value in mapping.items()})


def _freeze_value(value: object) -> object:
    if isinstance(value, dict):
        return _freeze_mapping(value)
    if isinstance(value, list):
        return tuple(_freeze_value(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze_value(item) for item in value)
    return value


def _calculate_digest(
    definition_id: str,
    version: int,
    steps: tuple[StepDefinition, ...],
) -> str:
    canonical_payload = {
        "definition_id": definition_id,
        "version": version,
        "steps": [_step_to_canonical(step) for step in steps],
    }
    encoded = json.dumps(
        canonical_payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _step_to_canonical(step: StepDefinition) -> dict[str, object]:
    return {
        "id": step.id,
        "tool": {"name": step.tool.name, "version": step.tool.version},
        "arguments": _thaw_value(step.arguments),
        "depends_on": list(step.depends_on),
        "risk": step.risk,
        "retry": {
            "max_attempts": step.retry.max_attempts,
            "initial_backoff_seconds": step.retry.initial_backoff_seconds,
        },
        "compensation_tool": (
            None
            if step.compensation_tool is None
            else {
                "name": step.compensation_tool.name,
                "version": step.compensation_tool.version,
            }
        ),
    }


def _thaw_value(value: object) -> object:
    if isinstance(value, MappingProxyType):
        return {key: _thaw_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_value(item) for item in value]
    return value
