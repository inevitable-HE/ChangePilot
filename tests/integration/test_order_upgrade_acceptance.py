from __future__ import annotations

from pathlib import Path

import pytest

from changepilot.workflow.domain.failures import InvalidTransition
from tests.support.order_upgrade import (
    drive_until_state,
    make_order_upgrade_runtime,
    order_upgrade_definition,
)


def _approve_pending(runtime, *, secret: str = "operator-secret") -> None:
    view = runtime.query.get_run(runtime.selected_run[0])
    request = view.pending_approval
    assert request is not None
    runtime.approvals.decide(
        view.run_id,
        request.request_id,
        "approved",
        expected_version=request.version,
        binding_digest=request.binding_digest,
        actor=f"ops:{secret}",
        reason=f"approved with {secret}",
    )


def test_order_upgrade_waits_for_approval_then_succeeds(tmp_path: Path) -> None:
    runtime = make_order_upgrade_runtime(tmp_path / "success.db")
    try:
        run_id = runtime.workflow.create_run(order_upgrade_definition())
        runtime.selected_run[0] = run_id
        assert runtime.driver.start() == ()

        drive_until_state(runtime, "waiting_approval")

        assert runtime.tools["schema.migrate"].contexts == []
        _approve_pending(runtime)
        drive_until_state(runtime, "succeeded")
        assert runtime.query.get_run(run_id).state == "succeeded"
        assert runtime.calls[-4:] == [
            "schema.migrate",
            "service.deploy-v2",
            "service.health-check",
            "service.smoke-test",
        ]
    finally:
        runtime.close()


def test_permanent_health_failure_compensates_service_then_schema(tmp_path: Path) -> None:
    runtime = make_order_upgrade_runtime(
        tmp_path / "compensation.db",
        health_outcomes=("permanent",),
    )
    try:
        run_id = runtime.workflow.create_run(order_upgrade_definition())
        runtime.selected_run[0] = run_id
        runtime.driver.start()
        drive_until_state(runtime, "waiting_approval")
        _approve_pending(runtime)

        drive_until_state(runtime, "compensated")

        view = runtime.query.get_run(run_id)
        assert view.state == "compensated"
        assert view.original_error.error_class == "permanent"
        assert view.compensation_error is None
        assert runtime.calls[-2:] == ["service.restore", "schema.rollback"]
        assert runtime.tools["service.smoke-test"].contexts == []
    finally:
        runtime.close()


def test_audit_is_contiguous_paginated_and_secret_free_everywhere(tmp_path: Path) -> None:
    secret = "TOP-SECRET-acceptance-value"
    runtime = make_order_upgrade_runtime(tmp_path / "audit.db")
    try:
        run_id = runtime.workflow.create_run(order_upgrade_definition())
        runtime.selected_run[0] = run_id
        runtime.driver.start()
        drive_until_state(runtime, "waiting_approval")
        _approve_pending(runtime, secret=secret)
        drive_until_state(runtime, "succeeded")

        view = runtime.query.get_run(run_id)
        events = runtime.query.list_events(run_id)
        cursor = events[len(events) // 2].sequence
        later = runtime.query.list_events(run_id, after_sequence=cursor)
        assert [item.sequence for item in events] == list(range(1, len(events) + 1))
        assert all(item.sequence > cursor for item in later)
        assert secret not in repr(view)
        assert secret not in repr(events)

        runtime.coordinator.close(wait=True)
        runtime.engine.dispose()
        assert secret.encode() not in runtime.database_path.read_bytes()
    finally:
        runtime.close()


def test_cancel_is_legal_before_start_and_illegal_after_success(tmp_path: Path) -> None:
    runtime = make_order_upgrade_runtime(tmp_path / "cancel.db")
    try:
        cancelled_id = runtime.workflow.create_run(order_upgrade_definition())
        runtime.workflow.cancel_run(cancelled_id)
        assert runtime.query.get_run(cancelled_id).state == "cancelled"

        successful_definition = order_upgrade_definition()
        successful_definition["version"] = 2
        successful_id = runtime.workflow.create_run(successful_definition)
        runtime.selected_run[0] = successful_id
        runtime.driver.start()
        drive_until_state(runtime, "waiting_approval")
        _approve_pending(runtime)
        drive_until_state(runtime, "succeeded")
        with pytest.raises(InvalidTransition):
            runtime.workflow.cancel_run(successful_id)
    finally:
        runtime.close()


def test_restart_driver_recovers_before_any_dispatch(tmp_path: Path) -> None:
    runtime = make_order_upgrade_runtime(tmp_path / "restart-order.db")
    try:
        run_id = runtime.workflow.create_run(order_upgrade_definition())
        runtime.selected_run[0] = run_id
        calls: list[str] = []
        original_recover = runtime.recovery.recover_nonterminal_runs
        original_run_once = runtime.coordinator.run_once

        def recover():
            calls.append("recovery")
            return original_recover()

        def run_once():
            calls.append("coordinator")
            return original_run_once()

        runtime.recovery.recover_nonterminal_runs = recover
        runtime.coordinator.run_once = run_once

        runtime.driver.start()
        runtime.driver.run_once()

        assert calls[:2] == ["recovery", "coordinator"]
    finally:
        runtime.close()
