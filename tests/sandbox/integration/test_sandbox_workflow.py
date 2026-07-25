from __future__ import annotations

from pathlib import Path

from changepilot.sandbox.application.runtime import make_sandbox_runtime
from changepilot.sandbox.domain.faults import FaultSpec, FaultType
from changepilot.sandbox.domain.models import SchemaVersion, ServiceVersion


def _start_approved(runtime) -> str:
    runtime.driver.start()
    run_id = runtime.create_upgrade_run()
    runtime.run_until("waiting_approval")
    runtime.approve_pending()
    return run_id


def test_fixed_workflow_upgrades_v1_sandbox_to_v2(tmp_path: Path) -> None:
    runtime = make_sandbox_runtime(tmp_path / "success")
    try:
        run_id = _start_approved(runtime)

        runtime.run_until("succeeded")

        state = runtime.manager.inspect(runtime.sandbox_id)
        assert runtime.query.get_run(run_id).state == "succeeded"
        assert state.service_version is ServiceVersion.V2
        assert state.schema_version is SchemaVersion.V2
        assert "orders-v1-to-v2" in state.migration_ids
    finally:
        runtime.close()


def test_health_failure_compensates_service_and_schema(tmp_path: Path) -> None:
    runtime = make_sandbox_runtime(
        tmp_path / "compensation",
        fault_specs=(
            FaultSpec(
                tool_name="service.health-check",
                call_number=1,
                fault_type=FaultType.PERMANENT_FAILURE,
            ),
        ),
    )
    try:
        run_id = _start_approved(runtime)

        runtime.run_until("compensated")

        state = runtime.manager.inspect(runtime.sandbox_id)
        assert runtime.query.get_run(run_id).state == "compensated"
        assert state.service_version is ServiceVersion.V1
        assert state.schema_version is SchemaVersion.V1
        events = runtime.query.list_events(run_id)
        compensation_steps = [
            event.step_id
            for event in events
            if event.event_type == "tool_attempt_completed"
            and event.phase == "compensation"
        ]
        assert compensation_steps == ["deploy-v2", "migrate-schema"]
    finally:
        runtime.close()


def test_restart_recovers_migration_committed_before_result(
    tmp_path: Path,
) -> None:
    root = tmp_path / "after-effect"
    runtime = make_sandbox_runtime(
        root,
        fault_specs=(
            FaultSpec(
                tool_name="schema.migrate",
                call_number=1,
                fault_type=FaultType.AFTER_EFFECT,
            ),
        ),
    )
    run_id = _start_approved(runtime)
    runtime.run_until_step("migrate-schema", "result_unknown")
    assert (
        runtime.manager.inspect(runtime.sandbox_id).schema_version
        is SchemaVersion.V2
    )
    runtime.close()

    resumed = make_sandbox_runtime(
        root,
        fault_specs=None,
        selected_run_id=run_id,
    )
    try:
        recovered = resumed.driver.start()
        assert run_id in recovered

        resumed.run_until("succeeded")

        state = resumed.manager.inspect(resumed.sandbox_id)
        assert state.service_version is ServiceVersion.V2
        assert state.schema_version is SchemaVersion.V2
        assert resumed.faults.call_count(
            resumed.sandbox_id,
            "schema.migrate",
        ) == 1
    finally:
        resumed.close()


def test_restart_retries_interruption_before_migration_effect(
    tmp_path: Path,
) -> None:
    root = tmp_path / "before-effect"
    runtime = make_sandbox_runtime(
        root,
        fault_specs=(
            FaultSpec(
                tool_name="schema.migrate",
                call_number=1,
                fault_type=FaultType.BEFORE_EFFECT,
            ),
        ),
    )
    run_id = _start_approved(runtime)
    runtime.run_until_step("migrate-schema", "result_unknown")
    assert (
        runtime.manager.inspect(runtime.sandbox_id).schema_version
        is SchemaVersion.V1
    )
    runtime.close()

    resumed = make_sandbox_runtime(
        root,
        fault_specs=None,
        selected_run_id=run_id,
    )
    try:
        assert run_id in resumed.driver.start()

        resumed.run_until("succeeded")

        state = resumed.manager.inspect(resumed.sandbox_id)
        assert state.schema_version is SchemaVersion.V2
        assert resumed.faults.call_count(
            resumed.sandbox_id,
            "schema.migrate",
        ) == 2
    finally:
        resumed.close()
