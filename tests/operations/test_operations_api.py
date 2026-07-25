from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from changepilot.operations.adapters.api import create_operations_app
from changepilot.operations.application.service import OperationsService


@pytest.fixture
def operations(tmp_path: Path):
    service = OperationsService(tmp_path / "operations")
    client = TestClient(create_operations_app(service))
    try:
        yield service, client
    finally:
        client.close()
        service.close()


def _request(*, scenario: str = "success") -> dict[str, object]:
    return {
        "service_id": "order-service",
        "current_version": "v1",
        "target_version": "v2",
        "change_summary": "Upgrade the order service and database schema.",
        "success_conditions": ["V2 health and smoke checks pass"],
        "constraints": ["Require approval before schema migration"],
        "scenario": scenario,
    }


def _submit(client: TestClient, *, scenario: str = "success") -> dict[str, object]:
    response = client.post("/api/requests", json=_request(scenario=scenario))
    assert response.status_code == 201
    return response.json()


def _approval(snapshot: dict[str, object], **updates) -> dict[str, object]:
    pending = snapshot["pending_approval"]
    payload = {
        "decision": "approved",
        "expected_version": pending["version"],
        "binding_digest": pending["binding_digest"],
        "actor": "local-operator",
        "reason": "Reviewed migration evidence and rollback policy.",
    }
    payload.update(updates)
    return payload


def test_incomplete_request_returns_clarification_without_run(operations) -> None:
    _, client = operations

    response = client.post(
        "/api/requests",
        json={
            "change_summary": "Upgrade the order service.",
            "success_conditions": [],
            "constraints": [],
            "scenario": "success",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "clarification_required"
    assert payload["run_id"] is None
    assert len(payload["clarification_questions"]) == 3
    assert client.get("/api/runs").json() == []


def test_successful_operator_flow_exposes_plan_events_and_reports(
    operations,
) -> None:
    _, client = operations
    request = _submit(client)
    run_id = request["run_id"]

    snapshot = client.get(f"/api/runs/{run_id}").json()
    plan = client.get(f"/api/runs/{run_id}/plan").json()

    assert snapshot["summary"]["state"] == "waiting_approval"
    migration = next(
        step for step in plan["steps"] if step["step_id"] == "migrate-schema"
    )
    assert migration["risk"] == "high"
    assert migration["approval_required"] is True
    assert migration["compensation_tool"]["name"] == "schema.rollback"

    before = client.get(f"/api/runs/{run_id}/events").json()
    cursor = before["events"][2]["sequence"]
    tail = client.get(
        f"/api/runs/{run_id}/events",
        params={"after": cursor},
    ).json()
    assert all(event["sequence"] > cursor for event in tail["events"])

    approved = client.post(
        f"/api/runs/{run_id}/approval",
        json=_approval(snapshot),
    )
    assert approved.status_code == 200
    assert approved.json()["summary"]["state"] == "succeeded"

    report = client.get(f"/api/runs/{run_id}/report.json")
    markdown = client.get(f"/api/runs/{run_id}/report.md")
    assert report.status_code == 200
    assert report.json()["run"]["summary"]["state"] == "succeeded"
    assert report.json()["model_usage"]["calls"] == 0
    assert markdown.status_code == 200
    assert "# ChangePilot Run Report" in markdown.text

    stream = client.get(f"/api/runs/{run_id}/events/stream")
    assert stream.status_code == 200
    assert "event: audit" in stream.text
    assert "id: " in stream.text


def test_stale_approval_is_rejected_with_authoritative_state(
    operations,
) -> None:
    _, client = operations
    run_id = _submit(client)["run_id"]
    snapshot = client.get(f"/api/runs/{run_id}").json()

    response = client.post(
        f"/api/runs/{run_id}/approval",
        json=_approval(snapshot, expected_version=999),
    )

    assert response.status_code == 409
    refreshed = client.get(f"/api/runs/{run_id}").json()
    assert refreshed["summary"]["state"] == "waiting_approval"
    assert refreshed["pending_approval"] is not None


def test_recovery_command_reconciles_unknown_effect_once(operations) -> None:
    service, client = operations
    run_id = _submit(client, scenario="recovery")["run_id"]
    waiting = client.get(f"/api/runs/{run_id}").json()

    interrupted = client.post(
        f"/api/runs/{run_id}/approval",
        json=_approval(waiting),
    )

    assert interrupted.status_code == 200
    interrupted_snapshot = interrupted.json()
    assert any(
        step["state"] == "result_unknown"
        for step in interrupted_snapshot["steps"]
    )

    recovered = client.post(
        f"/api/runs/{run_id}/recover",
        json={
            "expected_run_revision": interrupted_snapshot["summary"]["revision"]
        },
    )
    assert recovered.status_code == 200
    assert recovered.json()["summary"]["state"] == "succeeded"
    runtime = service._runtime_for(run_id)
    assert runtime.faults.call_count(
        runtime.sandbox_id,
        "schema.migrate",
    ) == 1


def test_rejected_approval_cancels_without_side_effects(operations) -> None:
    _, client = operations
    run_id = _submit(client)["run_id"]
    snapshot = client.get(f"/api/runs/{run_id}").json()

    rejected = client.post(
        f"/api/runs/{run_id}/approval",
        json=_approval(
            snapshot,
            decision="rejected",
            reason="Change window was withdrawn.",
        ),
    )

    assert rejected.status_code == 200
    assert rejected.json()["summary"]["state"] == "cancelled"


def test_terminal_run_can_be_loaded_after_service_restart(tmp_path: Path) -> None:
    root = tmp_path / "operations"
    first = OperationsService(root)
    client = TestClient(create_operations_app(first))
    request = _submit(client)
    run_id = request["run_id"]
    snapshot = client.get(f"/api/runs/{run_id}").json()
    client.post(
        f"/api/runs/{run_id}/approval",
        json=_approval(snapshot),
    )
    client.close()
    first.close()

    second = OperationsService(root)
    try:
        restored = second.get_snapshot(run_id)
        assert restored.summary.state == "succeeded"
        assert second.get_events(run_id).next_cursor > 0
    finally:
        second.close()
