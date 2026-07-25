from __future__ import annotations

import json
from threading import Lock

from pydantic import BaseModel

from changepilot.sandbox.application.manager import SandboxManager
from changepilot.sandbox.domain.faults import (
    FaultConfiguration,
    FaultSpec,
    FaultType,
)
from changepilot.workflow.domain.failures import DomainError, ErrorClass
from changepilot.workflow.ports.tools import (
    RecoveryQuery,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolProbeResult,
)


class DeterministicFaultInjector:
    def __init__(self, manager: SandboxManager) -> None:
        self._manager = manager
        self._lock = Lock()

    def configure(
        self,
        sandbox_id: str,
        specs: tuple[FaultSpec, ...],
    ) -> None:
        identities = {
            (spec.tool_name, spec.call_number)
            for spec in specs
        }
        if len(identities) != len(specs):
            raise ValueError("duplicate fault tool/call configuration")
        self._write(
            sandbox_id,
            FaultConfiguration(specs=specs),
        )

    def next_fault(
        self,
        sandbox_id: str,
        tool_name: str,
    ) -> FaultType | None:
        with self._lock:
            configuration = self._read(sandbox_id)
            counts = dict(configuration.call_counts)
            call_number = counts.get(tool_name, 0) + 1
            counts[tool_name] = call_number
            updated = configuration.model_copy(
                update={"call_counts": counts}
            )
            self._write(sandbox_id, updated)
        return next(
            (
                spec.fault_type
                for spec in configuration.specs
                if spec.tool_name == tool_name
                and spec.call_number == call_number
            ),
            None,
        )

    def call_count(self, sandbox_id: str, tool_name: str) -> int:
        return self._read(sandbox_id).call_counts.get(tool_name, 0)

    def _read(self, sandbox_id: str) -> FaultConfiguration:
        path = self._manager.resolve_resource(sandbox_id, "faults.json")
        if not path.exists():
            return FaultConfiguration()
        return FaultConfiguration.model_validate_json(
            path.read_text(encoding="utf-8")
        )

    def _write(
        self,
        sandbox_id: str,
        configuration: FaultConfiguration,
    ) -> None:
        path = self._manager.resolve_resource(sandbox_id, "faults.json")
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            configuration.model_dump_json(),
            encoding="utf-8",
        )
        temporary.replace(path)


class FaultInjectingTool:
    def __init__(
        self,
        inner: object,
        injector: DeterministicFaultInjector,
    ) -> None:
        self.descriptor = inner.descriptor
        self._inner = inner
        self._injector = injector

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: BaseModel,
    ) -> ToolExecutionResult:
        sandbox_id = getattr(arguments, "sandbox_id", None)
        if not isinstance(sandbox_id, str):
            raise DomainError(
                "sandbox tool arguments lack sandbox_id",
                error_class=ErrorClass.INTERNAL_CONSISTENCY,
            )
        fault = self._injector.next_fault(
            sandbox_id,
            self.descriptor.name,
        )
        if fault is FaultType.BEFORE_EFFECT:
            raise DomainError(
                "injected interruption before tool effect",
                error_class=ErrorClass.RESULT_UNKNOWN,
            )
        if fault is FaultType.PERMANENT_FAILURE:
            raise DomainError(
                "injected permanent tool failure",
                error_class=ErrorClass.PERMANENT,
            )
        result = self._inner.execute(context, arguments)
        if fault is FaultType.AFTER_EFFECT:
            raise DomainError(
                "injected interruption after tool effect",
                error_class=ErrorClass.RESULT_UNKNOWN,
            )
        return result

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        return self._inner.probe(query)
