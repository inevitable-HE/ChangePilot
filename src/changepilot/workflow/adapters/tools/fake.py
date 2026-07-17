from __future__ import annotations

import json
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel

from changepilot.workflow.ports.tools import (
    RecoveryQuery,
    ToolDescriptor,
    ToolExecutionContext,
    ToolExecutionResult,
    ToolProbeResult,
    ToolProbeStatus,
)


ScriptedExecuteOutcome: TypeAlias = ToolExecutionResult | Exception
ScriptedProbeOutcome: TypeAlias = ToolProbeResult | Exception


class ScriptedLedgerTool:
    def __init__(
        self,
        *,
        descriptor: ToolDescriptor,
        ledger_path: Path,
        execute_script: list[ScriptedExecuteOutcome] | None = None,
        probe_script: list[ScriptedProbeOutcome] | None = None,
    ) -> None:
        self.descriptor = descriptor
        self.ledger_path = Path(ledger_path)
        self._execute_script = list(execute_script or [])
        self._probe_script = list(probe_script or [])

    def execute(
        self,
        context: ToolExecutionContext,
        arguments: BaseModel,
    ) -> ToolExecutionResult:
        existing = self._read_effect(context.logical_idempotency_key)
        if existing is not None:
            return ToolExecutionResult(
                output=self.descriptor.output_model.model_validate(existing["output"]),
                effect_applied=False,
            )
        if not isinstance(arguments, self.descriptor.input_model):
            payload = (
                arguments.model_dump(mode="python")
                if isinstance(arguments, BaseModel)
                else arguments
            )
            arguments = self.descriptor.input_model.model_validate(payload)
        outcome = self._next_execute_outcome()
        if isinstance(outcome, Exception):
            raise outcome
        validated_output = self.descriptor.output_model.model_validate(
            outcome.output.model_dump(mode="python")
        )
        result = ToolExecutionResult(
            output=validated_output,
            effect_applied=outcome.effect_applied,
        )
        if result.effect_applied:
            self._append_effect(context.logical_idempotency_key, result.output)
        return result

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        existing = self._read_effect(query.logical_idempotency_key)
        if existing is not None:
            return ToolProbeResult(
                status=ToolProbeStatus.FOUND,
                output=self.descriptor.output_model.model_validate(existing["output"]),
            )
        outcome = self._next_probe_outcome()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    def effect_count(self, logical_idempotency_key: str) -> int:
        return sum(
            1
            for record in self._iter_records()
            if record["logical_idempotency_key"] == logical_idempotency_key
        )

    def _next_execute_outcome(self) -> ScriptedExecuteOutcome:
        if not self._execute_script:
            raise RuntimeError("no scripted execute outcome available")
        return self._execute_script.pop(0)

    def _next_probe_outcome(self) -> ScriptedProbeOutcome:
        if not self._probe_script:
            return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND, output=None)
        return self._probe_script.pop(0)

    def _append_effect(self, logical_idempotency_key: str, output: BaseModel) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        with self.ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(
                    {
                        "logical_idempotency_key": logical_idempotency_key,
                        "tool": {
                            "name": self.descriptor.name,
                            "version": self.descriptor.version,
                        },
                        "output": output.model_dump(mode="json"),
                    },
                    sort_keys=True,
                )
            )
            handle.write("\n")

    def _read_effect(self, logical_idempotency_key: str) -> dict[str, object] | None:
        for record in self._iter_records():
            if record["logical_idempotency_key"] == logical_idempotency_key:
                return record
        return None

    def _iter_records(self) -> list[dict[str, object]]:
        if not self.ledger_path.exists():
            return []
        with self.ledger_path.open("r", encoding="utf-8") as handle:
            return [json.loads(line) for line in handle if line.strip()]
