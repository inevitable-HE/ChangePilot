from __future__ import annotations

import pytest

from changepilot.planning.domain.failures import PlanValidationError
from changepilot.planning.domain.knowledge import RunbookDocument
from changepilot.planning.domain.models import TrustLevel
from changepilot.planning.application.ingestion import RunbookIngestionService
from tests.planning.integration.test_planning_graph import (
    _complete_request,
    _harness,
    _model_response,
    _plan_payload,
)


def test_prepare_rejects_plan_after_knowledge_changes() -> None:
    harness = _harness([])
    harness.model._scripted.append(
        _model_response(_plan_payload(harness.evidence_ref))
    )
    assert harness.service.start(_complete_request()).kind == "plan_ready"
    RunbookIngestionService(harness.store).ingest(
        RunbookDocument(
            document_id="new-policy",
            version="1",
            source="runbooks/new-policy.md",
            trust_level=TrustLevel.INTERNAL,
            content="# Policy\nA new policy became effective.",
            policy_key="new-policy",
            effective_at="2026-07-25",
        )
    )

    with pytest.raises(PlanValidationError, match="knowledge changed"):
        harness.service.prepare_workflow("session-1")


def test_prepare_returns_runtime_definition_when_plan_is_fresh() -> None:
    harness = _harness([])
    harness.model._scripted.append(
        _model_response(_plan_payload(harness.evidence_ref))
    )
    harness.service.start(_complete_request())

    prepared = harness.service.prepare_workflow("session-1")

    assert prepared.definition_id == "session-1-plan"
    assert prepared.definition_digest
