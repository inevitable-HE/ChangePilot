from __future__ import annotations

import json
from dataclasses import dataclass, field

from pydantic import TypeAdapter
from sqlalchemy import Engine, insert, select, update

from changepilot.planning.adapters.knowledge.schema import (
    model_cache,
    model_usage,
    planning_attempts,
    planning_plans,
    planning_sessions,
)
from changepilot.planning.application.model_gateway import RecordedModelUsage
from changepilot.planning.domain.models import (
    ChangePlan,
    ChangeRequest,
    PlanningResult,
    PreparedWorkflow,
)
from changepilot.planning.ports.models import ModelResponse


_RESULT_ADAPTER = TypeAdapter(PlanningResult)


@dataclass(slots=True)
class _MemorySession:
    request: ChangeRequest
    attempts: list[PlanningResult] = field(default_factory=list)
    plans: list[ChangePlan] = field(default_factory=list)
    prepared: PreparedWorkflow | None = None


class InMemoryPlanningRepository:
    def __init__(self) -> None:
        self._sessions: dict[str, _MemorySession] = {}

    def create_session(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> None:
        if session_id in self._sessions:
            raise ValueError(f"duplicate planning session {session_id!r}")
        self._sessions[session_id] = _MemorySession(request=request)

    def get_request(self, session_id: str) -> ChangeRequest:
        return self._get(session_id).request

    def update_request(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> None:
        self._get(session_id).request = request

    def next_attempt_number(self, session_id: str) -> int:
        return len(self._get(session_id).attempts) + 1

    def next_plan_version(self, session_id: str) -> int:
        return len(self._get(session_id).plans) + 1

    def save_attempt(
        self,
        session_id: str,
        attempt_no: int,
        request: ChangeRequest,
        result: PlanningResult,
    ) -> None:
        session = self._get(session_id)
        if attempt_no != len(session.attempts) + 1:
            raise ValueError("planning attempt number is not sequential")
        session.request = request
        session.attempts.append(result)

    def save_plan(self, session_id: str, plan: ChangePlan) -> None:
        session = self._get(session_id)
        if plan.version != len(session.plans) + 1:
            raise ValueError("plan version is not sequential")
        session.plans.append(plan)

    def get_latest_plan(self, session_id: str) -> ChangePlan:
        plans = self._get(session_id).plans
        if not plans:
            raise LookupError("planning session has no plan")
        return plans[-1]

    def get_latest_result(self, session_id: str) -> PlanningResult:
        attempts = self._get(session_id).attempts
        if not attempts:
            raise LookupError("planning session has no attempts")
        return attempts[-1]

    def bind_prepared_workflow(
        self,
        session_id: str,
        prepared: PreparedWorkflow,
    ) -> None:
        self._get(session_id).prepared = prepared

    def _get(self, session_id: str) -> _MemorySession:
        try:
            return self._sessions[session_id]
        except KeyError as exc:
            raise LookupError(f"unknown planning session {session_id!r}") from exc


class SQLitePlanningRepository:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def create_session(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(planning_sessions).values(
                    session_id=session_id,
                    request_json=request.model_dump_json(),
                    status="planning",
                    latest_plan_version=0,
                )
            )

    def get_request(self, session_id: str) -> ChangeRequest:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(planning_sessions.c.request_json).where(
                    planning_sessions.c.session_id == session_id
                )
            ).scalar_one_or_none()
        if value is None:
            raise LookupError(f"unknown planning session {session_id!r}")
        return ChangeRequest.model_validate_json(value)

    def update_request(
        self,
        session_id: str,
        request: ChangeRequest,
    ) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(planning_sessions)
                .where(planning_sessions.c.session_id == session_id)
                .values(
                    request_json=request.model_dump_json(),
                    status="planning",
                    updated_at="CURRENT_TIMESTAMP",
                )
            )
            if result.rowcount != 1:
                raise LookupError(f"unknown planning session {session_id!r}")

    def next_attempt_number(self, session_id: str) -> int:
        with self._engine.connect() as connection:
            latest = connection.execute(
                select(planning_attempts.c.attempt_no)
                .where(planning_attempts.c.session_id == session_id)
                .order_by(planning_attempts.c.attempt_no.desc())
                .limit(1)
            ).scalar_one_or_none()
        return int(latest or 0) + 1

    def next_plan_version(self, session_id: str) -> int:
        with self._engine.connect() as connection:
            latest = connection.execute(
                select(planning_sessions.c.latest_plan_version).where(
                    planning_sessions.c.session_id == session_id
                )
            ).scalar_one_or_none()
        if latest is None:
            raise LookupError(f"unknown planning session {session_id!r}")
        return int(latest) + 1

    def save_attempt(
        self,
        session_id: str,
        attempt_no: int,
        request: ChangeRequest,
        result: PlanningResult,
    ) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(planning_attempts).values(
                    session_id=session_id,
                    attempt_no=attempt_no,
                    input_json=request.model_dump_json(),
                    result_json=result.model_dump_json(),
                )
            )
            connection.execute(
                update(planning_sessions)
                .where(planning_sessions.c.session_id == session_id)
                .values(
                    request_json=request.model_dump_json(),
                    status=result.kind,
                    updated_at="CURRENT_TIMESTAMP",
                )
            )

    def save_plan(self, session_id: str, plan: ChangePlan) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                update(planning_plans)
                .where(
                    planning_plans.c.session_id == session_id,
                    planning_plans.c.status == "ready",
                )
                .values(status="superseded")
            )
            connection.execute(
                insert(planning_plans).values(
                    plan_id=plan.plan_id,
                    version=plan.version,
                    session_id=session_id,
                    content_digest=plan.content_digest,
                    knowledge_snapshot_digest=plan.knowledge_snapshot_digest,
                    status="ready",
                    plan_json=plan.model_dump_json(),
                )
            )
            connection.execute(
                update(planning_sessions)
                .where(planning_sessions.c.session_id == session_id)
                .values(
                    latest_plan_version=plan.version,
                    status="plan_ready",
                    updated_at="CURRENT_TIMESTAMP",
                )
            )

    def get_latest_plan(self, session_id: str) -> ChangePlan:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(planning_plans.c.plan_json)
                .where(planning_plans.c.session_id == session_id)
                .order_by(planning_plans.c.version.desc())
                .limit(1)
            ).scalar_one_or_none()
        if value is None:
            raise LookupError("planning session has no plan")
        return ChangePlan.model_validate_json(value)

    def get_latest_result(self, session_id: str) -> PlanningResult:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(planning_attempts.c.result_json)
                .where(planning_attempts.c.session_id == session_id)
                .order_by(planning_attempts.c.attempt_no.desc())
                .limit(1)
            ).scalar_one_or_none()
        if value is None:
            raise LookupError("planning session has no attempts")
        return _RESULT_ADAPTER.validate_json(value)

    def bind_prepared_workflow(
        self,
        session_id: str,
        prepared: PreparedWorkflow,
    ) -> None:
        with self._engine.begin() as connection:
            result = connection.execute(
                update(planning_plans)
                .where(
                    planning_plans.c.session_id == session_id,
                    planning_plans.c.plan_id == prepared.plan_id,
                    planning_plans.c.version == prepared.plan_version,
                )
                .values(
                    definition_id=prepared.definition_id,
                    definition_version=prepared.definition_version,
                    definition_digest=prepared.definition_digest,
                    status="prepared",
                )
            )
            if result.rowcount != 1:
                raise LookupError("latest plan cannot be bound")


class SQLiteModelCache:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def get(self, key: str) -> ModelResponse | None:
        with self._engine.connect() as connection:
            value = connection.execute(
                select(model_cache.c.response_json).where(
                    model_cache.c.cache_key == key
                )
            ).scalar_one_or_none()
        return None if value is None else ModelResponse.model_validate_json(value)

    def put(self, key: str, response: ModelResponse) -> None:
        with self._engine.begin() as connection:
            existing = connection.execute(
                select(model_cache.c.cache_key).where(
                    model_cache.c.cache_key == key
                )
            ).scalar_one_or_none()
            if existing is None:
                connection.execute(
                    insert(model_cache).values(
                        cache_key=key,
                        response_json=response.model_dump_json(),
                    )
                )


class SQLiteUsageRecorder:
    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def record(self, record: RecordedModelUsage) -> None:
        with self._engine.begin() as connection:
            connection.execute(
                insert(model_usage).values(
                    request_digest=record.request_digest,
                    model=record.model,
                    latency_ms=record.latency_ms,
                    input_tokens=record.input_tokens,
                    output_tokens=record.output_tokens,
                    total_tokens=record.total_tokens,
                    estimated_cost_microunits=record.estimated_cost_microunits,
                    cache_hit=int(record.cache_hit),
                    retry_count=record.retry_count,
                )
            )
