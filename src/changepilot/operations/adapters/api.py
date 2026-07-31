from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Sequence
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, PlainTextResponse, StreamingResponse

from changepilot.evaluation.domain.models import EvaluationReport
from changepilot.operations.application.service import (
    OperationsConflictError,
    OperationsService,
)
from changepilot.operations.domain.models import (
    ApprovalCommand,
    ChangeRequestInput,
    RecoveryCommand,
)
from changepilot.workflow.ports.persistence import OptimisticLockError


def create_operations_app(
    service: OperationsService,
    *,
    evaluation_roots: Sequence[str | Path] = (),
) -> FastAPI:
    report_roots = tuple(Path(root).resolve() for root in evaluation_roots)
    app = FastAPI(
        title="ChangePilot Operations API",
        version="0.1.0",
    )

    @app.exception_handler(LookupError)
    async def _not_found(_request: Request, error: LookupError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(error)})

    @app.exception_handler(OperationsConflictError)
    @app.exception_handler(OptimisticLockError)
    async def _conflict(_request: Request, error: Exception) -> JSONResponse:
        return JSONResponse(status_code=409, content={"detail": str(error)})

    @app.get("/api/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/api/requests", status_code=201)
    def submit_request(payload: ChangeRequestInput) -> dict[str, object]:
        return service.submit_request(payload).model_dump(mode="json")

    @app.get("/api/requests")
    def list_requests() -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json") for item in service.list_requests()
        ]

    @app.get("/api/runs")
    def list_runs(state: str | None = None) -> list[dict[str, object]]:
        return [
            item.model_dump(mode="json")
            for item in service.list_runs(state=state)
        ]

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str) -> dict[str, object]:
        return service.get_snapshot(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/guidance")
    def get_guidance(run_id: str) -> dict[str, object]:
        return service.get_guidance(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/planning")
    def get_planning_trace(run_id: str) -> dict[str, object]:
        return service.get_planning_trace(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/plan")
    def get_plan(run_id: str) -> dict[str, object]:
        return service.get_plan(run_id).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/events")
    def get_events(
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> dict[str, object]:
        return service.get_events(run_id, after).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/events/stream")
    def stream_events(
        request: Request,
        run_id: str,
        after: int = Query(default=0, ge=0),
    ) -> StreamingResponse:
        return StreamingResponse(
            _event_stream(service, request, run_id, after),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/api/runs/{run_id}/approval")
    def decide_approval(
        run_id: str,
        command: ApprovalCommand,
    ) -> dict[str, object]:
        return service.decide_approval(run_id, command).model_dump(mode="json")

    @app.post("/api/runs/{run_id}/recover")
    def recover(
        run_id: str,
        command: RecoveryCommand,
    ) -> dict[str, object]:
        return service.recover(run_id, command).model_dump(mode="json")

    @app.get("/api/runs/{run_id}/report.json")
    def json_report(run_id: str) -> dict[str, object]:
        report = service.render_report(run_id, markdown=False)
        if not isinstance(report, dict):
            raise HTTPException(status_code=500, detail="invalid report")
        return report

    @app.get("/api/runs/{run_id}/report.md")
    def markdown_report(run_id: str) -> PlainTextResponse:
        report = service.render_report(run_id, markdown=True)
        if not isinstance(report, str):
            raise HTTPException(status_code=500, detail="invalid report")
        return PlainTextResponse(report, media_type="text/markdown")

    @app.get("/api/evaluations")
    def evaluation_history() -> list[dict[str, object]]:
        reports: dict[str, EvaluationReport] = {}
        for root in report_roots:
            if not root.is_dir():
                continue
            for report_path in root.rglob("evaluation.json"):
                try:
                    report = EvaluationReport.model_validate_json(
                        report_path.read_text(encoding="utf-8"),
                        strict=False,
                    )
                except (OSError, ValueError):
                    continue
                reports[report.run_id] = report
        return [
            report.model_dump(mode="json")
            for report in sorted(
                reports.values(),
                key=lambda item: item.metadata.generated_at
                or datetime.min.replace(
                    tzinfo=timezone.utc,
                ),
                reverse=True,
            )
        ]

    return app


async def _event_stream(
    service: OperationsService,
    request: Request,
    run_id: str,
    after: int,
) -> AsyncIterator[str]:
    cursor = after
    while True:
        page = service.get_events(run_id, cursor)
        for event in page.events:
            cursor = int(event["sequence"])
            yield (
                f"id: {cursor}\n"
                "event: audit\n"
                f"data: {json.dumps(event, sort_keys=True)}\n\n"
            )
        snapshot = service.get_snapshot(run_id)
        if snapshot.summary.state in {
            "succeeded",
            "failed",
            "cancelled",
            "compensated",
            "compensation_failed",
            "manual_intervention",
        }:
            return
        if await request.is_disconnected():
            return
        if not page.events:
            yield ": heartbeat\n\n"
        await asyncio.sleep(0.25)
