from __future__ import annotations

from typing import NotRequired, TypedDict

from changepilot.planning.application.retrieval import RetrievalResult
from changepilot.planning.domain.models import (
    ChangePlan,
    ChangeRequest,
    PlanningResult,
)


class PlanningState(TypedDict):
    session_id: str
    request: ChangeRequest
    plan_version: int
    retrieval: NotRequired[RetrievalResult]
    raw_payload: NotRequired[dict[str, object]]
    plan: NotRequired[ChangePlan]
    validation_errors: NotRequired[tuple[str, ...]]
    repair_count: NotRequired[int]
    result: NotRequired[PlanningResult]
