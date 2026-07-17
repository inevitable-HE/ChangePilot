from __future__ import annotations

import json
import os
from pathlib import Path
from typing import TypeAlias

from pydantic import BaseModel

from changepilot.workflow.application.tooling import redact
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


try:  # pragma: no cover - exercised on Windows workers
    import msvcrt
except ImportError:  # pragma: no cover - Unix
    msvcrt = None

try:  # pragma: no cover - exercised on Unix
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None


class LedgerCorruptionError(RuntimeError):
    """Raised when the JSONL ledger cannot be trusted."""


class _LedgerLock:
    def __init__(self, lock_path: Path) -> None:
        self._lock_path = lock_path
        self._handle = None

    def __enter__(self) -> _LedgerLock:
        self._lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._lock_path.open("a+b")
        self._handle.seek(0, os.SEEK_END)
        if self._handle.tell() == 0:
            self._handle.write(b"\0")
            self._handle.flush()
        self._handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_LOCK, 1)
        elif fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX)
        else:  # pragma: no cover - platform guard
            raise RuntimeError("no supported file locking primitive available")
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._handle is None:
            return
        self._handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(self._handle.fileno(), msvcrt.LK_UNLCK, 1)
        elif fcntl is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        self._handle.close()
        self._handle = None


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
        with self._locked_ledger():
            existing = self._read_effect_unlocked(context.logical_idempotency_key)
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
                self._append_effect_unlocked(context.logical_idempotency_key, result.output)
            return result

    def probe(self, query: RecoveryQuery) -> ToolProbeResult:
        with self._locked_ledger():
            existing = self._read_effect_unlocked(query.logical_idempotency_key)
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
        with self._locked_ledger():
            return 1 if self._read_effect_unlocked(logical_idempotency_key) is not None else 0

    def _next_execute_outcome(self) -> ScriptedExecuteOutcome:
        if not self._execute_script:
            raise RuntimeError("no scripted execute outcome available")
        return self._execute_script.pop(0)

    def _next_probe_outcome(self) -> ScriptedProbeOutcome:
        if not self._probe_script:
            return ToolProbeResult(status=ToolProbeStatus.NOT_FOUND, output=None)
        return self._probe_script.pop(0)

    def _append_effect_unlocked(self, logical_idempotency_key: str, output: BaseModel) -> None:
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "logical_idempotency_key": logical_idempotency_key,
            "tool": {
                "name": self.descriptor.name,
                "version": self.descriptor.version,
            },
            "output": redact(
                output.model_dump(mode="python"),
                sensitive_paths=self.descriptor.sensitive_argument_paths,
            )
        }
        encoded = (
            json.dumps(payload, sort_keys=True, ensure_ascii=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        with self.ledger_path.open("ab") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())

    def _read_effect_unlocked(self, logical_idempotency_key: str) -> dict[str, object] | None:
        for record in self._iter_records_unlocked():
            if record["logical_idempotency_key"] == logical_idempotency_key:
                return record
        return None

    def _iter_records_unlocked(self) -> list[dict[str, object]]:
        if not self.ledger_path.exists():
            return []
        records: list[dict[str, object]] = []
        seen_keys: set[str] = set()
        with self.ledger_path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise LedgerCorruptionError(
                        f"invalid ledger JSON on line {line_number} of {self.ledger_path}"
                    ) from exc
                if not isinstance(record, dict):
                    raise LedgerCorruptionError(
                        f"invalid ledger record on line {line_number} of {self.ledger_path}"
                    )
                logical_key = record.get("logical_idempotency_key")
                if not isinstance(logical_key, str) or not logical_key:
                    raise LedgerCorruptionError(
                        f"invalid logical_idempotency_key on line {line_number} of {self.ledger_path}"
                    )
                if logical_key in seen_keys:
                    raise LedgerCorruptionError(
                        f"duplicate logical_idempotency_key '{logical_key}' in {self.ledger_path}"
                    )
                if "output" not in record:
                    raise LedgerCorruptionError(
                        f"missing output on line {line_number} of {self.ledger_path}"
                    )
                seen_keys.add(logical_key)
                records.append(record)
        return records

    def _locked_ledger(self) -> _LedgerLock:
        return _LedgerLock(self.ledger_path.with_suffix(self.ledger_path.suffix + ".lock"))
