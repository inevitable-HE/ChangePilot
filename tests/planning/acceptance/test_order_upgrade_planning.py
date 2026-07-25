from __future__ import annotations

from pathlib import Path

from changepilot.planning.adapters.models.mock import MockModelGateway
from changepilot.planning.application.ingestion import InMemoryKnowledgeStore
from changepilot.planning.application.retrieval import HybridRetriever
from changepilot.planning.domain.models import PlanReady
from changepilot.workflow.application.tooling import ToolRegistry
from tests.planning.support import (
    AllChunksIndex,
    ingest_example_runbooks,
    make_planning_service,
    model_response,
    order_upgrade_plan_payload,
    order_upgrade_policies,
    order_upgrade_request,
)
from tests.support.order_upgrade import (
    drive_until_state,
    make_order_upgrade_runtime,
)


def test_agent_plan_hands_off_to_runtime_and_stops_for_approval(
    tmp_path: Path,
) -> None:
    runtime = make_order_upgrade_runtime(tmp_path / "agent-runtime.db")
    try:
        tools = ToolRegistry(runtime.tools.values())
        store = InMemoryKnowledgeStore()
        ingest_example_runbooks(store)
        retrieval = HybridRetriever(
            store=store,
            index=AllChunksIndex(store),
        ).retrieve("orders schema migration rollback", top_k=8)
        model = MockModelGateway(
            [model_response(order_upgrade_plan_payload(retrieval.evidence))]
        )
        planning = make_planning_service(
            store=store,
            tools=tools,
            policies=order_upgrade_policies(),
            model=model,
        )

        result = planning.start(order_upgrade_request())

        assert isinstance(result, PlanReady)
        prepared = planning.prepare_workflow(result.session_id)
        run_id = runtime.workflow.create_run(dict(prepared.payload))
        runtime.selected_run[0] = run_id
        runtime.driver.start()
        drive_until_state(runtime, "waiting_approval")

        view = runtime.query.get_run(run_id)
        assert view.pending_approval is not None
        assert view.pending_approval.step_id == "migrate-schema"
        assert runtime.tools["schema.migrate"].contexts == []
        assert set(runtime.calls[:2]) == {"service.inspect", "schema.inspect"}
        assert runtime.calls[2] == "upgrade.precheck"
        assert model.call_count == 1
    finally:
        runtime.close()
