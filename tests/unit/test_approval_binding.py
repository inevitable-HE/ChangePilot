from __future__ import annotations

import pytest

from changepilot.workflow.application.approvals import approval_binding_digest
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
