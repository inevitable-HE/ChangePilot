from __future__ import annotations

import importlib
import json
import multiprocessing
import queue
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


class SensitiveLedgerOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    effect_id: str
    credentials: dict[str, object]
    metadata: dict[str, object]


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


def make_sensitive_descriptor():
    ports = load_ports_module()
    return ports.ToolDescriptor(
        name="scripted_secret_deploy",
        version="1.0.0",
        input_model=ScriptArguments,
        output_model=SensitiveLedgerOutput,
        risk=ports.ToolRisk.MEDIUM,
        idempotency=ports.ToolIdempotency.SUPPORTED,
        default_timeout_seconds=15,
        max_timeout_seconds=60,
        sensitive_argument_paths=("credentials.token", "metadata.audit.password"),
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


def _race_worker(
    ledger_path_text: str,
    logical_key: str,
    effect_id: str,
    barrier,
    result_queue,
) -> None:
    try:
        ports = load_ports_module()
        fake = load_fake_module()

        tool = fake.ScriptedLedgerTool(
            descriptor=make_descriptor(),
            ledger_path=Path(ledger_path_text),
            execute_script=[
                ports.ToolExecutionResult(
                    output=ScriptOutput(effect_id=effect_id, state="applied"),
                    effect_applied=True,
                )
            ],
        )
        barrier.wait(timeout=10)
        result = tool.execute(make_context(logical_key), ScriptArguments(action="deploy"))
        result_queue.put(
            {
                "status": "ok",
                "effect_applied": result.effect_applied,
                "effect_id": result.output.effect_id,
                "state": result.output.state,
            }
        )
    except BaseException as exc:
        result_queue.put(
            {
                "status": "error",
                "error": f"{exc.__class__.__name__}: {exc}",
            }
        )


def _ledger_record(*, logical_key: str, output: BaseModel) -> str:
    return json.dumps(
        {
            "logical_idempotency_key": logical_key,
            "tool": {"name": "scripted_deploy", "version": "1.0.0"},
            "output": output.model_dump(mode="json"),
        },
        sort_keys=True,
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


def test_fake_ledger_redacts_sensitive_fields_before_persisting() -> None:
    ports = load_ports_module()
    tooling = load_tooling_module()
    fake = load_fake_module()

    descriptor = make_sensitive_descriptor()
    logical_key = tooling.logical_idempotency_key(
        run_id="run-42",
        step_id="deploy",
        tool_name="scripted_secret_deploy",
        tool_version="1.0.0",
        phase=ports.ToolExecutionPhase.FORWARD,
    )
    ledger_path = Path(tempfile.mkdtemp(prefix="changepilot-fake-ledger-")) / "effects.jsonl"
    tool = fake.ScriptedLedgerTool(
        descriptor=descriptor,
        ledger_path=ledger_path,
        execute_script=[
            ports.ToolExecutionResult(
                output=SensitiveLedgerOutput(
                    effect_id="fx-1",
                    credentials={
                        "token": "super-secret-value",
                        "secret_ref": ports.SecretRef(
                            provider="env",
                            name="AWS_SECRET_ACCESS_KEY",
                        ),
                    },
                    metadata={
                        "owner": "platform",
                        "audit": {"password": "raw-password"},
                    },
                ),
                effect_applied=True,
            )
        ],
    )

    result = tool.execute(make_context(logical_key), ScriptArguments(action="deploy"))
    persisted = ledger_path.read_text(encoding="utf-8")

    assert result.effect_applied is True
    assert "super-secret-value" not in persisted
    assert "AWS_SECRET_ACCESS_KEY" not in persisted
    assert "raw-password" not in persisted
    assert tooling.REDACTED in persisted


def test_fake_ledger_applies_same_key_once_across_real_processes() -> None:
    tooling = load_tooling_module()
    fake = load_fake_module()

    logical_key = tooling.logical_idempotency_key(
        run_id="run-42",
        step_id="deploy",
        tool_name="scripted_deploy",
        tool_version="1.0.0",
        phase=load_ports_module().ToolExecutionPhase.FORWARD,
    )
    ledger_path = Path(tempfile.mkdtemp(prefix="changepilot-fake-ledger-")) / "effects.jsonl"
    worker_count = 6
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(worker_count)
    result_queue = ctx.Queue()
    processes = [
        ctx.Process(
            target=_race_worker,
            args=(str(ledger_path), logical_key, f"fx-{index}", barrier, result_queue),
        )
        for index in range(worker_count)
    ]

    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=20)
            assert process.is_alive() is False
            assert process.exitcode == 0
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)

    results = []
    for _ in range(worker_count):
        try:
            results.append(result_queue.get(timeout=5))
        except queue.Empty as exc:
            pytest.fail(f"worker did not publish a result: {exc}")

    errors = [item["error"] for item in results if item["status"] == "error"]
    assert errors == []

    effect_ids = {item["effect_id"] for item in results}
    assert len(effect_ids) == 1
    assert sum(1 for item in results if item["effect_applied"]) == 1

    replay_tool = fake.ScriptedLedgerTool(
        descriptor=make_descriptor(),
        ledger_path=ledger_path,
    )
    winning_effect_id = next(iter(effect_ids))

    assert replay_tool.effect_count(logical_key) == 1
    assert replay_tool.probe(make_query(logical_key)).output == ScriptOutput(
        effect_id=winning_effect_id,
        state="applied",
    )


def test_fake_ledger_raises_typed_corruption_for_bad_json_lines() -> None:
    fake = load_fake_module()

    logical_key = "logical-key"
    ledger_path = Path(tempfile.mkdtemp(prefix="changepilot-fake-ledger-")) / "effects.jsonl"
    ledger_path.write_text(
        _ledger_record(
            logical_key=logical_key,
            output=ScriptOutput(effect_id="fx-1", state="applied"),
        )
        + "\nthis-is-not-json\n",
        encoding="utf-8",
    )
    tool = fake.ScriptedLedgerTool(
        descriptor=make_descriptor(),
        ledger_path=ledger_path,
    )

    with pytest.raises(fake.LedgerCorruptionError, match="line 2"):
        tool.effect_count(logical_key)

    with pytest.raises(fake.LedgerCorruptionError, match="line 2"):
        tool.probe(make_query(logical_key))


def test_fake_ledger_raises_typed_corruption_for_duplicate_keys() -> None:
    fake = load_fake_module()

    logical_key = "logical-key"
    ledger_path = Path(tempfile.mkdtemp(prefix="changepilot-fake-ledger-")) / "effects.jsonl"
    ledger_path.write_text(
        "\n".join(
            [
                _ledger_record(
                    logical_key=logical_key,
                    output=ScriptOutput(effect_id="fx-1", state="applied"),
                ),
                _ledger_record(
                    logical_key=logical_key,
                    output=ScriptOutput(effect_id="fx-2", state="replayed"),
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    tool = fake.ScriptedLedgerTool(
        descriptor=make_descriptor(),
        ledger_path=ledger_path,
    )

    with pytest.raises(fake.LedgerCorruptionError, match=logical_key):
        tool.effect_count(logical_key)

    with pytest.raises(fake.LedgerCorruptionError, match=logical_key):
        tool.probe(make_query(logical_key))
