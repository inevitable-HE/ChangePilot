from __future__ import annotations

import pytest
from pydantic import ValidationError

from changepilot.planning.domain.models import (
    ChangePlan,
    ChangeRequest,
    PlanStep,
    PlanToolRef,
)


def _plan() -> ChangePlan:
    return ChangePlan(
        plan_id="plan-1",
        version=1,
        goal="upgrade orders",
        steps=(
            PlanStep(
                id="inspect",
                tool=PlanToolRef(name="service.inspect", version="1.0.0"),
            ),
        ),
        knowledge_snapshot_digest="knowledge-sha",
        prompt_version="planning-v1",
        tool_policy_version="policy-v1",
    )


def test_change_request_rejects_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        ChangeRequest(
            service_id="orders",
            current_version="1.0",
            target_version="2.0",
            change_summary="upgrade",
            success_conditions=("healthy",),
            unexpected=True,
        )


def test_change_request_allows_missing_context_for_clarification() -> None:
    request = ChangeRequest(change_summary="upgrade orders")

    assert request.target_version is None


def test_plan_digest_is_stable_for_equivalent_content() -> None:
    assert _plan().content_digest == _plan().content_digest


def test_plan_rejects_duplicate_step_ids() -> None:
    step = PlanStep(
        id="inspect",
        tool=PlanToolRef(name="service.inspect", version="1.0.0"),
    )

    with pytest.raises(ValidationError, match="step ids must be unique"):
        ChangePlan(
            plan_id="plan-1",
            version=1,
            goal="upgrade",
            steps=(step, step),
            knowledge_snapshot_digest="knowledge-sha",
            prompt_version="planning-v1",
            tool_policy_version="policy-v1",
        )
