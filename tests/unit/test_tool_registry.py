from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError


ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_ports_module():
    try:
        return importlib.import_module("changepilot.workflow.ports.tools")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing ports module: {exc.name}")


def load_tooling_module():
    try:
        return importlib.import_module("changepilot.workflow.application.tooling")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing tooling module: {exc.name}")


class DeployArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    target: str
    timeout_seconds: int


class DeployOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    deployment_id: str
    applied: bool


class ProbeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    found: bool
    deployment_id: str | None = None


class NonStrictArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timeout_seconds: int


class MutableArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    timeout_seconds: int


class PermissiveOutput(BaseModel):
    model_config = ConfigDict(extra="allow", frozen=True, strict=True)

    deployment_id: str


def make_descriptor():
    ports = load_ports_module()
    return ports.ToolDescriptor(
        name="deploy_service",
        version="1.2.3",
        input_model=DeployArguments,
        output_model=DeployOutput,
        risk=ports.ToolRisk.HIGH,
        idempotency=ports.ToolIdempotency.SUPPORTED,
        default_timeout_seconds=30,
        max_timeout_seconds=120,
        sensitive_argument_paths=("credentials.token", "metadata.secret"),
    )


class DeployTool:
    def __init__(self, descriptor) -> None:
        self.descriptor = descriptor

    def execute(self, context, arguments):
        ports = load_ports_module()
        return ports.ToolExecutionResult(
            output=DeployOutput(
                deployment_id=f"{arguments.target}-{context.run_id}",
                applied=True,
            ),
            effect_applied=True,
        )

    def probe(self, query):
        ports = load_ports_module()
        return ports.ToolProbeResult(
            status=ports.ToolProbeStatus.NOT_FOUND,
            output=None,
        )


def test_descriptor_validates_boundary_types_and_is_immutable() -> None:
    ports = load_ports_module()

    descriptor = make_descriptor()

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=NonStrictArguments,
            output_model=DeployOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=MutableArguments,
            output_model=DeployOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=DeployArguments,
            output_model=PermissiveOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=dict,
            output_model=DeployOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=DeployArguments,
            output_model=str,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=DeployArguments,
            output_model=DeployOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=0,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=DeployArguments,
            output_model=DeployOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=20,
            sensitive_argument_paths=("credentials.token",),
        )

    with pytest.raises(ValidationError):
        ports.ToolDescriptor(
            name="deploy_service",
            version="1.2.3",
            input_model=DeployArguments,
            output_model=DeployOutput,
            risk=ports.ToolRisk.HIGH,
            idempotency=ports.ToolIdempotency.SUPPORTED,
            default_timeout_seconds=30,
            max_timeout_seconds=120,
            sensitive_argument_paths=("credentials..token",),
        )

    with pytest.raises(ValidationError):
        descriptor.version = "9.9.9"


def test_registry_preserves_tool_catalog_compatibility_and_rejects_duplicates() -> None:
    tooling = load_tooling_module()
    registry = tooling.ToolRegistry()

    descriptor = make_descriptor()
    registry.register(DeployTool(descriptor))

    assert registry.contains("deploy_service", "1.2.3") is True
    registry.validate_arguments(
        "deploy_service",
        "1.2.3",
        {"target": "prod", "timeout_seconds": 30},
    )

    with pytest.raises(ValueError):
        registry.register(DeployTool(descriptor))

    with pytest.raises(LookupError):
        registry.resolve("missing", "0.0.1")


def test_registry_rejects_descriptor_mismatch_invalid_arguments_and_invalid_output() -> None:
    ports = load_ports_module()
    tooling = load_tooling_module()
    registry = tooling.ToolRegistry()

    descriptor = make_descriptor()
    mismatched = ports.ToolDescriptor(
        name="deploy_service",
        version="9.9.9",
        input_model=DeployArguments,
        output_model=DeployOutput,
        risk=ports.ToolRisk.HIGH,
        idempotency=ports.ToolIdempotency.SUPPORTED,
        default_timeout_seconds=30,
        max_timeout_seconds=120,
        sensitive_argument_paths=("credentials.token",),
    )

    with pytest.raises(ValueError):
        registry.register(DeployTool(descriptor), descriptor=mismatched)

    registry.register(DeployTool(descriptor), descriptor=descriptor)

    validated_arguments = registry.coerce_arguments(
        "deploy_service",
        "1.2.3",
        {"target": "prod", "timeout_seconds": 30},
    )
    assert validated_arguments == DeployArguments(target="prod", timeout_seconds=30)

    with pytest.raises(ValidationError):
        validated_arguments.timeout_seconds = 99

    with pytest.raises(ValueError):
        registry.validate_arguments(
            "deploy_service",
            "1.2.3",
            {"target": "prod", "timeout_seconds": "30"},
        )

    with pytest.raises(ValueError):
        registry.validate_arguments(
            "deploy_service",
            "1.2.3",
            {
                "target": "prod",
                "timeout_seconds": 30,
                "unexpected": True,
            },
        )

    validated = registry.validate_output(
        "deploy_service",
        "1.2.3",
        {"deployment_id": "dep-1", "applied": True},
    )
    assert validated == DeployOutput(deployment_id="dep-1", applied=True)

    with pytest.raises(ValueError):
        registry.validate_output(
            "deploy_service",
            "1.2.3",
            {"deployment_id": "dep-1"},
        )
