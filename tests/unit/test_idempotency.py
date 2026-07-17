from __future__ import annotations

import importlib
import sys
import tempfile
from datetime import UTC, datetime, timedelta
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


def load_fake_module():
    try:
        return importlib.import_module("changepilot.workflow.adapters.tools.fake")
    except ModuleNotFoundError as exc:
        pytest.fail(f"missing fake tools module: {exc.name}")


class ScriptArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    action: str


class ScriptOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    effect_id: str
    state: str


def make_descriptor():
    ports = load_ports_module()
    return ports.ToolDescriptor(
        name="scripted_deploy",
        version="1.0.0",
        input_model=ScriptArguments,
        output_model=ScriptOutput,
        risk=ports.ToolRisk.MEDIUM,
        idempotency=ports.ToolIdempotency.SUPPORTED,
        default_timeout_seconds=15,
        max_timeout_seconds=60,
        sensitive_argument_paths=(),
    )


def make_context(logical_key: str):
    ports = load_ports_module()
    return ports.ToolExecutionContext(
        run_id="run-42",
        step_id="deploy",
        attempt_number=3,
        phase=ports.ToolExecutionPhase.FORWARD,
        logical_idempotency_key=logical_key,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        metadata={"trace_id": "trace-123"},
    )


def make_query(logical_key: str):
    ports = load_ports_module()
    return ports.RecoveryQuery(
        run_id="run-42",
        step_id="deploy",
        phase=ports.ToolExecutionPhase.FORWARD,
        logical_idempotency_key=logical_key,
        deadline=datetime.now(UTC) + timedelta(seconds=30),
        metadata={"trace_id": "trace-123"},
    )


def test_logical_idempotency_key_is_stable_and_phase_scoped() -> None:
    ports = load_ports_module()
    tooling = load_tooling_module()

    first = tooling.logical_idempotency_key(
        run_id="ab",
        step_id="c",
        tool_name="deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.FORWARD,
    )
    repeated = tooling.logical_idempotency_key(
        run_id="ab",
        step_id="c",
        tool_name="deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.FORWARD,
    )
    changed = tooling.logical_idempotency_key(
        run_id="a",
        step_id="bc",
        tool_name="deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.FORWARD,
    )
    compensation = tooling.logical_idempotency_key(
        run_id="ab",
        step_id="c",
        tool_name="deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.COMPENSATION,
    )

    assert first == repeated
    assert first != changed
    assert first != compensation
    assert len(first) == 64

    with pytest.raises(ValueError):
        tooling.logical_idempotency_key(
            run_id="",
            step_id="deploy",
            tool_name="deploy",
            tool_version="1.0.0",
            phase=ports.ToolExecutionPhase.FORWARD,
        )


def test_execution_and_recovery_contracts_are_typed_and_immutable() -> None:
    ports = load_ports_module()
    tooling = load_tooling_module()

    logical_key = tooling.logical_idempotency_key(
        run_id="run-42",
        step_id="deploy",
        tool_name="scripted_deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.FORWARD,
    )
    context = make_context(logical_key)
    query = make_query(logical_key)

    assert context.logical_idempotency_key == logical_key
    assert query.logical_idempotency_key == logical_key

    with pytest.raises(ValidationError):
        context.run_id = "mutated"

    with pytest.raises(ValidationError):
        ports.ToolExecutionContext(
            run_id="run-42",
            step_id="deploy",
            attempt_number=0,
            phase=ports.ToolExecutionPhase.FORWARD,
            logical_idempotency_key=logical_key,
            deadline=datetime.now(UTC),
            metadata={},
        )


def test_fake_ledger_deduplicates_effects_across_instances_and_supports_probe() -> None:
    ports = load_ports_module()
    tooling = load_tooling_module()
    fake = load_fake_module()

    descriptor = make_descriptor()
    logical_key = tooling.logical_idempotency_key(
        run_id="run-42",
        step_id="deploy",
        tool_name="scripted_deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.FORWARD,
    )
    context = make_context(logical_key)
    query = make_query(logical_key)
    ledger_path = Path(tempfile.mkdtemp(prefix="changepilot-fake-ledger-")) / "effects.jsonl"

    first_tool = fake.ScriptedLedgerTool(
        descriptor=descriptor,
        ledger_path=ledger_path,
        execute_script=[
            ports.ToolExecutionResult(
                output=ScriptOutput(effect_id="fx-1", state="applied"),
                effect_applied=True,
            )
        ],
    )
    first = first_tool.execute(context, ScriptArguments(action="deploy"))

    second_tool = fake.ScriptedLedgerTool(
        descriptor=descriptor,
        ledger_path=ledger_path,
        execute_script=[
            ports.ToolExecutionResult(
                output=ScriptOutput(effect_id="fx-2", state="should-not-run"),
                effect_applied=True,
            )
        ],
        probe_script=[
            ports.ToolProbeResult(
                status=ports.ToolProbeStatus.NOT_FOUND,
                output=None,
            )
        ],
    )
    replayed = second_tool.execute(context, ScriptArguments(action="deploy"))
    probed = second_tool.probe(query)

    assert first.effect_applied is True
    assert replayed.effect_applied is False
    assert replayed.output == ScriptOutput(effect_id="fx-1", state="applied")
    assert second_tool.effect_count(logical_key) == 1
    assert probed.status is ports.ToolProbeStatus.FOUND
    assert probed.output == ScriptOutput(effect_id="fx-1", state="applied")
