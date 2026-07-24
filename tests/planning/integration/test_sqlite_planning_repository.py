from __future__ import annotations

import json

from sqlalchemy import select

from changepilot.planning.adapters.knowledge.schema import model_usage
from changepilot.planning.adapters.knowledge.sqlite import initialize_planning_schema
from changepilot.planning.adapters.persistence import (
    SQLiteModelCache,
    SQLitePlanningRepository,
    SQLiteUsageRecorder,
)
from changepilot.planning.application.model_gateway import RecordedModelUsage
from changepilot.planning.domain.models import (
    PlanReady,
    PreparedWorkflow,
    RetrievedEvidence,
    TrustLevel,
)
from changepilot.planning.ports.models import ModelResponse, ModelUsage
from changepilot.workflow.adapters.persistence.sqlite import create_sqlite_engine
from tests.planning.integration.test_planning_graph import (
    _complete_request,
    _harness,
    _plan_payload,
)
from changepilot.planning.domain.models import ChangePlan


def test_sqlite_planning_repository_round_trips_plan_and_binding(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "planning.db")
    initialize_planning_schema(engine)
    repository = SQLitePlanningRepository(engine)
    harness = _harness([])
    plan = ChangePlan.model_validate_json(
        json.dumps(_plan_payload(harness.evidence_ref))
    ).model_copy(update={"version": 1})
    evidence = RetrievedEvidence(
        reference=harness.evidence_ref,
        text="Schema writes require approval.",
        trust_level=TrustLevel.AUTHORITATIVE,
        hybrid_score=0.5,
    )
    request = _complete_request()
    result = PlanReady(
        session_id="session-1",
        plan=plan,
        evidence=(evidence,),
    )
    repository.create_session("session-1", request)
    repository.save_attempt("session-1", 1, request, result)
    repository.save_plan("session-1", plan)
    prepared = PreparedWorkflow(
        plan_id=plan.plan_id,
        plan_version=plan.version,
        knowledge_snapshot_digest=plan.knowledge_snapshot_digest,
        definition_id=plan.plan_id,
        definition_version=plan.version,
        definition_digest="definition-digest",
        payload={"definition_id": plan.plan_id, "version": plan.version},
    )
    repository.bind_prepared_workflow("session-1", prepared)

    assert repository.get_latest_plan("session-1") == plan
    assert repository.get_latest_result("session-1") == result
    assert repository.next_attempt_number("session-1") == 2
    assert repository.next_plan_version("session-1") == 2
    engine.dispose()


def test_sqlite_model_cache_and_usage_store_only_structured_metadata(tmp_path) -> None:
    engine = create_sqlite_engine(tmp_path / "model.db")
    initialize_planning_schema(engine)
    cache = SQLiteModelCache(engine)
    recorder = SQLiteUsageRecorder(engine)
    response = ModelResponse(
        payload={"plan_id": "plan-1"},
        usage=ModelUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        model="mock",
        latency_ms=2,
        finish_reason="stop",
    )
    cache.put("cache-digest", response)
    recorder.record(
        RecordedModelUsage(
            request_digest="request-digest",
            model="mock",
            latency_ms=2,
            input_tokens=10,
            output_tokens=5,
            total_tokens=15,
            cache_hit=False,
            retry_count=0,
            estimated_cost_microunits=1,
        )
    )

    assert cache.get("cache-digest") == response
    with engine.connect() as connection:
        row = connection.execute(select(model_usage)).mappings().one()
    assert row["request_digest"] == "request-digest"
    assert "plan_id" not in row
    engine.dispose()
