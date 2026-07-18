from __future__ import annotations

import math
from enum import Enum

import pytest

from changepilot.workflow.application.approvals import (
    ApprovalRequest,
    approval_binding_digest,
)
from changepilot.workflow.application.tooling import redact
from changepilot.workflow.ports.tools import SecretRef


def test_binding_digest_is_stable_for_equivalent_argument_order() -> None:
    first = approval_binding_digest(
        "definition-digest",
        "schema-migrate",
        "schema.apply",
        "1.0.0",
        {"target": "v2", "options": {"online": True, "batch": 50}},
    )
    second = approval_binding_digest(
        "definition-digest",
        "schema-migrate",
        "schema.apply",
        "1.0.0",
        {"options": {"batch": 50, "online": True}, "target": "v2"},
    )

    assert first == second
    assert len(first) == 64


@pytest.mark.parametrize(
    ("field", "changed"),
    [
        ("plan_digest", "changed-definition"),
        ("step_id", "changed-step"),
        ("tool_name", "schema.replace"),
        ("tool_version", "2.0.0"),
        ("redacted_arguments", {"target": "v3"}),
    ],
)
def test_binding_digest_changes_with_every_bound_component(
    field: str,
    changed: object,
) -> None:
    values: dict[str, object] = {
        "plan_digest": "definition-digest",
        "step_id": "schema-migrate",
        "tool_name": "schema.apply",
        "tool_version": "1.0.0",
        "redacted_arguments": {"target": "v2"},
    }
    baseline = approval_binding_digest(**values)
    values[field] = changed

    assert approval_binding_digest(**values) != baseline


def test_binding_digest_never_depends_on_raw_secret_reference_name() -> None:
    first = redact(
        {
            "target": "v2",
            "credential": SecretRef(provider="env", name="PROD_DB_PASSWORD"),
        }
    )
    second = redact(
        {
            "target": "v2",
            "credential": SecretRef(provider="env", name="OTHER_DB_PASSWORD"),
        }
    )

    assert first == second
    assert approval_binding_digest(
        "definition-digest",
        "schema-migrate",
        "schema.apply",
        "1.0.0",
        first,
    ) == approval_binding_digest(
        "definition-digest",
        "schema-migrate",
        "schema.apply",
        "1.0.0",
        second,
    )


@pytest.mark.parametrize(
    "arguments",
    [
        {1: "integer", "1": "string"},
        {"nested": {False: "boolean-key"}},
        {"value": object()},
        {"value": b"bytes"},
        {"value": {"not", "json"}},
        {"value": math.nan},
        {"value": math.inf},
        {"value": -math.inf},
    ],
)
def test_binding_digest_rejects_non_canonical_json_before_key_coercion(
    arguments: object,
) -> None:
    with pytest.raises(ValueError, match="canonical JSON"):
        approval_binding_digest(
            "definition-digest",
            "schema-migrate",
            "schema.apply",
            "1.0.0",
            arguments,
        )


def test_binding_digest_accepts_finite_json_primitives_and_nested_lists() -> None:
    digest = approval_binding_digest(
        "definition-digest",
        "schema-migrate",
        "schema.apply",
        "1.0.0",
        {
            "values": [None, True, False, 0, -2, 1.5, "text"],
            "nested": {"items": []},
        },
    )

    assert len(digest) == 64


def _approval_request(**changes: object) -> ApprovalRequest:
    values: dict[str, object] = {
        "id": "approval-1",
        "run_id": "run-1",
        "definition_digest": "definition-digest",
        "step_id": "schema-migrate",
        "tool_name": "schema.apply",
        "tool_version": "1.0.0",
        "redacted_arguments": {"target": "v2"},
        "binding_digest": "binding-digest",
        "risk_reasons": ("tool_risk_high",),
        "created_at": "2026-07-17T00:00:00+00:00",
    }
    values.update(changes)
    return ApprovalRequest(**values)


def test_approval_request_normalizes_status_to_strong_enum() -> None:
    request = _approval_request()

    assert isinstance(request.status, Enum)
    assert request.status.value == "pending"


@pytest.mark.parametrize(
    "changes",
    [
        {"version": -1},
        {"version": True},
        {"status": "unknown"},
        {"version": 1},
        {"decision": "approved"},
        {"actor": "raw-actor"},
        {"reason": "raw-reason"},
        {"decided_at": "2026-07-17T00:01:00+00:00"},
        {"status": "approved", "version": 0, "decision": "approved"},
        {"status": "approved", "version": 1, "decision": None},
        {"status": "rejected", "version": 1, "decision": "approved"},
        {"status": "invalidated", "version": 1, "decision": "approved"},
        {"status": "invalidated", "version": 1, "decided_at": None},
    ],
)
def test_approval_request_rejects_inconsistent_state(changes: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _approval_request(**changes)
